"""从单轮样本列表计算指标并生成与参考工程一致的本轮数据正文（串口单环）。"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping


def _f(row: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    try:
        v = row.get(key, default)
        return float(v) if v is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def compute_metrics(samples: List[Mapping[str, Any]]) -> Dict[str, Any]:
    """对齐 reference `AdvancedDataBuffer.calculate_advanced_metrics`。"""
    if not samples:
        return {}

    data = list(samples)
    inputs = [_f(d, "input") for d in data]
    errors = [_f(d, "setpoint") - _f(d, "input") for d in data]
    abs_errors = [abs(e) for e in errors]

    avg_error = sum(abs_errors) / len(abs_errors) if abs_errors else 0.0
    max_error = max(abs_errors) if abs_errors else 0.0

    setpoint = _f(data[-1], "setpoint") if data else 0.0
    max_input = max(inputs) if inputs else 0.0
    overshoot = 0.0
    if max_input > setpoint and setpoint != 0:
        overshoot = ((max_input - setpoint) / setpoint) * 100.0

    steady_state_len = max(1, int(len(data) * 0.2))
    steady_state_error = sum(abs_errors[-steady_state_len:]) / steady_state_len

    zero_crossings = 0
    for i in range(1, len(errors)):
        if (errors[i - 1] > 0 and errors[i] < 0) or (errors[i - 1] < 0 and errors[i] > 0):
            zero_crossings += 1

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
        "setpoint": setpoint,
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


def build_prompt_data(samples: List[Mapping[str, Any]], config: Mapping[str, Any]) -> str:
    """
    生成本轮「Current Status + 时间序列摘要」文本。
    config 需含 load_prompt_config 返回结构（当前实现未读 config 内文案，仅保持签名一致）。
    """
    del config  # 文案与参考硬编码一致；预留与 JSON 对齐的扩展点

    if not samples:
        return ""

    metrics = compute_metrics(samples)
    setpoint = float(metrics.get("setpoint", _f(samples[-1], "setpoint")))
    current_pid = _current_pid_from_samples(samples)

    all_data = list(samples)
    step = max(1, len(all_data) // 30)
    sampled_data = all_data[::step]

    lines: List[str] = []
    lines.append("## Current Status")
    lines.append(f"- 设定值 (Setpoint): {setpoint}")
    lines.append(
        f"- 当前 PID: P={current_pid['p']}, I={current_pid['i']}, D={current_pid['d']}"
    )
    lines.append(f"- 平均误差: {metrics.get('avg_error', 0):.2f}")
    lines.append(f"- 最大误差: {metrics.get('max_error', 0):.2f}")
    lines.append(f"- 超调量: {metrics.get('overshoot', 0):.1f}%")
    lines.append(f"- 稳态误差估算: {metrics.get('steady_state_error', 0):.2f}")
    lines.append(
        f"- 震荡检测: 过零点 {metrics.get('zero_crossings', 0)} 次 (状态: {metrics.get('status', 'UNKNOWN')})"
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
