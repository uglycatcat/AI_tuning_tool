from __future__ import annotations

from fastapi import APIRouter

from tuning_tool.llm_settings import (
    ai_tuning_reference_duration_seconds,
    ai_tuning_reference_sample_interval_seconds,
    ai_tuning_rounds,
    read_root_config,
)

router = APIRouter(tags=["tuning-ui"])


@router.get("/tuning/ui-settings")
def tuning_ui_settings() -> dict[str, float | int]:
    """供前端虚拟 AI 调参读取：每轮采样时长、采样间隔、询问轮次（来自项目根 config.json）。"""
    data = read_root_config()
    duration_s = ai_tuning_reference_duration_seconds(data)
    interval_s = ai_tuning_reference_sample_interval_seconds(data)
    rounds = ai_tuning_rounds(data)
    rate_hz = 1.0 / interval_s if interval_s > 0 else 0.0
    return {
        "round_duration_seconds": duration_s,
        "sample_interval_seconds": interval_s,
        "sample_rate_hz": round(rate_hz, 6),
        "rounds": rounds,
    }
