from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Mapping

from tuning_tool.client import request_once
from tuning_tool.prompt_concat import build_full_prompt
from tuning_tool.response_parser import append_capture_to_output2_log, parse_json_response

_CSV_FIELDS = ("timestamp", "setpoint", "input", "pwm", "error", "p", "i", "d")


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _ensure_round_dir() -> Path:
    out_dir = _project_root() / "web_tool" / "runtime" / "virtual_rounds"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _coerce_float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _normalize_samples(raw_samples: Any) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    if not isinstance(raw_samples, list):
        return out
    for row in raw_samples:
        if not isinstance(row, Mapping):
            continue
        out.append({field: _coerce_float(row.get(field), 0.0) for field in _CSV_FIELDS})
    return out


def _write_round_csv(round_index: int, samples: list[dict[str, float]]) -> Path:
    out_dir = _ensure_round_dir()
    csv_path = out_dir / f"round_{round_index}.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(_CSV_FIELDS))
        writer.writeheader()
        for row in samples:
            writer.writerow(row)
    return csv_path


def _format_prompt_text(system: str, user: str, round_index: int) -> str:
    lines = [
        "=" * 80,
        f"ROUND {round_index} REQUEST PROMPT",
        "=" * 80,
        "",
        "----- BEGIN role=system -----",
        system,
        "----- END role=system -----",
        "",
        "----- BEGIN role=user -----",
        user,
        "----- END role=user -----",
        "",
    ]
    return "\n".join(lines)


def run_virtual_round(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    raw_payload = payload or {}
    round_index = int(raw_payload.get("round_index") or 1)
    samples = _normalize_samples(raw_payload.get("samples"))
    history_text_raw = raw_payload.get("history_text")
    history_text = str(history_text_raw).strip() if history_text_raw is not None else None
    if history_text == "":
        history_text = None
    if not samples:
        raise ValueError("samples 为空，无法执行本轮调参。")

    csv_path = _write_round_csv(round_index, samples)
    prompt = build_full_prompt(samples=samples, history_text=history_text)
    prompt_text = _format_prompt_text(prompt["system"], prompt["user"], round_index)

    llm_resp = request_once(system=prompt["system"], user=prompt["user"])
    raw_text = str(llm_resp.get("raw_text") or "")
    if not raw_text:
        raise RuntimeError("模型返回为空。")

    out_dir = _ensure_round_dir()
    output2_path = out_dir / f"round_{round_index}_output2.log"
    append_capture_to_output2_log(raw_text, log_path=output2_path)
    response_text = output2_path.read_text(encoding="utf-8")

    parsed = parse_json_response(raw_text, strict=True) or parse_json_response(raw_text, strict=False)
    parsed_pid = {
        "p": _coerce_float((parsed or {}).get("p"), 0.0),
        "i": _coerce_float((parsed or {}).get("i"), 0.0),
        "d": _coerce_float((parsed or {}).get("d"), 0.0),
    }

    return {
        "status": "ok",
        "round_index": round_index,
        "csv_path": str(csv_path),
        "output2_log_path": str(output2_path),
        "prompt_text": prompt_text,
        "response_text": response_text,
        "raw_response_text": raw_text,
        "parsed_pid": parsed_pid,
        "model": str(llm_resp.get("model") or ""),
    }
