(() => {
  const $ = (id) => document.getElementById(id);

  const WINDOW_S = 10;
  /** 仿真步长 (s)，100Hz */
  const SIM_DT = 0.01;
  /** 速度环时间常数 (s)：实际速度一阶跟上 PID 给出的速度指令 */
  const TAU_V = 0.08;
  /** 与项目根 config.json 中 AI_TUNING_* 一致，启动时由 /api/tuning/ui-settings 覆盖 */
  let tuningTotalRounds = 10;
  let tuningSampleIntervalS = 0.1;
  let tuningRoundDurationS = 4.0;

  let mode = "virtual"; // "virtual" | "serial"
  /** @type {WebSocket | null} */
  let serialWs = null;
  let serialConnected = false;
  let serialIngestPaused = false;
  let serialAwaitingSample = false;
  let spPidTimer = 0;
  let chart;
  /** @type {number} */
  let simTimerId = 0;
  let simT = 0;
  let simY = 0;
  let simV = 0;
  let simLastVCmd = 0;
  let simInt = 0;
  /** @type {number | null} */
  let simPrevE = null;
  const bufTarget = [];
  const bufActual = [];
  /** 位置误差 r−y，与右纵轴对齐 */
  const bufError = [];
  const tuningState = {
    pendingEnabled: false,
    effectiveEnabled: false,
    running: false,
    roundIndex: 0,
    roundStartT: null,
    sampleLastT: null,
    samples: [],
    promptPages: [],
    responsePages: [],
    promptPageIndex: -1,
    responsePageIndex: -1,
    historyRounds: [],
    promptPageLatencyMs: [],
    busy: false,
    finished: false,
  };

  const api = async (path, opts = {}) => {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = text;
    }
    if (!res.ok) {
      let msg = res.statusText;
      if (typeof data === "object" && data) {
        if (typeof data.detail === "string") {
          msg = data.detail;
        } else if (Array.isArray(data.detail)) {
          msg = data.detail
            .map((x) => (typeof x === "object" && x.msg ? `${x.loc?.join?.(".") || ""}: ${x.msg}` : JSON.stringify(x)))
            .join("; ");
        } else if (typeof data.message === "string") {
          msg = data.message;
        }
      } else if (typeof data === "string" && data.trim()) {
        msg = data.trim();
      }
      const err = new Error(msg || res.statusText);
      err.status = res.status;
      err.body = data;
      throw err;
    }
    return data;
  };

  const readFloat = (id, fallback) => {
    const v = parseFloat(String($(id).value || ""));
    return Number.isFinite(v) ? v : fallback;
  };

  const readInt = (id, fallback) => {
    const v = parseInt(String($(id).value || ""), 10);
    return Number.isFinite(v) ? v : fallback;
  };

  const setPageText = (kind, text) => {
    if (kind === "prompt") {
      $("ta-prompt").value = text || "";
      return;
    }
    $("ta-response").value = text || "";
  };

  const updatePageUI = (kind) => {
    const pages = kind === "prompt" ? tuningState.promptPages : tuningState.responsePages;
    const idxKey = kind === "prompt" ? "promptPageIndex" : "responsePageIndex";
    const currentIndex = tuningState[idxKey];
    const total = pages.length;
    const display = total ? currentIndex + 1 : 0;
    $(`${kind}-page-indicator`).textContent = `${display} / ${total}`;
    $(`${kind}-page-jump`).value = String(Math.max(1, display || 1));
    setPageText(kind, total ? pages[currentIndex] : "");
    if (kind === "prompt") updatePromptRoundLatencyDisplay();
  };

  const updatePromptRoundLatencyDisplay = () => {
    const el = $("prompt-round-trip-ms");
    if (!el) return;
    const idx = tuningState.promptPageIndex;
    const arr = tuningState.promptPageLatencyMs;
    if (idx < 0 || !arr.length || idx >= arr.length) {
      el.textContent = "组装→响应：—";
      return;
    }
    const v = arr[idx];
    if (v == null || !Number.isFinite(v)) {
      el.textContent = "组装→响应：—";
    } else {
      el.textContent = `组装→响应：${v.toFixed(1)} ms`;
    }
  };

  const setPageIndex = (kind, nextIndex) => {
    const pages = kind === "prompt" ? tuningState.promptPages : tuningState.responsePages;
    const idxKey = kind === "prompt" ? "promptPageIndex" : "responsePageIndex";
    if (!pages.length) {
      tuningState[idxKey] = -1;
      updatePageUI(kind);
      return;
    }
    const maxIndex = pages.length - 1;
    tuningState[idxKey] = Math.max(0, Math.min(maxIndex, nextIndex));
    updatePageUI(kind);
  };

  const pushPage = (kind, text, latencyMs) => {
    const pages = kind === "prompt" ? tuningState.promptPages : tuningState.responsePages;
    pages.push(text || "");
    if (kind === "prompt") {
      tuningState.promptPageLatencyMs.push(
        typeof latencyMs === "number" && Number.isFinite(latencyMs) ? latencyMs : null
      );
    }
    setPageIndex(kind, pages.length - 1);
  };

  const clearPagedLogs = () => {
    tuningState.promptPages = [];
    tuningState.responsePages = [];
    tuningState.promptPageIndex = -1;
    tuningState.responsePageIndex = -1;
    tuningState.historyRounds = [];
    tuningState.promptPageLatencyMs = [];
    updatePageUI("prompt");
    updatePageUI("response");
  };

  const setReadonlyTextareas = (readonly) => {
    $("ta-prompt").readOnly = readonly;
    $("ta-response").readOnly = readonly;
  };

  const updateTuningEffectiveLabel = () => {
    const pending = tuningState.pendingEnabled ? "接入" : "未接入";
    const current = tuningState.effectiveEnabled ? "接入" : "未接入";
    $("vp-tuning-effective-state").textContent = `当前：${current} · 待生效：${pending}`;
  };

  const targetValue = (t, wave, period, amp, offset) => {
    const T = Math.abs(period) > 1e-6 ? Math.abs(period) : 1e-6;
    const ph = (t % T) / T;
    if (wave === "triangle") {
      const tri = ph < 0.5 ? -1 + 4 * ph : 3 - 4 * ph;
      return offset + amp * tri;
    }
    return offset + amp * Math.sin((2 * Math.PI * t) / T);
  };

  const trimBuffers = (tNow) => {
    const tMin = tNow - WINDOW_S - 2 * SIM_DT;
    while (bufTarget.length && bufTarget[0].x < tMin) bufTarget.shift();
    while (bufActual.length && bufActual[0].x < tMin) bufActual.shift();
    while (bufError.length && bufError[0].x < tMin) bufError.shift();
  };

  const syncChartBuffers = () => {
    if (!chart) return;
    chart.data.datasets[0].data = bufTarget;
    chart.data.datasets[1].data = bufActual;
    chart.data.datasets[2].data = bufError;
  };

  const updateChartStats = () => {
    const el = $("chart-stats");
    if (!el) return;
    if (!bufError.length) {
      el.textContent = "误差统计：—（无数据）";
      return;
    }
    const abs = bufError.map((p) => Math.abs(p.y));
    const last = bufError[bufError.length - 1].y;
    const meanAbs = abs.reduce((a, b) => a + b, 0) / abs.length;
    const maxAbs = abs.reduce((a, b) => Math.max(a, b), 0);
    el.textContent = [
      `当前误差 e=r−y：${last.toFixed(3)}`,
      `窗内平均 |e|：${meanAbs.toFixed(3)}`,
      `窗内最大 |e|：${maxAbs.toFixed(3)}`,
      `样本点：${bufError.length}`,
    ].join("\n");
  };

  const ensureChart = () => {
    const canvas = $("pid-chart");
    if (chart) return;
    chart = new Chart(canvas, {
      type: "line",
      data: {
        datasets: [
          {
            label: "目标",
            data: bufTarget,
            yAxisID: "y",
            borderColor: "#5ec8e8",
            borderWidth: 1.75,
            borderDash: [10, 5],
            tension: 0.2,
            pointRadius: 0,
            parsing: false,
          },
          {
            label: "实际",
            data: bufActual,
            yAxisID: "y",
            borderColor: "#e8c27a",
            borderWidth: 2.35,
            tension: 0.18,
            pointRadius: 0,
            parsing: false,
          },
          {
            label: "误差 (r−y)",
            data: bufError,
            yAxisID: "y1",
            borderColor: "#c89ef0",
            borderWidth: 1.6,
            borderDash: [4, 4],
            tension: 0.12,
            pointRadius: 0,
            parsing: false,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "nearest", intersect: false },
        scales: {
          x: {
            type: "linear",
            title: { display: true, text: "时间 (s)", color: "#9a9a9a" },
            ticks: { color: "#9a9a9a", maxTicksLimit: 8 },
            grid: { color: "#2e2e2e" },
          },
          y: {
            position: "left",
            title: { display: true, text: "位置", color: "#9a9a9a" },
            ticks: { color: "#9a9a9a" },
            grid: { color: "#2e2e2e" },
          },
          y1: {
            type: "linear",
            position: "right",
            title: { display: true, text: "误差", color: "#c89ef0" },
            ticks: { color: "#c4a8e0" },
            grid: { drawOnChartArea: false },
          },
        },
        plugins: {
          legend: { labels: { color: "#e0e0e0" } },
        },
      },
    });
  };

  const setChartWindow = (tNow) => {
    if (!chart) return;
    chart.options.scales.x.min = tNow - WINDOW_S;
    chart.options.scales.x.max = tNow;
  };

  const updateChartSerialEmpty = () => {
    ensureChart();
    bufTarget.length = 0;
    bufActual.length = 0;
    bufError.length = 0;
    syncChartBuffers();
    chart.options.scales.x.min = undefined;
    chart.options.scales.x.max = undefined;
    chart.update();
    updateChartStats();
  };

  const simReset = () => {
    simT = 0;
    simY = 0;
    simV = 0;
    simLastVCmd = 0;
    simInt = 0;
    simPrevE = null;
    bufTarget.length = 0;
    bufActual.length = 0;
    bufError.length = 0;
  };

  const resetRoundSampler = () => {
    tuningState.roundStartT = null;
    tuningState.sampleLastT = null;
    tuningState.samples = [];
  };

  const resetTuningRuntime = () => {
    tuningState.running = false;
    tuningState.roundIndex = 0;
    tuningState.busy = false;
    tuningState.finished = false;
    resetRoundSampler();
  };

  /**
   * 固定步长积分一步（100Hz 调用）。
   * 外环：位置误差 e=r-y → PID 得到速度指令 v_cmd。
   * 内环：实际速度 v 一阶跟上 v_cmd；位置 y 由 v 积分。
   */
  const simStep = (dt) => {
    const wave = $("vp-wave").value === "triangle" ? "triangle" : "sine";
    const period = readFloat("vp-period", 4);
    const amp = readFloat("vp-amp", 10);
    const offset = readFloat("vp-offset", 50);
    const P = readFloat("vp-p", 0.8);
    const I = readFloat("vp-i", 0);
    const D = readFloat("vp-d", 0);

    const r = targetValue(simT, wave, period, amp, offset);
    const e = r - simY;
    simInt += e * dt;
    const dedt = simPrevE === null || dt < 1e-12 ? 0 : (e - simPrevE) / dt;
    simPrevE = e;

    const vCmd = P * e + I * simInt + D * dedt;
    simLastVCmd = vCmd;

    simV += ((vCmd - simV) / TAU_V) * dt;
    simY += simV * dt;

    const ePlot = r - simY;
    bufTarget.push({ x: simT, y: r });
    bufActual.push({ x: simT, y: simY });
    bufError.push({ x: simT, y: ePlot });
    trimBuffers(simT);
    simT += dt;
  };

  const setPidInputs = (pid) => {
    if (!pid || typeof pid !== "object") return;
    if (Number.isFinite(pid.p)) $("vp-p").value = String(pid.p);
    if (Number.isFinite(pid.i)) $("vp-i").value = String(pid.i);
    if (Number.isFinite(pid.d)) $("vp-d").value = String(pid.d);
  };

  const buildSampleRow = () => {
    const nowMs = simT * 1000;
    const setpoint = bufTarget.length ? bufTarget[bufTarget.length - 1].y : 0;
    const input = simY;
    const error = setpoint - input;
    return {
      timestamp: nowMs,
      setpoint,
      input,
      // 与当前虚拟 PID 的速度状态保持一致。
      pwm: simV,
      error,
      p: readFloat("vp-p", 0.8),
      i: readFloat("vp-i", 0),
      d: readFloat("vp-d", 0),
    };
  };

  const buildHistoryText = () => {
    const recent = tuningState.historyRounds.slice(-3);
    if (!recent.length) return null;
    const lines = [];
    for (const item of recent) {
      const pid = item.pid || {};
      lines.push(
        `Round ${item.round}: PID(P=${Number(pid.p || 0).toFixed(6)}, I=${Number(pid.i || 0).toFixed(6)}, D=${Number(pid.d || 0).toFixed(6)})`
      );
      if (item.summary) lines.push(`Summary: ${item.summary}`);
    }
    return lines.join("\n");
  };

  const runTuningRound = async (samples, roundIndex) => {
    const payload = {
      round_index: roundIndex,
      samples,
      history_text: buildHistoryText(),
      plant_profile: "virtual_tracking",
    };
    return api("/api/debug/virtual-round", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  };

  const markRoundDone = () => {
    tuningState.roundIndex += 1;
    resetRoundSampler();
    if (tuningState.roundIndex >= tuningTotalRounds) {
      tuningState.running = false;
      tuningState.finished = true;
      $("vp-status").textContent = `运行中 · 调参完成（${tuningTotalRounds} 轮）`;
    }
  };

  const tickTuning = async () => {
    if (!tuningState.effectiveEnabled || mode !== "virtual") return;
    if (tuningState.finished) return;
    if (!simTimerId || tuningState.busy) return;
    if (tuningState.roundIndex >= tuningTotalRounds) {
      tuningState.running = false;
      tuningState.finished = true;
      return;
    }
    if (!tuningState.running) {
      tuningState.running = true;
      resetRoundSampler();
    }
    if (tuningState.roundStartT === null) tuningState.roundStartT = simT;
    if (tuningState.sampleLastT === null) tuningState.sampleLastT = simT - tuningSampleIntervalS;

    if (simT - tuningState.sampleLastT >= tuningSampleIntervalS) {
      tuningState.samples.push(buildSampleRow());
      tuningState.sampleLastT = simT;
    }

    const elapsed = simT - tuningState.roundStartT;
    if (elapsed < tuningRoundDurationS) return;

    const thisRound = tuningState.roundIndex + 1;
    tuningState.busy = true;
    const t0 = performance.now();
    try {
      const resp = await runTuningRound(tuningState.samples, thisRound);
      const elapsedMs = performance.now() - t0;
      pushPage("prompt", String(resp.prompt_text || ""), elapsedMs);
      pushPage("response", String(resp.response_text || ""));
      if (resp && resp.parsed_pid) setPidInputs(resp.parsed_pid);
      tuningState.historyRounds.push({
        round: thisRound,
        pid: resp.parsed_pid || {},
        summary: String(resp.raw_response_text || "").replace(/\s+/g, " ").slice(0, 240),
      });
      markRoundDone();
    } catch (e) {
      const elapsedMs = performance.now() - t0;
      pushPage("prompt", "", elapsedMs);
      pushPage("response", `第 ${thisRound} 轮失败：${e.message}`);
      markRoundDone();
    } finally {
      tuningState.busy = false;
    }
  };

  const tickVirtual = () => {
    simStep(SIM_DT);
    void tickTuning();
    ensureChart();
    syncChartBuffers();
    setChartWindow(simT);
    chart.update();
    updateChartStats();
    const roundInfo = tuningState.effectiveEnabled
      ? ` · 调参轮次 ${Math.min(tuningState.roundIndex + 1, tuningTotalRounds)}/${tuningTotalRounds}`
      : "";
    $("vp-status").textContent = `运行中 · t=${simT.toFixed(2)}s · y=${simY.toFixed(2)} · v=${simV.toFixed(2)}${roundInfo}`;
    $("chart-hint").textContent = `虚拟 PID · 三曲线 · 双纵轴 · 100Hz · Δt=${SIM_DT}s · 窗 ${WINDOW_S}s`;
  };

  const stopVirtualLoop = () => {
    if (simTimerId) clearInterval(simTimerId);
    simTimerId = 0;
    const startBtn = $("btn-vp-start");
    if (startBtn) startBtn.disabled = false;
  };

  const setSerialStatus = (text) => {
    const el = $("serial-status");
    if (el) el.textContent = text;
  };

  const closeSerialWebSocket = () => {
    if (serialWs) {
      try {
        serialWs.close();
      } catch {
        /* noop */
      }
      serialWs = null;
    }
  };

  const stopSerialSession = async () => {
    closeSerialWebSocket();
    serialConnected = false;
    serialIngestPaused = false;
    serialAwaitingSample = false;
    const pBtn = $("btn-serial-pause");
    if (pBtn) pBtn.textContent = "暂停";
    try {
      await api("/api/serial/disconnect", { method: "POST" });
    } catch {
      /* noop */
    }
  };

  const openSerialWebSocket = () => {
    closeSerialWebSocket();
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${window.location.host}/api/serial/stream`;
    const ws = new WebSocket(url);
    serialWs = ws;
    ws.onmessage = (ev) => {
      handleSerialWsMessage(ev);
    };
    ws.onerror = () => {
      if (mode === "serial") setSerialStatus("WebSocket 错误（曲线可能无法更新）");
    };
  };

  const handleSerialWsMessage = (ev) => {
    if (mode !== "serial") return;
    let data;
    try {
      data = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (data.type === "sample") {
      if (serialIngestPaused) return;
      const t = Number(data.t);
      const setpoint = Number(data.setpoint);
      const input = Number(data.input);
      const err = Number(data.error);
      if (![t, setpoint, input, err].every((x) => Number.isFinite(x))) return;
      bufTarget.push({ x: t, y: setpoint });
      bufActual.push({ x: t, y: input });
      bufError.push({ x: t, y: err });
      trimBuffers(t);
      ensureChart();
      syncChartBuffers();
      setChartWindow(t);
      chart.update();
      updateChartStats();
      if (serialAwaitingSample) {
        serialAwaitingSample = false;
      }
      $("chart-hint").textContent = `串口 · 三曲线 · 双纵轴 · 窗 ${WINDOW_S}s`;
      setSerialStatus(
        `运行中 · t=${t.toFixed(3)}s · setpoint=${setpoint.toFixed(3)} · input=${input.toFixed(3)} · error=${err.toFixed(3)}`
      );
    } else if (data.type === "parse_error") {
      setSerialStatus("数据解析失败");
    } else if (data.type === "io_error") {
      setSerialStatus(String(data.message || "串口读取异常"));
    }
  };

  const scheduleSerialPidSend = () => {
    if (!serialConnected || mode !== "serial") return;
    if (spPidTimer) window.clearTimeout(spPidTimer);
    spPidTimer = window.setTimeout(async () => {
      spPidTimer = 0;
      const p = readFloat("sp-p", 0);
      const i = readFloat("sp-i", 0);
      const d = readFloat("sp-d", 0);
      const line = `set_pid[${p},${i},${d}]`;
      try {
        await api("/api/serial/send", {
          method: "POST",
          body: JSON.stringify({ line }),
        });
      } catch {
        /* 静默失败，避免打断调参 */
      }
    }, 280);
  };

  const loadPorts = async () => {
    const data = await api("/api/serial/ports");
    const dl = $("serial-port-list");
    if (!dl) return;
    dl.innerHTML = "";
    const ports = (data && data.ports) || [];
    for (const p of ports) {
      const opt = document.createElement("option");
      opt.value = p;
      dl.appendChild(opt);
    }
  };

  const pullLlm = async () => {
    const d = await api("/api/debug/llm");
    clearPagedLogs();
    if (d.prompt) pushPage("prompt", d.prompt);
    if (d.response) pushPage("response", d.response);
  };

  const applyModeUI = () => {
    const isVirtual = mode === "virtual";
    $("panel-virtual").classList.toggle("hidden", !isVirtual);
    $("panel-serial").classList.toggle("hidden", isVirtual);
    $("btn-mode-toggle").textContent = isVirtual ? "模式：虚拟 PID" : "模式：串口";
    setReadonlyTextareas(true);
    if (isVirtual) {
      $("chart-hint").textContent = `虚拟 PID · 三曲线 · 双纵轴 · 窗 ${WINDOW_S}s`;
      updateTuningEffectiveLabel();
    } else {
      $("chart-hint").textContent = `串口模式 · 曲线窗 ${WINDOW_S}s（连接并启动后显示）`;
    }
  };

  const fullResetForModeSwitch = () => {
    if (mode === "virtual") {
      void stopSerialSession();
      setSerialStatus("未连接");
    }
    stopVirtualLoop();
    simReset();
    resetTuningRuntime();
    $("vp-status").textContent = "已停止";
    clearPagedLogs();
    if (mode === "serial") {
      updateChartSerialEmpty();
      setSerialStatus(serialConnected ? "已连接，点击「启动」下发指令" : "未连接");
    } else {
      ensureChart();
      syncChartBuffers();
      chart.options.scales.x.min = 0;
      chart.options.scales.x.max = WINDOW_S;
      chart.update();
      updateChartStats();
    }
  };

  const toggleMode = () => {
    mode = mode === "virtual" ? "serial" : "virtual";
    fullResetForModeSwitch();
    applyModeUI();
  };

  const init = async () => {
    ensureChart();
    simReset();
    clearPagedLogs();
    resetTuningRuntime();
    syncChartBuffers();
    chart.options.scales.x.min = 0;
    chart.options.scales.x.max = WINDOW_S;
    chart.update();
    updateChartStats();
    $("chart-hint").textContent = `虚拟 PID · 三曲线 · 双纵轴 · 窗 ${WINDOW_S}s`;
    tuningState.pendingEnabled = Boolean($("vp-enable-tuning").checked);
    tuningState.effectiveEnabled = false;
    applyModeUI();

    try {
      const d = await api("/api/tuning/ui-settings");
      if (d && typeof d === "object") {
        const r = Number(d.rounds);
        if (Number.isFinite(r)) tuningTotalRounds = Math.max(1, Math.floor(r));
        const dur = Number(d.round_duration_seconds);
        if (Number.isFinite(dur) && dur > 0) tuningRoundDurationS = dur;
        const iv = Number(d.sample_interval_seconds);
        if (Number.isFinite(iv) && iv > 0) tuningSampleIntervalS = iv;
      }
    } catch {
      /* 使用与 config 缺省一致的页内默认值 */
    }

    $("btn-mode-toggle").addEventListener("click", () => toggleMode());
    $("vp-enable-tuning").addEventListener("change", () => {
      tuningState.pendingEnabled = Boolean($("vp-enable-tuning").checked);
      updateTuningEffectiveLabel();
    });

    $("btn-vp-start").addEventListener("click", () => {
      if (mode !== "virtual") return;
      if (simTimerId) return;
      $("vp-status").textContent = "运行中";
      simTimerId = setInterval(tickVirtual, Math.round(1000 * SIM_DT));
      const startBtn = $("btn-vp-start");
      if (startBtn) startBtn.disabled = true;
    });

    $("btn-vp-pause").addEventListener("click", () => {
      stopVirtualLoop();
      $("vp-status").textContent = "已暂停";
    });

    $("btn-vp-restart").addEventListener("click", () => {
      stopVirtualLoop();
      simReset();
      resetTuningRuntime();
      clearPagedLogs();
      tuningState.effectiveEnabled = tuningState.pendingEnabled;
      updateTuningEffectiveLabel();
      $("vp-status").textContent = "已停止";
      ensureChart();
      syncChartBuffers();
      chart.options.scales.x.min = 0;
      chart.options.scales.x.max = WINDOW_S;
      chart.update();
      updateChartStats();
    });

    $("btn-serial-refresh").addEventListener("click", async () => {
      try {
        await loadPorts();
        if (mode === "serial") setSerialStatus("端口列表已刷新");
      } catch (e) {
        if (mode === "serial") setSerialStatus(`刷新失败：${e.message}`);
      }
    });

    $("btn-serial-connect").addEventListener("click", async () => {
      const port = ($("serial-port-input") && $("serial-port-input").value) || "";
      const baudrate = parseInt(String($("serial-baud").value || "1000000"), 10) || 1000000;
      try {
        const r = await api("/api/serial/connect", {
          method: "POST",
          body: JSON.stringify({ port, baudrate }),
        });
        if (r && r.status === "ok") {
          serialConnected = true;
          serialIngestPaused = false;
          serialAwaitingSample = false;
          openSerialWebSocket();
          setSerialStatus("已连接，点击「启动」下发调试指令");
        } else {
          serialConnected = false;
          setSerialStatus(String((r && r.message) || "连接失败"));
        }
      } catch (e) {
        serialConnected = false;
        setSerialStatus(`连接失败：${e.message}`);
      }
    });

    $("btn-serial-disconnect").addEventListener("click", async () => {
      await stopSerialSession();
      setSerialStatus("未连接");
    });

    $("btn-serial-start").addEventListener("click", async () => {
      if (mode !== "serial") return;
      if (!serialConnected) {
        setSerialStatus("请先连接串口");
        return;
      }
      const period = readFloat("sp-period", 4);
      const amp = readFloat("sp-amp", 10);
      const off = readFloat("sp-offset", 50);
      const line = `debug_pid_ai_tuning start ${period} ${amp} ${off}`;
      try {
        await api("/api/serial/send", {
          method: "POST",
          body: JSON.stringify({ line }),
        });
        bufTarget.length = 0;
        bufActual.length = 0;
        bufError.length = 0;
        serialAwaitingSample = true;
        ensureChart();
        syncChartBuffers();
        chart.options.scales.x.min = undefined;
        chart.options.scales.x.max = undefined;
        chart.update();
        updateChartStats();
        setSerialStatus("没有收到数据");
      } catch (e) {
        setSerialStatus(`启动失败：${e.message}`);
      }
    });

    $("btn-serial-pause").addEventListener("click", async () => {
      if (mode !== "serial" || !serialConnected) return;
      const next = !serialIngestPaused;
      try {
        await api("/api/serial/ingest-pause", {
          method: "POST",
          body: JSON.stringify({ paused: next }),
        });
        serialIngestPaused = next;
        $("btn-serial-pause").textContent = serialIngestPaused ? "继续" : "暂停";
        setSerialStatus(
          serialIngestPaused ? "已暂停（本机不再解析与刷新曲线；下位机仍在运行）" : "已继续接收与绘图"
        );
      } catch (e) {
        setSerialStatus(`暂停切换失败：${e.message}`);
      }
    });

    $("btn-serial-close").addEventListener("click", async () => {
      if (mode !== "serial" || !serialConnected) {
        setSerialStatus("请先连接串口");
        return;
      }
      try {
        await api("/api/serial/send", {
          method: "POST",
          body: JSON.stringify({ line: "debug_pid_ai_tuning stop" }),
        });
        setSerialStatus("已下发关闭指令（下位机应停止上报）");
      } catch (e) {
        setSerialStatus(`关闭指令失败：${e.message}`);
      }
    });

    ["sp-p", "sp-i", "sp-d"].forEach((id) => {
      const el = $(id);
      if (el) el.addEventListener("change", () => scheduleSerialPidSend());
    });

    $("btn-llm-pull").addEventListener("click", async () => {
      try {
        await pullLlm();
      } catch (e) {
        pushPage("prompt", `拉取失败：${e.message}`);
      }
    });

    $("btn-prompt-prev").addEventListener("click", () => {
      setPageIndex("prompt", tuningState.promptPageIndex - 1);
    });
    $("btn-prompt-next").addEventListener("click", () => {
      setPageIndex("prompt", tuningState.promptPageIndex + 1);
    });
    $("btn-prompt-jump").addEventListener("click", () => {
      const page = readInt("prompt-page-jump", 1);
      setPageIndex("prompt", page - 1);
    });
    $("btn-response-prev").addEventListener("click", () => {
      setPageIndex("response", tuningState.responsePageIndex - 1);
    });
    $("btn-response-next").addEventListener("click", () => {
      setPageIndex("response", tuningState.responsePageIndex + 1);
    });
    $("btn-response-jump").addEventListener("click", () => {
      const page = readInt("response-page-jump", 1);
      setPageIndex("response", page - 1);
    });

    await loadPorts();
    await pullLlm();
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
