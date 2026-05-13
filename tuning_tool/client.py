#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性 AI 请求客户端（复用 chat.py 的配置与用量日志语义）。"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

CONFIG_NAME = "config.json"
DEFAULT_BASE_URL = "https://c.loonaai.cn"
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT = 120.0
DEFAULT_USAGE_LOG = "token_usage_sessions.jsonl"
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_DELAY_SECONDS = 0.75

USAGE_COUNT_KEYS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model: str
    max_tokens: int
    timeout: float
    usage_log_path: Path
    input_price_per_mtok: float | None
    output_price_per_mtok: float | None
    max_retries: int
    retry_base_delay_seconds: float


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _read_config() -> dict[str, Any]:
    path = _project_root() / CONFIG_NAME
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _coerce_int(value: object, default: int, *, min_v: int, max_v: int) -> int:
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


def _coerce_float(value: object, default: float, *, min_v: float, max_v: float) -> float:
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


def _optional_price(value: object) -> float | None:
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


def _resolve_usage_log_path(data: dict[str, Any]) -> Path:
    raw = os.environ.get("USAGE_LOG_FILE", "").strip() or str(
        data.get("USAGE_LOG_FILE", "") or ""
    ).strip() or DEFAULT_USAGE_LOG
    p = Path(raw)
    if not p.is_absolute():
        p = _project_root() / p
    return p


def load_settings() -> Settings:
    data = _read_config()
    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip() or str(
        data.get("LLM_API_KEY", "")
    ).strip()
    base_url = (
        os.environ.get("ANTHROPIC_BASE_URL", "").strip()
        or str(data.get("BASE_URL", "")).strip()
        or DEFAULT_BASE_URL
    ).rstrip("/")
    model = (
        os.environ.get("ANTHROPIC_MODEL", "").strip()
        or str(data.get("MODEL", "")).strip()
        or DEFAULT_MODEL
    )
    max_tokens = _coerce_int(
        os.environ.get("ANTHROPIC_MAX_TOKENS", "") or data.get("MAX_TOKENS"),
        DEFAULT_MAX_TOKENS,
        min_v=1,
        max_v=200_000,
    )
    timeout = _coerce_float(
        os.environ.get("ANTHROPIC_TIMEOUT", "") or data.get("REQUEST_TIMEOUT_SECONDS"),
        DEFAULT_TIMEOUT,
        min_v=5.0,
        max_v=600.0,
    )
    usage_log_path = _resolve_usage_log_path(data)
    max_retries = _coerce_int(
        os.environ.get("ANTHROPIC_MAX_RETRIES", "") or data.get("REQUEST_MAX_RETRIES"),
        DEFAULT_MAX_RETRIES,
        min_v=0,
        max_v=15,
    )
    retry_base_delay_seconds = _coerce_float(
        os.environ.get("ANTHROPIC_RETRY_BASE_DELAY_SECONDS", "")
        or data.get("REQUEST_RETRY_BASE_DELAY_SECONDS"),
        DEFAULT_RETRY_BASE_DELAY_SECONDS,
        min_v=0.05,
        max_v=60.0,
    )
    in_price = _optional_price(
        os.environ.get("MODEL_INPUT_PRICE_PER_MTOK", "")
        or data.get("MODEL_INPUT_PRICE_PER_MTOK")
    )
    out_price = _optional_price(
        os.environ.get("MODEL_OUTPUT_PRICE_PER_MTOK", "")
        or data.get("MODEL_OUTPUT_PRICE_PER_MTOK")
    )

    if not api_key:
        raise RuntimeError(
            "未找到 API 密钥：在 config.json 设置 LLM_API_KEY，或设置环境变量 ANTHROPIC_AUTH_TOKEN。"
        )

    return Settings(
        api_key=api_key,
        base_url=base_url,
        model=model,
        max_tokens=max_tokens,
        timeout=timeout,
        usage_log_path=usage_log_path,
        input_price_per_mtok=in_price,
        output_price_per_mtok=out_price,
        max_retries=max_retries,
        retry_base_delay_seconds=retry_base_delay_seconds,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _usage_counts(usage: Any) -> dict[str, int]:
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


def _sum_usage_tokens(counts: dict[str, int]) -> int:
    return int(sum(counts.values()))


def _estimate_cost_usd(
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
    total = _sum_usage_tokens(counts)
    est = _estimate_cost_usd(counts, input_price_per_mtok, output_price_per_mtok)
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
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _http_status_retryable(status_code: int) -> bool:
    """网关多后端时 502（含无效 model slug 透传）、限流等与短暂不可用可短暂重试。"""
    return status_code in (408, 409, 429, 502, 503, 504)


def extract_text_blocks(message: Any) -> str:
    parts: list[str] = []
    for block in message.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts).strip()


def request_once(*, system: str, user: str) -> Dict[str, Any]:
    """
    发起一次真实请求并返回结构化结果。
    同步把 usage 统计写入根目录 token_usage_sessions.jsonl（与 chat.py 一致语义）。
    """
    try:
        from anthropic import Anthropic, APIConnectionError, APIStatusError
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "缺少依赖 anthropic。请先在当前 Python 环境安装：pip install anthropic"
        ) from exc

    s = load_settings()
    started = _now_iso()
    counts = {k: 0 for k in USAGE_COUNT_KEYS}

    try:
        client = Anthropic(api_key=s.api_key, base_url=s.base_url, timeout=s.timeout)
        kwargs: Dict[str, Any] = {
            "model": s.model,
            "max_tokens": s.max_tokens,
            "messages": [{"role": "user", "content": user}],
        }
        system_text = (system or "").strip()
        if system_text:
            kwargs["system"] = system_text

        resp = None
        for attempt in range(s.max_retries + 1):
            try:
                resp = client.messages.create(**kwargs)
                break
            except APIStatusError as e:
                code = int(getattr(e, "status_code", 0) or 0)
                if attempt < s.max_retries and _http_status_retryable(code):
                    time.sleep(s.retry_base_delay_seconds * (2**attempt))
                    continue
                raise RuntimeError(f"API 错误 ({e.status_code}): {e.message}") from e
            except APIConnectionError as e:
                if attempt < s.max_retries:
                    time.sleep(s.retry_base_delay_seconds * (2**attempt))
                    continue
                raise RuntimeError(f"连接失败: {e}") from e
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"请求异常: {e}") from e
        if resp is None:
            raise RuntimeError("未收到模型响应。")

        usage = _usage_counts(getattr(resp, "usage", None))
        for k in counts:
            counts[k] += usage[k]

        raw_text = extract_text_blocks(resp)
        if not raw_text:
            raise RuntimeError("模型返回空内容。")

        ended = _now_iso()
        append_session_usage_log(
            s.usage_log_path,
            started_at=started,
            ended_at=ended,
            model=s.model,
            input_price_per_mtok=s.input_price_per_mtok,
            output_price_per_mtok=s.output_price_per_mtok,
            counts=counts,
        )

        return {
            "raw_text": raw_text,
            "usage_counts": counts,
            "model": s.model,
            "started_at": started,
            "ended_at": ended,
            "usage_log_path": str(s.usage_log_path),
        }
    except Exception:
        ended = _now_iso()
        try:
            append_session_usage_log(
                s.usage_log_path,
                started_at=started,
                ended_at=ended,
                model=s.model,
                input_price_per_mtok=s.input_price_per_mtok,
                output_price_per_mtok=s.output_price_per_mtok,
                counts=counts,
            )
        except OSError:
            pass
        raise


__all__ = [
    "load_settings",
    "extract_text_blocks",
    "request_once",
    "append_session_usage_log",
]
