"""加载 default_prompt.json，并将样本 / 历史 / 上下文拼接为完整 system 与 user。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .metrics import build_prompt_data, compute_metrics
from .response_parser import append_capture_to_output2_log
from .prompt_display import format_prompt_messages

_CONFIG_CACHE: Dict[str, Any] | None = None
_CONFIG_PATH: str | None = None


def load_prompt_config(path: Optional[str | Path] = None) -> Dict[str, Any]:
    """默认加载与本文件同目录下的 default_prompt.json。"""
    global _CONFIG_CACHE, _CONFIG_PATH
    resolved = Path(path) if path else Path(__file__).resolve().parent / "default_prompt.json"
    key = str(resolved.resolve())
    if _CONFIG_CACHE is not None and _CONFIG_PATH == key:
        return _CONFIG_CACHE
    with open(resolved, encoding="utf-8") as f:
        _CONFIG_CACHE = json.load(f)
    _CONFIG_PATH = key
    return _CONFIG_CACHE


def clear_prompt_config_cache() -> None:
    global _CONFIG_CACHE, _CONFIG_PATH
    _CONFIG_CACHE = None
    _CONFIG_PATH = None


def _required_mapping(config: Mapping[str, Any], path: str) -> Mapping[str, Any]:
    node: Any = config
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            raise KeyError(f"missing prompt config key: {path}")
        node = node[part]
    if not isinstance(node, Mapping):
        raise TypeError(f"prompt config key is not mapping: {path}")
    return node


def _required_str(config: Mapping[str, Any], path: str) -> str:
    node: Any = config
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            raise KeyError(f"missing prompt config key: {path}")
        node = node[part]
    if not isinstance(node, str):
        raise TypeError(f"prompt config key is not string: {path}")
    text = node.strip()
    if not text:
        raise ValueError(f"prompt config key is empty: {path}")
    return text


def build_system(config: Mapping[str, Any], *, plant_profile: str = "hardware_step") -> str:
    sys_cfg = config.get("system") or {}
    suffix = str(sys_cfg.get("serial_mode_suffix", "")).strip()

    hw = str(sys_cfg.get("hardware_step_base") or "").strip()
    legacy = str(sys_cfg.get("base") or "").strip()
    vt = str(sys_cfg.get("virtual_tracking_base") or "").strip()

    if plant_profile == "virtual_tracking":
        core = vt or legacy or hw
        return core

    core_hw = hw or legacy or vt
    if not suffix:
        return core_hw
    if not core_hw:
        return suffix
    return f"{core_hw}\n\n{suffix}"


def resolve_plant_profile(prompt_context: Optional[Mapping[str, Any]]) -> str:
    pv = ""
    if prompt_context:
        pv = str(prompt_context.get("plant_profile") or "").strip().lower().replace("-", "_")
    if pv == "virtual_tracking":
        return "virtual_tracking"
    return "hardware_step"


def merge_prompt_context(
    config: Mapping[str, Any],
    prompt_context: Optional[Mapping[str, Any]],
    *,
    plant_profile: str,
) -> Dict[str, Any]:
    """合并默认 context、工况专属护栏与各轮显式上下文。"""
    ctx_cfg = config.get("context") or {}
    merged: Dict[str, Any] = dict(ctx_cfg.get("default_keys") or {})

    hints = ctx_cfg.get("guardrail_hints")
    gh: str | None = None
    if isinstance(hints, Mapping):
        raw = hints.get(plant_profile)
        if isinstance(raw, str):
            gh = raw.strip()

    if gh:
        merged["per_round_guardrail_hint"] = gh

    profiles = ctx_cfg.get("profile_defaults")
    if isinstance(profiles, Mapping):
        defaults = profiles.get(plant_profile)
        if isinstance(defaults, Mapping):
            for key, value in defaults.items():
                merged.setdefault(str(key), value)

    if prompt_context:
        merged.update(dict(prompt_context))
    merged["plant_profile"] = plant_profile
    return merged


def _stringify_context_value(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, (list, tuple, set)):
        return "、".join(_stringify_context_value(item) for item in value)
    return str(value)


def format_model_context(
    prompt_context: Optional[Mapping[str, Any]],
    config: Mapping[str, Any],
    *,
    serial_port: Optional[str] = None,
) -> str:
    """
    渲染「## 模型上下文信息」块；与 reference `_format_prompt_context` 行为一致。
    若 prompt_context 为 None，则使用 config['context']['default_keys']，
    并用参数 serial_port 覆盖其中的 serial_port（若传入非空）。
    """
    defaults = (config.get("context") or {}).get("default_keys") or {}
    merged: Dict[str, Any] = dict(defaults)
    if serial_port is not None and str(serial_port).strip():
        merged["serial_port"] = str(serial_port).strip()
    if prompt_context:
        merged.update(dict(prompt_context))
    if not merged.get("serial_port"):
        merged["serial_port"] = "UNKNOWN"

    title = _required_str(config, "user.section_title_model_context")
    lines = [title]
    for key, value in merged.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, tuple, set)) and not value:
            continue
        label = str(key).replace("_", " ")
        lines.append(f"- {label}: {_stringify_context_value(value)}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _preference_block(summary: str, config: Mapping[str, Any]) -> str:
    summary = str(summary or "").strip()
    if not summary:
        return ""
    pref = _required_mapping(config, "user.preference_section")
    title = _required_str(config, "user.preference_section.title")
    lines_out = [title]
    for line in pref.get("lines") or []:
        lines_out.append(str(line).format(summary=summary))
    return "\n".join(lines_out)


def _single_controller_block(config: Mapping[str, Any]) -> str:
    u = _required_mapping(config, "user")
    title = _required_str(config, "user.section_title_single_controller")
    bullets = u.get("single_controller_bullets") or []
    parts = [title, *[str(b) for b in bullets]]
    return "\n".join(parts)


def _round_task_block(config: Mapping[str, Any], *, plant_profile: str) -> str:
    u = _required_mapping(config, "user")
    rt_block = (
        u.get("round_task_virtual_tracking")
        if plant_profile == "virtual_tracking"
        else None
    )
    rt = rt_block or u.get("round_task") or {}
    section = _required_str(config, "user.section_title_round_task")
    mode_label = str(rt.get("mode_label", "")).strip()
    task_line = str(rt.get("task_line", "")).strip()
    compare_line = str(rt.get("compare_line", "")).strip()
    output_fields = str(rt.get("output_fields", "")).strip()
    json_only_tmpl = str(rt.get("json_only_line", "")).strip()
    if not all((mode_label, task_line, compare_line, output_fields, json_only_tmpl)):
        raise ValueError(f"round_task section incomplete for profile={plant_profile}")
    json_only = json_only_tmpl.format(output_fields=output_fields)
    body = "\n".join(
        [
            f"- 当前模式：{mode_label}",
            f"- {task_line}",
            f"- {compare_line}",
            json_only,
        ]
    )
    return f"{section}\n{body}"


def build_user(
    history_text: Optional[str],
    prompt_data: str,
    prompt_context: Optional[Mapping[str, Any]],
    user_preference_summary: Optional[str],
    config: Mapping[str, Any],
    *,
    plant_profile: str,
    serial_port: Optional[str] = None,
    include_context: bool = True,
) -> str:
    defaults = _required_mapping(config, "user.defaults")
    history_block = (history_text or "").strip() or str(defaults.get("empty_history", "")).strip()
    data_block = (prompt_data or "").strip() or str(defaults.get("no_round_data", "")).strip()
    if not history_block or not data_block:
        raise ValueError("user.defaults.empty_history/no_round_data must be non-empty")

    sections: List[str] = [history_block, data_block]

    if include_context:
        ctx = format_model_context(prompt_context, config, serial_port=serial_port)
        if ctx:
            sections.append(ctx)

    pref = _preference_block(user_preference_summary or "", config)
    if pref:
        sections.append(pref)

    sections.append(_round_task_block(config, plant_profile=plant_profile))
    sections.append(_single_controller_block(config))

    return "\n\n".join(s for s in sections if s)


def build_full_prompt(
    samples: List[Mapping[str, Any]],
    history_text: Optional[str] = None,
    prompt_context: Optional[Mapping[str, Any]] = None,
    user_preference_summary: Optional[str] = None,
    *,
    serial_port: Optional[str] = None,
    config_path: Optional[str | Path] = None,
    include_context: bool = True,
) -> Dict[str, str]:
    config = load_prompt_config(config_path)
    plant_profile = resolve_plant_profile(prompt_context)
    merged_ctx = merge_prompt_context(config, prompt_context, plant_profile=plant_profile)
    system = build_system(config, plant_profile=plant_profile)
    prompt_data = build_prompt_data(samples, config, plant_profile=plant_profile)
    user = build_user(
        history_text,
        prompt_data,
        merged_ctx,
        user_preference_summary,
        config,
        plant_profile=plant_profile,
        serial_port=serial_port,
        include_context=include_context,
    )
    return {"system": system, "user": user}


def write_multi_round_prompt_log(
    test_data_path: str | Path,
    output_log_path: str | Path,
    *,
    config_path: Optional[str | Path] = None,
) -> None:
    """
    读取 test_data.json，对每一轮调用 build_full_prompt，将发往模型的完整 system/user
    正文写入 output_log_path（多轮依次追加，便于检查每轮差异）。
    """
    td = Path(test_data_path)
    raw = json.loads(td.read_text(encoding="utf-8"))
    serial_port = str(raw.get("serial_port") or "").strip() or None
    pref = raw.get("user_preference_summary")
    pctx = raw.get("prompt_context")
    rounds = raw.get("rounds") or []
    if not rounds:
        raise ValueError("test_data.json: missing or empty 'rounds'")

    out_path = Path(output_log_path)
    blocks: List[str] = []
    n = len(rounds)
    for idx, rd in enumerate(rounds, start=1):
        ridx = int(rd.get("round_index", idx))
        samples = rd.get("samples") or []
        history_text = rd.get("history_text")
        if history_text is not None:
            history_text = str(history_text)

        out = build_full_prompt(
            samples,
            history_text=history_text,
            prompt_context=pctx,
            user_preference_summary=str(pref) if pref else None,
            serial_port=serial_port,
            config_path=config_path,
        )
        system = out["system"]
        user = out["user"]

        blocks.append("")
        blocks.append("=" * 80)
        blocks.append(f"ROUND {ridx} / {n}  —  FULL TEXT SENT TO MODEL (two logical messages)")
        blocks.append("=" * 80)
        blocks.append("")
        blocks.append(format_prompt_messages(system=system, user=user).strip())
        blocks.append("")

    out_path.write_text("\n".join(blocks).lstrip("\n"), encoding="utf-8")


def run_one_shot_ai_pipeline(
    test_data_path: str | Path,
    output_log_path: str | Path,
    output2_log_path: str | Path,
    *,
    config_path: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """
    一次性闭环：
    test_data 第 1 轮 -> build_full_prompt -> 写 output.log -> request_once -> raw response 写 output2.log。
    """
    td = Path(test_data_path)
    raw = json.loads(td.read_text(encoding="utf-8"))
    rounds = raw.get("rounds") or []
    if not rounds:
        raise ValueError("test_data.json: missing or empty 'rounds'")

    r0 = rounds[0]
    samples = r0.get("samples") or []
    history_text = r0.get("history_text")
    if history_text is not None:
        history_text = str(history_text)

    serial_port = str(raw.get("serial_port") or "").strip() or None
    pref = raw.get("user_preference_summary")
    pctx = raw.get("prompt_context")

    prompt = build_full_prompt(
        samples,
        history_text=history_text,
        prompt_context=pctx,
        user_preference_summary=str(pref) if pref else None,
        serial_port=serial_port,
        config_path=config_path,
    )

    output_log = Path(output_log_path)
    blocks: List[str] = [
        "=" * 80,
        "ONE-SHOT REQUEST PROMPT",
        "=" * 80,
        "",
        format_prompt_messages(system=prompt["system"], user=prompt["user"]).strip(),
        "",
    ]
    output_log.write_text("\n".join(blocks), encoding="utf-8")

    from .client import request_once

    resp = request_once(system=prompt["system"], user=prompt["user"])
    output2_log = append_capture_to_output2_log(
        resp["raw_text"], log_path=Path(output2_log_path)
    )

    return {
        "output_log_path": str(output_log),
        "output2_log_path": str(output2_log),
        "usage_log_path": str(resp.get("usage_log_path", "")),
        "model": str(resp.get("model", "")),
    }


__all__ = [
    "load_prompt_config",
    "clear_prompt_config_cache",
    "build_system",
    "resolve_plant_profile",
    "merge_prompt_context",
    "format_model_context",
    "build_user",
    "build_full_prompt",
    "build_prompt_data",
    "compute_metrics",
    "write_multi_round_prompt_log",
    "run_one_shot_ai_pipeline",
]

if __name__ == "__main__":
    _here = Path(__file__).resolve().parent
    _test_data = _here / "test_data.json"
    _output_log = _here / "output.log"
    _output2_log = _here / "output2.log"

    if not _test_data.is_file():
        print(
            f"[ERROR] Missing {_test_data.name}. Please prepare a test_data.json in tuning_tool/ first.",
            flush=True,
        )
        raise SystemExit(1)

    result = run_one_shot_ai_pipeline(_test_data, _output_log, _output2_log)
    print(f"[OK] Wrote prompt to {result['output_log_path']}")
    print(f"[OK] Wrote parse log to {result['output2_log_path']}")
    if result.get("usage_log_path"):
        print(f"[OK] Usage appended to {result['usage_log_path']}")
