"""从单轮样本列表计算指标并生成与参考工程一致的本轮数据正文（串口单环）。"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping


def _f(row: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    try:
        v = row.get(key, default)
        return float(v) if v is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def _rms(values: List[float]) -> float:
    if not values:
        return 0.0
    return math.sqrt(sum(x * x for x in values) / len(values))


def compute_metrics(samples: List[Mapping[str, Any]]) -> Dict[str, Any]:
    """对齐 reference `AdvancedDataBuffer.calculate_advanced_metrics`，并扩展时变给定语义。"""
    if not samples:
        return {}

    data = list(samples)
    inputs = [_f(d, "input") for d in data]
    setpoints = [_f(d, "setpoint") for d in data]
    errors = [sp - inp for sp, inp in zip(setpoints, inputs)]
    abs_errors = [abs(e) for e in errors]

    avg_error = sum(abs_errors) / len(abs_errors) if abs_errors else 0.0
    max_error = max(abs_errors) if abs_errors else 0.0
    rms_tracking_error = _rms(errors)

    sp_min = min(setpoints) if setpoints else 0.0
    sp_max = max(setpoints) if setpoints else 0.0
    sp_span = sp_max - sp_min
    sp_mid = (sp_min + sp_max) / 2.0 if setpoints else 0.0
    rel_scale = max(abs(sp_mid), abs(sp_min), abs(sp_max), 1.0)
    reference_time_varying = bool(sp_span > max(1e-3, 5e-3 * rel_scale))

    setpoint_last = _f(data[-1], "setpoint")
    max_input = max(inputs) if inputs else 0.0

    overshoot = 0.0
    if not reference_time_varying:
        if max_input > setpoint_last and setpoint_last != 0:
            overshoot = ((max_input - setpoint_last) / abs(setpoint_last)) * 100.0

    peak_overshoot_instant_pct = 0.0
    for spv, inp in zip(setpoints, inputs):
        denom = abs(spv)
        if denom < 1e-9:
            continue
        if inp > spv:
            peak_overshoot_instant_pct = max(
                peak_overshoot_instant_pct, ((inp - spv) / denom) * 100.0
            )

    steady_state_len = max(1, int(len(data) * 0.2))
    steady_state_error = sum(abs_errors[-steady_state_len:]) / steady_state_len

    zero_crossings = 0
    for i in range(1, len(errors)):
        if (errors[i - 1] > 0 and errors[i] < 0) or (errors[i - 1] < 0 and errors[i] > 0):
            zero_crossings += 1

    if reference_time_varying:
        margin = max(1e-6, 0.12 * max(sp_span, 1e-6))
        if avg_error > 2.0 * margin:
            status = "TRACKING_LARGE_ERROR"
        elif avg_error > margin:
            status = "TRACKING_MODERATE"
        elif peak_overshoot_instant_pct > 5.0:
            status = "TRACKING_OVERSHOOTING_VS_INSTANT_SP"
        else:
            status = "TRACKING_SMALL_ERROR"
    else:
        status = "STABLE"
        if zero_crossings > len(data) * 0.3:
            status = "OSCILLATING"
        elif overshoot > 5.0:
            status = "OVERSHOOTING"
        elif avg_error > 10.0 and steady_state_error > 5.0:
            status = "SLOW_RESPONSE"

    return {
        "avg_error": avg_error,
        "max_error": max_error,
        "overshoot": overshoot,
        "steady_state_error": steady_state_error,
        "zero_crossings": zero_crossings,
        "status": status,
        "setpoint": setpoint_last,
        "reference_time_varying": reference_time_varying,
        "setpoint_min": sp_min,
        "setpoint_max": sp_max,
        "setpoint_span": sp_span,
        "rms_tracking_error": rms_tracking_error,
        "tracking_peak_overshoot_instant_pct": peak_overshoot_instant_pct,
    }


def _current_pid_from_samples(samples: List[Mapping[str, Any]]) -> Dict[str, float]:
    if not samples:
        return {"p": 0.0, "i": 0.0, "d": 0.0}
    last = samples[-1]
    return {
        "p": _f(last, "p", 1.0),
        "i": _f(last, "i", 0.1),
        "d": _f(last, "d", 0.05),
    }


def build_prompt_data(
    samples: List[Mapping[str, Any]],
    *,
    plant_profile: str | None = None,
) -> str:
    """
    生成本轮「Current Status + 时间序列摘要」正文。
    plant_profile 若为 virtual_tracking，会附加一段「时变给定」释意（与 metrics 一并呈现）。
    """
    if not samples:
        return ""

    metrics = compute_metrics(samples)
    setpoint = float(metrics.get("setpoint", _f(samples[-1], "setpoint")))
    current_pid = _current_pid_from_samples(samples)
    reference_tv = bool(metrics.get("reference_time_varying"))

    all_data = list(samples)
    step = max(1, len(all_data) // 30)
    sampled_data = all_data[::step]

    lines: List[str] = []
    lines.append("## Current Status")

    if reference_tv:
        lines.append(
            f"- 设定值轨迹 r(t): 时变 · 区间内 min={metrics.get('setpoint_min', 0):.4g}, "
            f"max={metrics.get('setpoint_max', 0):.4g}, span≈{metrics.get('setpoint_span', 0):.4g}"
        )
        lines.append(
            f"- 窗口末刻 r(t_last): {setpoint:.4g} （勿将整条窗口误判为恒定阶跃给定）"
        )
    else:
        lines.append(f"- 设定值 (Setpoint): {setpoint}")

    lines.append(
        f"- 当前 PID: P={current_pid['p']}, I={current_pid['i']}, D={current_pid['d']}"
    )
    lines.append(f"- 平均绝对误差 MAE(|e|): {metrics.get('avg_error', 0):.4f}")
    lines.append(f"- 最大绝对误差: {metrics.get('max_error', 0):.4f}")
    lines.append(f"- RMS(跟踪误差 e=r-y): {metrics.get('rms_tracking_error', 0):.4f}")

    if reference_tv:
        lines.append(
            f"- 瞬时相对超调峰: max_t max(0,(y−r)/|r|)·100 ≈ "
            f"{metrics.get('tracking_peak_overshoot_instant_pct', 0):.2f}%"
        )
        lines.append("- 定点阶跃语义超调%: （给定随时间变化，此项不作为主判据，已弱化）")

        lines.append("- 误差过零点次数（仅供参考，给定随时间变化时勿单独推断振荡）:")
        lines.append(f"  count={metrics.get('zero_crossings', 0)}")
    else:
        lines.append(f"- 超调量(近似恒定给定): {metrics.get('overshoot', 0):.1f}%")
        lines.append(
            f"- 震荡检测: 过零点 {metrics.get('zero_crossings', 0)} 次 "
            f"(状态: {metrics.get('status', 'UNKNOWN')})"
        )

    lines.append(f"- 稳态误差估算(末段20%均值): {metrics.get('steady_state_error', 0):.4f}")
    lines.append(f"- 状态摘要: {metrics.get('status', 'UNKNOWN')}")

    if plant_profile == "virtual_tracking" or reference_tv:
        lines.append("")
        lines.append(
            "## 给定模式说明（自动）\n"
            "- 本窗口数据表明给定 r(t) 随时间显著变化。调参目标是跟踪波形而非单点爬坡。\n"
            "- 禁止仅用「看起来像正弦振荡」推断临界增益自持振荡；需结合能否跟上 r(t) 的 RMS/相位滞后。\n"
            "- Phase 仍可分 P→I→D，但请以跟踪 RMS 下降 / 瞬时超调可控为主要优化信号。"
        )

    lines.append("")
    lines.append(f"## 时间序列数据摘要 (采样 {len(sampled_data)} 点):")
    lines.append("SimTime(ms), Input, PWM, Error")

    for d in sampled_data:
        lines.append(
            f"{_f(d, 'timestamp'):.0f}, {_f(d, 'input'):.2f}, {_f(d, 'pwm'):.1f}, {_f(d, 'error'):.2f}"
        )

    return "\n".join(lines)


__all__ = ["compute_metrics", "build_prompt_data"]
