"""串口单环 PID 调参：默认提示词与 prompt 拼接。"""

from typing import Any

_EXPORT_NAMES = (
    "append_capture_to_output2_log",
    "build_full_prompt",
    "build_system",
    "build_user",
    "build_prompt_data",
    "clear_prompt_config_cache",
    "compute_metrics",
    "extract_json_candidates",
    "finalize_llm_output",
    "format_model_context",
    "load_prompt_config",
    "parse_json_response",
    "parse_structured_text_response",
    "request_once",
    "run_one_shot_ai_pipeline",
    "sanitize_result",
    "write_multi_round_prompt_log",
)


def __getattr__(name: str) -> Any:
    if name in _EXPORT_NAMES:
        from . import prompt_concat as _pc
        from . import metrics as _m
        from . import response_parser as _rp
        from . import client as _c

        if name in ("build_prompt_data", "compute_metrics"):
            return getattr(_m, name)
        if name == "request_once":
            return getattr(_c, name)
        if name in (
            "append_capture_to_output2_log",
            "extract_json_candidates",
            "finalize_llm_output",
            "parse_json_response",
            "parse_structured_text_response",
            "sanitize_result",
        ):
            return getattr(_rp, name)
        return getattr(_pc, name)
    raise AttributeError(name)


__all__ = list(_EXPORT_NAMES)
