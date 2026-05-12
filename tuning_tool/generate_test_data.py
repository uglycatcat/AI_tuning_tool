#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成多轮串口单环 PID 测试数据，写入 test_data.json。

每轮包含：
- samples：仿串口 CSV 解析后的字典列表（timestamp, setpoint, input, pwm, error, p, i, d）
- history_text：进入该轮前累积的调参历史正文（第 1 轮为 null，与真实流程一致）

运行（仓库根目录）:
  python3 -m tuning_tool.generate_test_data
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from tuning_tool.metrics import compute_metrics

_MAX_THOUGHT = 500
_MAX_ANALYSIS = 500


def _format_tuning_history(records: List[Dict[str, Any]]) -> str:
    """对齐 reference `TuningHistory.to_prompt_text` 格式（用于写入下一轮 history_text）。"""
    if not records:
        return ""
    text = "## 调参历史 (最近几轮):\n\n"
    for rec in records:
        m = rec["metrics"]
        pid = rec["pid"]
        r = int(rec["round"])
        text += f"### Round {r}\n"
        text += f"- **采用参数**: P={pid['p']:.4f}, I={pid['i']:.4f}, D={pid['d']:.4f}\n"
        text += (
            f"- **表现指标**: AvgErr={m.get('avg_error', 0):.2f}, MaxErr={m.get('max_error', 0):.2f}, "
            f"Overshoot={m.get('overshoot', 0):.1f}%, Status={m.get('status', 'UNKNOWN')}\n"
        )
        if rec.get("thought"):
            t = str(rec["thought"])
            if len(t) > _MAX_THOUGHT:
                t = t[:_MAX_THOUGHT].rstrip() + "..."
            text += f"- **AI思考过程**: {t}\n"
        if rec.get("analysis"):
            a = str(rec["analysis"])
            if len(a) > _MAX_ANALYSIS:
                a = a[:_MAX_ANALYSIS].rstrip() + "..."
            text += f"- **AI分析总结**: {a}\n"
        text += "\n"
    return text.rstrip() + "\n"


def _make_samples(
    *,
    setpoint: float,
    n: int,
    dt_ms: float,
    p: float,
    i: float,
    d: float,
    input_start: float,
    input_end: float,
    pwm_start: float,
    pwm_end: float,
    t0_ms: float = 0.0,
) -> List[Dict[str, float]]:
    out: List[Dict[str, float]] = []
    for k in range(n):
        t = t0_ms + k * dt_ms
        alpha = (n - 1) and (k / (n - 1)) or 0.0
        inp = input_start + (input_end - input_start) * alpha
        pwm = pwm_start + (pwm_end - pwm_start) * alpha
        err = setpoint - inp
        out.append(
            {
                "timestamp": float(t),
                "setpoint": float(setpoint),
                "input": float(inp),
                "pwm": float(pwm),
                "error": float(err),
                "p": float(p),
                "i": float(i),
                "d": float(d),
            }
        )
    return out


def build_fixture() -> Dict[str, Any]:
    """构造 3 轮：升温偏慢 → 改善 → 接近设定仍有稳态误差。"""
    serial_port = "/dev/ttyUSB_TEST"
    preference = "优先稳定性，不接受明显振荡；允许较慢收敛。"

    history_records: List[Dict[str, Any]] = []
    rounds: List[Dict[str, Any]] = []

    # Round 1: low gains, slow approach
    s1 = _make_samples(
        setpoint=100.0,
        n=90,
        dt_ms=50.0,
        p=1.0,
        i=0.05,
        d=0.0,
        input_start=22.0,
        input_end=55.0,
        pwm_start=120.0,
        pwm_end=280.0,
    )
    rounds.append(
        {
            "round_index": 1,
            "history_text": None,
            "samples": s1,
        }
    )
    pid1 = {"p": s1[-1]["p"], "i": s1[-1]["i"], "d": s1[-1]["d"]}
    m1 = compute_metrics(s1)
    history_records.append(
        {
            "round": 1,
            "pid": pid1,
            "metrics": m1,
            "thought": "第一轮：误差仍较大，响应偏慢；在硬件约束下先小幅提高 P，并略微增加 I 以压制稳态偏差。",
            "analysis": "从时间序列看 input 尚未贴近 setpoint，PWM 仍在上升段；建议保守上调 P，I 保持小步。",
        }
    )

    # Round 2: better gains
    s2 = _make_samples(
        setpoint=100.0,
        n=90,
        dt_ms=50.0,
        p=1.35,
        i=0.08,
        d=0.02,
        input_start=55.0,
        input_end=88.0,
        pwm_start=260.0,
        pwm_end=420.0,
        t0_ms=float(s1[-1]["timestamp"] + 50.0),
    )
    rounds.append(
        {
            "round_index": 2,
            "history_text": _format_tuning_history(history_records),
            "samples": s2,
        }
    )
    pid2 = {"p": s2[-1]["p"], "i": s2[-1]["i"], "d": s2[-1]["d"]}
    m2 = compute_metrics(s2)
    history_records.append(
        {
            "round": 2,
            "pid": pid2,
            "metrics": m2,
            "thought": "第二轮：跟踪明显改善，但末段仍有残余误差；可微增 I，并观察是否引入轻微振荡。",
            "analysis": "超调仍可控，AvgErr 下降；若继续加 I 需同步观察过零与 PWM 抖动。",
        }
    )

    # Round 3: near setpoint
    s3 = _make_samples(
        setpoint=100.0,
        n=90,
        dt_ms=50.0,
        p=1.35,
        i=0.12,
        d=0.03,
        input_start=88.0,
        input_end=97.5,
        pwm_start=400.0,
        pwm_end=480.0,
        t0_ms=float(s2[-1]["timestamp"] + 50.0),
    )
    rounds.append(
        {
            "round_index": 3,
            "history_text": _format_tuning_history(history_records),
            "samples": s3,
        }
    )

    return {
        "serial_port": serial_port,
        "user_preference_summary": preference,
        "prompt_context": None,
        "rounds": rounds,
    }


def main() -> None:
    root = Path(__file__).resolve().parent
    out_path = root / "test_data.json"
    data = build_fixture()
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out_path} ({len(data['rounds'])} rounds)")


if __name__ == "__main__":
    main()
