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

  const appendSerialLog = (line) => {
    const el = $("serial-log");
    if (!el) return;
    el.textContent += `${line}\n`;
    el.scrollTop = el.scrollHeight;
  };

  const loadPorts = async () => {
    const data = await api("/api/serial/ports");
    const sel = $("serial-ports");
    sel.innerHTML = "";
    const ports = (data && data.ports) || [];
    if (!ports.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "（无端口 · 占位）";
      sel.appendChild(opt);
      return;
    }
    for (const p of ports) {
      const opt = document.createElement("option");
      opt.value = p;
      opt.textContent = p;
      sel.appendChild(opt);
    }
  };

  const refreshSerialStatus = async () => {
    try {
      const s = await api("/api/serial/status");
      appendSerialLog(`[status] ${s.status}: ${s.message}`);
    } catch (e) {
      appendSerialLog(`[status] ${e.message}`);
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
      $("chart-hint").textContent = "串口模式：暂无曲线数据（占位）";
    }
  };

  const fullResetForModeSwitch = () => {
    stopVirtualLoop();
    simReset();
    resetTuningRuntime();
    $("vp-status").textContent = "已停止";
    $("serial-log").textContent = "";
    clearPagedLogs();
    if (mode === "serial") {
      updateChartSerialEmpty();
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
        appendSerialLog("[refresh] 已刷新端口列表");
      } catch (e) {
        appendSerialLog(`[refresh] ${e.message}`);
      }
    });

    $("btn-serial-connect").addEventListener("click", async () => {
      const port = $("serial-ports").value || "";
      const baudrate = parseInt(String($("serial-baud").value || "115200"), 10) || 115200;
      try {
        const r = await api("/api/serial/connect", {
          method: "POST",
          body: JSON.stringify({ port, baudrate }),
        });
        appendSerialLog(`[connect] ${JSON.stringify(r)}`);
      } catch (e) {
        appendSerialLog(`[connect] ${e.message}`);
      }
    });

    $("btn-serial-disconnect").addEventListener("click", async () => {
      try {
        const r = await api("/api/serial/disconnect", { method: "POST" });
        appendSerialLog(`[disconnect] ${JSON.stringify(r)}`);
      } catch (e) {
        appendSerialLog(`[disconnect] ${e.message}`);
      }
    });

    $("btn-serial-send").addEventListener("click", async () => {
      const line = $("serial-send-line").value || "";
      try {
        await api("/api/serial/send", {
          method: "POST",
          body: JSON.stringify({ line }),
        });
        appendSerialLog("[send] 意外成功");
      } catch (e) {
        appendSerialLog(`[send] ${e.status || ""} ${e.message}`);
      }
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
