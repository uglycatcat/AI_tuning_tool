#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从模型原始回复中抽取 JSON 调参结果（对齐 reference_project llm/response_parser.py）。

命令行用法：运行后粘贴终端里得到的完整模型输出，以 Ctrl-D 结束；全部内容会先写入 output2.log，
并追加一次解析探测结果，便于后续再决定字段去向。

  python3 -m tuning_tool.response_parser
  # 或
  python3 tuning_tool/response_parser.py
"""

from __future__ import annotations

import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_FLOAT_PATTERN = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_PID_TRIPLET_RE = re.compile(
    rf"P\s*[:=]\s*({_FLOAT_PATTERN})\s*[,，]?\s*I\s*[:=]\s*({_FLOAT_PATTERN})\s*[,，]?\s*D\s*[:=]\s*({_FLOAT_PATTERN})",
    re.IGNORECASE,
)
_STATUS_RE = re.compile(r"\b(DONE|TUNING)\b", re.IGNORECASE)

# 单环 PID 输出收紧：与 default_prompt.json 中 system 硬约束一致（可在此调参）
REQUIRED_OUTPUT_KEYS = (
    "thought_process",
    "analysis_summary",
    "tuning_action",
    "p",
    "i",
    "d",
    "status",
)
_MAX_LEN = {
    "thought_process": 2000,
    "analysis_summary": 1200,
    "tuning_action": 800,
}
_PID_CEIL = {"p": 100.0, "i": 30.0, "d": 20.0}


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(_coerce_text(x) for x in value)
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    return str(value)


def _strip_inline_fences(text: str) -> str:
    return re.sub(r"```[\s\S]*?```", "", text, flags=re.IGNORECASE)


def _strip_markdown_heading_lines(text: str) -> str:
    out_lines: List[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        out_lines.append(line)
    return "\n".join(out_lines).strip()


def _clamp_pid(name: str, v: float, issues: List[str]) -> float:
    hi = float(_PID_CEIL.get(name, 1e9))
    if v > hi:
        issues.append(f"clamp {name}: {v} -> {hi}")
        return hi
    return v


def finalize_llm_output(
    data: Dict[str, Any],
    *,
    strict: bool = True,
) -> tuple[Optional[Dict[str, Any]], List[str]]:
    """
    在 sanitize_result 之后进一步收紧输出：
    - 白名单仅保留单环 7 键；
    - 文本字段去 Markdown 标题行、去围栏残留、截断长度；
    - list/dict 等非字符串思考过程会被压平；
    - PID 做上界钳制（与参考工程默认护栏同量级，防止乱值）。
    strict=True：缺 p/i/d、非数、或清理后 thought 仍为空则整段判失败返回 None。
    strict=False：仍要求 p/i/d 合法；允许 thought 在清理后为空（用短占位句）。
    """
    issues: List[str] = []
    d = dict(data)

    for k in ("p", "i", "d"):
        if k not in d:
            issues.append(f"missing {k}")
            return None, issues
        try:
            num = float(d[k])
        except (TypeError, ValueError):
            issues.append(f"invalid numeric {k}")
            return None, issues
        if not math.isfinite(num) or num < 0:
            issues.append(f"non-finite or negative {k}")
            return None, issues
        d[k] = _clamp_pid(k, num, issues)

    for text_key in ("thought_process", "analysis_summary", "tuning_action"):
        raw = _coerce_text(d.get(text_key, ""))
        raw = _strip_inline_fences(raw)
        raw = _strip_markdown_heading_lines(raw)
        raw = raw.strip()
        lim = int(_MAX_LEN.get(text_key, 4000))
        if len(raw) > lim:
            issues.append(f"truncate {text_key}: {len(raw)} -> {lim}")
            raw = raw[:lim].rstrip()
        d[text_key] = raw

    if "status" not in d or str(d.get("status", "")).strip() == "":
        d["status"] = "TUNING"
    st = str(d.get("status", "TUNING")).strip().upper()
    d["status"] = "DONE" if st == "DONE" else "TUNING"

    if strict:
        if not str(d.get("thought_process", "")).strip():
            issues.append("thought_process empty after cleanup")
            return None, issues
    else:
        if not str(d.get("thought_process", "")).strip():
            d["thought_process"] = "（解析宽松模式：原 thought_process 为空，已占位）"
            issues.append("filled empty thought_process (loose mode)")

    out: Dict[str, Any] = {k: d[k] for k in REQUIRED_OUTPUT_KEYS}
    if set(out.keys()) != set(REQUIRED_OUTPUT_KEYS):
        issues.append(f"internal build error keys={sorted(out.keys())}")
        return None, issues

    return out, issues


def extract_json_candidates(text: str) -> List[str]:
    candidates: List[str] = []
    stripped = text.strip()

    if stripped:
        candidates.append(stripped)

    fenced_matches = re.findall(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    candidates.extend(fenced_matches)

    for start in range(len(text)):
        if text[start] != "{":
            continue
        depth = 0
        for end in range(start, len(text)):
            char = text[end]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : end + 1])
                    break

    return candidates


def _sanitize_pid_mapping(mapping: Dict[str, Any]) -> Dict[str, float]:
    pid_values: Dict[str, float] = {}
    for key in ("p", "i", "d"):
        value = mapping.get(key)
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric) and numeric >= 0:
            pid_values[key] = numeric
    return pid_values


def sanitize_result(data: Dict[str, Any]) -> Dict[str, Any]:
    sanitized = dict(data)

    for key in ("p", "i", "d"):
        value = sanitized.get(key)
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            sanitized.pop(key, None)
            continue

        if not math.isfinite(numeric) or numeric < 0:
            sanitized.pop(key, None)
        else:
            sanitized[key] = numeric

    for controller_key in ("controller_1", "controller_2"):
        controller_value = sanitized.get(controller_key)
        if isinstance(controller_value, dict):
            sanitized[controller_key] = _sanitize_pid_mapping(controller_value)
            if not sanitized[controller_key]:
                sanitized.pop(controller_key, None)

    if "status" in sanitized:
        status = str(sanitized["status"]).strip().upper()
        sanitized["status"] = "DONE" if status == "DONE" else "TUNING"

    if not sanitized.get("analysis_summary"):
        sanitized["analysis_summary"] = str(
            sanitized.get("analysis") or "No analysis summary provided."
        )

    if not sanitized.get("thought_process"):
        sanitized["thought_process"] = str(
            sanitized.get("analysis_summary") or "No detailed reasoning provided."
        )

    if not sanitized.get("tuning_action"):
        sanitized["tuning_action"] = "ADJUST_PID"

    return sanitized


def _extract_labeled_section(text: str, labels: List[str]) -> str:
    pattern = re.compile(
        r"\[(?:"
        + "|".join(re.escape(label) for label in labels)
        + r")\]\s*(.+?)(?=\n\s*\[[^\]]+\]|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        return ""
    return match.group(1).strip().rstrip(",，")


def _extract_pid_triplet(text: str) -> Dict[str, float]:
    match = _PID_TRIPLET_RE.search(text)
    if not match:
        return {}
    parsed = {
        "p": float(match.group(1)),
        "i": float(match.group(2)),
        "d": float(match.group(3)),
    }
    return _sanitize_pid_mapping(parsed)


def parse_structured_text_response(text: str) -> Optional[Dict[str, Any]]:
    stripped = text.strip()
    if not stripped:
        return None

    result: Dict[str, Any] = {}

    thought = _extract_labeled_section(stripped, ["思考", "Thought"])
    if thought:
        result["thought_process"] = thought

    analysis = _extract_labeled_section(stripped, ["分析", "Analysis"])
    if analysis:
        result["analysis_summary"] = analysis

    tuning_action = _extract_labeled_section(stripped, ["调参", "Action"])
    if tuning_action:
        result["tuning_action"] = tuning_action

    controller_1 = _extract_pid_triplet(
        _extract_labeled_section(stripped, ["控制器 1", "Controller 1"])
    )
    if controller_1:
        result["controller_1"] = controller_1

    controller_2 = _extract_pid_triplet(
        _extract_labeled_section(stripped, ["控制器 2", "Controller 2"])
    )
    if controller_2:
        result["controller_2"] = controller_2

    if "controller_1" not in result:
        target_text = tuning_action if tuning_action else stripped
        single_pid = _extract_pid_triplet(target_text)
        if not single_pid and tuning_action:
            pid_section = _extract_labeled_section(stripped, ["PID"])
            if pid_section:
                single_pid = _extract_pid_triplet(pid_section)
            else:
                text_without_analysis = stripped
                if analysis:
                    text_without_analysis = stripped.replace(analysis, "")
                single_pid = _extract_pid_triplet(text_without_analysis)

        if single_pid:
            result.update(single_pid)

    status_block = _extract_labeled_section(stripped, ["状态", "Status"])
    status_match = _STATUS_RE.search(status_block or stripped)
    if status_match:
        result["status"] = status_match.group(1).upper()

    has_pid = any(key in result for key in ("p", "i", "d", "controller_1", "controller_2"))
    if not has_pid:
        return None

    return sanitize_result(result)


def parse_json_response(
    text: str,
    *,
    strict: bool = True,
    collect_notes: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    抽取 JSON 并 sanitize；默认 strict=True 时再经 finalize_llm_output，
    不符合白名单/长度/必填的候选会跳过，尝试下一候选。
    若传入 collect_notes，会把 finalize_llm_output 返回的 issues 追加进去（成功或失败都会累积尝试痕迹）。
    """
    for candidate in extract_json_candidates(text):
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        merged = sanitize_result(data)
        finalized, issues = finalize_llm_output(merged, strict=strict)
        if collect_notes is not None:
            collect_notes.extend(issues)
        if finalized is not None:
            return finalized
    rest = parse_structured_text_response(text)
    if rest is None:
        return None
    finalized, issues = finalize_llm_output(rest, strict=strict)
    if collect_notes is not None:
        collect_notes.extend(issues)
    return finalized


