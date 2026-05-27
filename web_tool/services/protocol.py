from __future__ import annotations

import re
from typing import Mapping

SERIAL_SAMPLE_FIELDS = ("timestamp", "setpoint", "input", "pwm", "error", "p", "i", "d")
PID_TUNING_RE = re.compile(r"^\s*pid_tuning_param\s*:\s*(.+?)\s*$", re.IGNORECASE)


def _split_payload(payload: str) -> dict[str, float] | None:
    parts = payload.split(",")
    if len(parts) != len(SERIAL_SAMPLE_FIELDS):
        return None
    out: dict[str, float] = {}
    for key, raw in zip(SERIAL_SAMPLE_FIELDS, parts, strict=True):
        try:
            out[key] = float(raw.strip())
        except ValueError:
            return None
    return out


def parse_pid_tuning_line(line: str) -> dict[str, float] | None:
    s = line.strip()
    match = PID_TUNING_RE.match(s)
    if match is None:
        return None
    return _split_payload(match.group(1))


def normalize_sample_row(row: Mapping[str, object]) -> dict[str, float]:
    out: dict[str, float] = {}
    for field in SERIAL_SAMPLE_FIELDS:
        try:
            out[field] = float(row.get(field, 0.0))
        except (TypeError, ValueError):
            out[field] = 0.0
    return out


__all__ = [
    "SERIAL_SAMPLE_FIELDS",
    "PID_TUNING_RE",
    "parse_pid_tuning_line",
    "normalize_sample_row",
]
