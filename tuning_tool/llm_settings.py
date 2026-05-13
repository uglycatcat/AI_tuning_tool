"""项目根 config.json 与 LLM 用量日志的共享加载逻辑（chat.py 与 tuning_tool.client 共用）。"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, TextIO

CONFIG_NAME = "config.json"
DEFAULT_BASE_URL = "https://c.loonaai.cn"
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT = 120.0
DEFAULT_USAGE_LOG = "token_usage_sessions.jsonl"

USAGE_COUNT_KEYS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def read_root_config(*, log_read_errors_to: TextIO | None = None) -> dict[str, Any]:
    path = project_root() / CONFIG_NAME
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        if log_read_errors_to is not None:
            print(f"读取 {CONFIG_NAME} 失败: {e}", file=log_read_errors_to)
        return {}


def coerce_int(value: object, default: int, *, min_v: int, max_v: int) -> int:
    if value is None or value == "":
        n = default
    elif isinstance(value, bool):
        n = default
    elif isinstance(value, int):
        n = value
    else:
        try:
            n = int(str(value).strip())
        except (TypeError, ValueError):
            n = default
    return max(min_v, min(max_v, n))


def coerce_float(value: object, default: float, *, min_v: float, max_v: float) -> float:
    if value is None or value == "":
        x = default
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        x = float(value)
    else:
        try:
            x = float(str(value).strip())
        except (TypeError, ValueError):
            x = default
    return max(min_v, min(max_v, x))


def optional_price(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def resolve_usage_log_path(data: dict[str, Any], *, root: Path) -> Path:
    raw = os.environ.get("USAGE_LOG_FILE", "").strip() or str(
        data.get("USAGE_LOG_FILE", "") or ""
    ).strip() or DEFAULT_USAGE_LOG
    p = Path(raw)
    if not p.is_absolute():
        p = root / p
    return p


def optional_system(data: dict[str, Any]) -> str | None:
    if os.environ.get("ANTHROPIC_SYSTEM") is not None:
        s = os.environ.get("ANTHROPIC_SYSTEM", "").strip()
        return s or None
    raw = data.get("SYSTEM")
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip()
        return s or None
    return None


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def usage_counts(usage: Any) -> dict[str, int]:
    out = {k: 0 for k in USAGE_COUNT_KEYS}
    if usage is None:
        return out
    for k in USAGE_COUNT_KEYS:
        v = getattr(usage, k, None)
        if v is None and isinstance(usage, Mapping):
            v = usage.get(k)
        try:
            out[k] = max(0, int(v or 0))
        except (TypeError, ValueError):
            out[k] = 0
    return out


def sum_usage_tokens(counts: dict[str, int]) -> int:
    return int(sum(counts.values()))


def estimate_cost_usd(
    counts: dict[str, int],
    in_price: float | None,
    out_price: float | None,
) -> float | None:
    if in_price is None and out_price is None:
        return None
    cost = 0.0
    if in_price is not None:
        cost += (counts["input_tokens"] / 1_000_000.0) * in_price
    if out_price is not None:
        cost += (counts["output_tokens"] / 1_000_000.0) * out_price
    return round(cost, 8)


def append_session_usage_log(
    path: Path,
    *,
    started_at: str,
    ended_at: str,
    model: str,
    input_price_per_mtok: float | None,
    output_price_per_mtok: float | None,
    counts: dict[str, int],
) -> None:
    total = sum_usage_tokens(counts)
    est = estimate_cost_usd(counts, input_price_per_mtok, output_price_per_mtok)
    record = {
        "程序启动时间": started_at,
        "程序结束时间": ended_at,
        "所用模型": model,
        "模型输入价格_每百万token": input_price_per_mtok,
        "模型输出价格_每百万token": output_price_per_mtok,
        "输入token用量": counts["input_tokens"],
        "输出token用量": counts["output_tokens"],
        "缓存创建输入token": counts["cache_creation_input_tokens"],
        "缓存读取输入token": counts["cache_read_input_tokens"],
        "token总用量_各分项之和": total,
        "估算费用_仅输入加输出常规项": est,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)


def extract_text_blocks(message: Any) -> str:
    parts: list[str] = []
    for block in message.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts).strip()


def web_tool_host_from_config(data: dict[str, Any]) -> str | None:
    if "WEB_TOOL_HOST" not in data:
        return None
    raw = str(data.get("WEB_TOOL_HOST", "") or "").strip()
    return raw or None


def web_tool_port_from_config(data: dict[str, Any]) -> int | None:
    if "WEB_TOOL_PORT" not in data:
        return None
    v = data.get("WEB_TOOL_PORT")
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    try:
        p = int(v) if isinstance(v, int) else int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return max(1, min(65535, p))


__all__ = [
    "CONFIG_NAME",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TIMEOUT",
    "DEFAULT_USAGE_LOG",
    "USAGE_COUNT_KEYS",
    "append_session_usage_log",
    "coerce_float",
    "coerce_int",
    "estimate_cost_usd",
    "extract_text_blocks",
    "now_iso",
    "optional_price",
    "optional_system",
    "project_root",
    "read_root_config",
    "resolve_usage_log_path",
    "sum_usage_tokens",
    "usage_counts",
    "web_tool_host_from_config",
    "web_tool_port_from_config",
]