def append_capture_to_output2_log(
    raw_text: str,
    *,
    log_path: Optional[Path] = None,
) -> Path:
    """
    将 stdin 得到的原始全文写入 tuning_tool/output2.log（含时间戳与字节数），
    并追加解析探测块（parse_json_response 成功则输出 JSON，否则标明失败）。
    """
    base = Path(__file__).resolve().parent
    path = log_path or (base / "output2.log")
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    body = raw_text if isinstance(raw_text, str) else str(raw_text)

    lines: List[str] = [
        "=" * 72,
        f"captured_at: {ts}",
        f"bytes_utf8: {len(body.encode('utf-8'))}",
        "chars: " + str(len(body)),
        "=" * 72,
        "",
        "--- raw_paste (verbatim below) ---",
        "",
        body,
        "",
        "=" * 72,
        "--- parse_probe (for later routing; not authoritative) ---",
        "",
    ]

    try:
        strict_notes: List[str] = []
        loose_notes: List[str] = []
        parsed_strict = parse_json_response(body, strict=True, collect_notes=strict_notes)
        parsed_loose = parsed_strict
        if parsed_strict is None:
            parsed_loose = parse_json_response(body, strict=False, collect_notes=loose_notes)
        if parsed_strict is None and parsed_loose is None:
            lines.append("parse_json_response(strict=True): None")
            lines.append("parse_json_response(strict=False): None")
        else:
            if parsed_strict is None:
                lines.append(
                    "strict parse failed; loose parse shown below if non-None (may use placeholder text)."
                )
            lines.append("--- strict ---")
            lines.append(
                json.dumps(parsed_strict, ensure_ascii=False, indent=2)
                if parsed_strict
                else "None"
            )
            if strict_notes:
                lines.append("--- guard_notes (strict attempts) ---")
                lines.extend(f"- {n}" for n in strict_notes)
            if parsed_loose != parsed_strict:
                lines.append("--- loose (fallback) ---")
                lines.append(json.dumps(parsed_loose, ensure_ascii=False, indent=2))
                if loose_notes:
                    lines.append("--- guard_notes (loose) ---")
                    lines.extend(f"- {n}" for n in loose_notes)
    except Exception as exc:  # noqa: BLE001
        lines.append(f"parse_json_response raised: {exc!r}")

    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


__all__ = [
    "extract_json_candidates",
    "parse_structured_text_response",
    "parse_json_response",
    "sanitize_result",
    "finalize_llm_output",
    "append_capture_to_output2_log",
]


def _cli_main() -> None:
    here = Path(__file__).resolve().parent
    print(
        "请粘贴「模型完整输出」（可含 AI: 前缀、Markdown 围栏等），输入结束后：",
        file=sys.stderr,
    )
    print("  Linux/macOS: Ctrl-D", file=sys.stderr)
    print("  若为空直接结束，将只写入空捕获记录。", file=sys.stderr)
    print("", file=sys.stderr)

    raw = sys.stdin.read()
    out = append_capture_to_output2_log(raw)
    print(f"[OK] 已写入全文与解析探测到: {out}", file=sys.stderr)


if __name__ == "__main__":
    _cli_main()
