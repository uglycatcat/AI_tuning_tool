#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性 AI 请求客户端（复用根目录 config.json 与用量日志语义）。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from tuning_tool.llm_settings import (
    USAGE_COUNT_KEYS,
    append_session_usage_log,
    extract_text_blocks,
    load_runtime_settings,
    now_iso,
    usage_counts,
)

DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_DELAY_SECONDS = 0.75


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


def load_settings() -> Settings:
    cfg = load_runtime_settings(include_retry=True)
    api_key = str(cfg["api_key"])

    if not api_key:
        raise RuntimeError(
            "未找到 API 密钥：在 config.json 设置 LLM_API_KEY，或设置环境变量 ANTHROPIC_AUTH_TOKEN。"
        )

    return Settings(
        api_key=api_key,
        base_url=str(cfg["base_url"]),
        model=str(cfg["model"]),
        max_tokens=int(cfg["max_tokens"]),
        timeout=float(cfg["timeout"]),
        usage_log_path=cfg["usage_log_path"],
        input_price_per_mtok=cfg.get("input_price_per_mtok"),
        output_price_per_mtok=cfg.get("output_price_per_mtok"),
        max_retries=int(cfg.get("max_retries", DEFAULT_MAX_RETRIES)),
        retry_base_delay_seconds=float(
            cfg.get("retry_base_delay_seconds", DEFAULT_RETRY_BASE_DELAY_SECONDS)
        ),
    )


def _http_status_retryable(status_code: int) -> bool:
    """网关多后端时 502（含无效 model slug 透传）、限流等与短暂不可用可短暂重试。"""
    return status_code in (408, 409, 429, 502, 503, 504)


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
    started = now_iso()
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

        usage = usage_counts(getattr(resp, "usage", None))
        for k in counts:
            counts[k] += usage[k]

        raw_text = extract_text_blocks(resp)
        if not raw_text:
            raise RuntimeError("模型返回空内容。")

        ended = now_iso()
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
        ended = now_iso()
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
    "request_once",
]
