"""从单轮样本列表计算指标并根据 JSON 模板生成本轮数据正文。"""

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


def _cfg(config: Mapping[str, Any], path: str) -> Any:
    node: Any = config
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            raise KeyError(f"missing prompt config key: {path}")
        node = node[part]
    return node


def _fmt(template: str, **kwargs: Any) -> str:
    return template.format(**{k: str(v) for k, v in kwargs.items()})


def build_prompt_data(
    samples: List[Mapping[str, Any]],
    config: Mapping[str, Any],
    *,
    plant_profile: str | None = None,
) -> str:
    """生成本轮「Current Status + 时间序列摘要」正文。"""
    if not samples:
        return ""

    metrics = compute_metrics(samples)
    setpoint = float(metrics.get("setpoint", _f(samples[-1], "setpoint")))
    current_pid = _current_pid_from_samples(samples)
    reference_tv = bool(metrics.get("reference_time_varying"))

    all_data = list(samples)
    step = max(1, len(all_data) // 30)
    sampled_data = all_data[::step]

    pd = _cfg(config, "user.prompt_data")
    lines: List[str] = [str(pd["section_current_status"])]

    if reference_tv:
        lines.append(
            _fmt(
                str(pd["line_setpoint_dynamic_range"]),
                setpoint_min=f"{metrics.get('setpoint_min', 0):.4g}",
                setpoint_max=f"{metrics.get('setpoint_max', 0):.4g}",
                setpoint_span=f"{metrics.get('setpoint_span', 0):.4g}",
            )
        )
        lines.append(
            _fmt(str(pd["line_setpoint_dynamic_last"]), setpoint_last=f"{setpoint:.4g}")
        )
    else:
        lines.append(_fmt(str(pd["line_setpoint_static"]), setpoint=f"{setpoint}"))

    lines.append(
        _fmt(
            str(pd["line_pid"]),
            p=current_pid["p"],
            i=current_pid["i"],
            d=current_pid["d"],
        )
    )
    lines.append(_fmt(str(pd["line_mae"]), avg_error=f"{metrics.get('avg_error', 0):.4f}"))
    lines.append(_fmt(str(pd["line_max_error"]), max_error=f"{metrics.get('max_error', 0):.4f}"))
    lines.append(
        _fmt(str(pd["line_rms"]), rms_tracking_error=f"{metrics.get('rms_tracking_error', 0):.4f}")
    )

    if reference_tv:
        lines.append(
            _fmt(
                str(pd["line_peak_overshoot_dynamic"]),
                tracking_peak_overshoot_instant_pct=(
                    f"{metrics.get('tracking_peak_overshoot_instant_pct', 0):.2f}"
                ),
            )
        )
        lines.append(str(pd["line_overshoot_note_dynamic"]))

        lines.append(str(pd["line_zero_cross_note_dynamic"]))
        lines.append(
            _fmt(
                str(pd["line_zero_cross_count_dynamic"]),
                zero_crossings=metrics.get("zero_crossings", 0),
            )
        )
    else:
        lines.append(_fmt(str(pd["line_overshoot_static"]), overshoot=f"{metrics.get('overshoot', 0):.1f}"))
        lines.append(
            _fmt(
                str(pd["line_oscillation_static"]),
                zero_crossings=metrics.get("zero_crossings", 0),
                status=metrics.get("status", "UNKNOWN"),
            )
        )

    lines.append(
        _fmt(
            str(pd["line_steady_state_error"]),
            steady_state_error=f"{metrics.get('steady_state_error', 0):.4f}",
        )
    )
    lines.append(_fmt(str(pd["line_status"]), status=metrics.get("status", "UNKNOWN")))

    if plant_profile == "virtual_tracking" or reference_tv:
        lines.append("")
        lines.append(str(pd["time_varying_note_title"]))
        for bullet in list(pd["time_varying_note_bullets"]):
            lines.append(str(bullet))

    lines.append("")
    lines.append(_fmt(str(pd["timeseries_title"]), sample_count=len(sampled_data)))
    lines.append(str(pd["timeseries_header"]))

    for d in sampled_data:
        lines.append(
            f"{_f(d, 'timestamp'):.0f}, {_f(d, 'input'):.2f}, {_f(d, 'pwm'):.1f}, {_f(d, 'error'):.2f}"
        )

    return "\n".join(lines)


__all__ = ["compute_metrics", "build_prompt_data"]
