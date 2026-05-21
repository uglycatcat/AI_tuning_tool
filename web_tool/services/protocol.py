from __future__ import annotations

from typing import Mapping

SERIAL_SAMPLE_FIELDS = ("timestamp", "setpoint", "input", "pwm", "error", "p", "i", "d")
PID_TUNING_PREFIX = "pid_tuning_param["
PID_TUNING_SUFFIX = "]"


def parse_pid_tuning_line(line: str) -> dict[str, float] | None:
    s = line.strip()
    if not (s.startswith(PID_TUNING_PREFIX) and s.endswith(PID_TUNING_SUFFIX)):
        return None
    inner = s[len(PID_TUNING_PREFIX) : -len(PID_TUNING_SUFFIX)]
    parts = inner.split(",")
    if len(parts) != len(SERIAL_SAMPLE_FIELDS):
        return None
    out: dict[str, float] = {}
    for key, raw in zip(SERIAL_SAMPLE_FIELDS, parts, strict=True):
        try:
            out[key] = float(raw.strip())
        except ValueError:
            return None
    return out


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
    "PID_TUNING_PREFIX",
    "PID_TUNING_SUFFIX",
    "parse_pid_tuning_line",
    "normalize_sample_row",
]
