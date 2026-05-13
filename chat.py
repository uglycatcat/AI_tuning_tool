#!/usr/bin/env python3
"""
Anthropic Messages 兼容网关的终端多轮对话。

配置优先级：同名环境变量 > 项目根目录 config.json > 本文件内默认值。
主要环境变量：ANTHROPIC_AUTH_TOKEN、ANTHROPIC_BASE_URL、ANTHROPIC_MODEL 等（见 load_settings）。

子命令：python chat.py --list-models  # 列出网关 /v1/models（仅需标准库，可不装 anthropic）

依赖：anthropic；可选 prompt-toolkit（改善中文/emoji 下行内退格）。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tuning_tool.llm_settings import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    USAGE_COUNT_KEYS,
    append_session_usage_log,
    coerce_float,
    coerce_int,
    extract_text_blocks,
    now_iso,
    optional_price,
    optional_system,
    project_root,
    read_root_config,
    resolve_usage_log_path,
    usage_counts,
)

_PT_FALLBACK_HINTED = False


@dataclass(frozen=True)
class Settings:
    """一次进程内只读的对话与日志配置。"""

    api_key: str
    base_url: str
    model: str
    max_tokens: int
    system: str | None
    timeout: float
    usage_log_path: Path
    input_price_per_mtok: float | None
    output_price_per_mtok: float | None


def load_settings() -> Settings:
    """合并 config.json 与环境变量，缺少密钥时退出进程。"""
    data = read_root_config(log_read_errors_to=sys.stderr)
    root = project_root()
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
    max_tokens = coerce_int(
        os.environ.get("ANTHROPIC_MAX_TOKENS", "") or data.get("MAX_TOKENS"),
        DEFAULT_MAX_TOKENS,
        min_v=1,
        max_v=200_000,
    )
    timeout = coerce_float(
        os.environ.get("ANTHROPIC_TIMEOUT", "") or data.get("REQUEST_TIMEOUT_SECONDS"),
        DEFAULT_TIMEOUT,
        min_v=5.0,
        max_v=600.0,
    )
    system = optional_system(data)
    in_price = optional_price(
        os.environ.get("MODEL_INPUT_PRICE_PER_MTOK", "")
        or data.get("MODEL_INPUT_PRICE_PER_MTOK")
    )
    out_price = optional_price(
        os.environ.get("MODEL_OUTPUT_PRICE_PER_MTOK", "")
        or data.get("MODEL_OUTPUT_PRICE_PER_MTOK")
    )
    usage_log_path = resolve_usage_log_path(data, root=root)
    if not api_key:
        print(
            "未找到 API 密钥：在 config.json 设置 LLM_API_KEY，"
            "或设置环境变量 ANTHROPIC_AUTH_TOKEN。",
            file=sys.stderr,
        )
        sys.exit(1)
    return Settings(
        api_key=api_key,
        base_url=base_url,
        model=model,
        max_tokens=max_tokens,
        system=system,
        timeout=timeout,
        usage_log_path=usage_log_path,
        input_price_per_mtok=in_price,
        output_price_per_mtok=out_price,
    )


def list_models_cli(base_url: str, api_key: str) -> None:
    """GET {base}/v1/models（Bearer），打印每行一个模型 id。"""
    url = f"{base_url.rstrip('/')}/v1/models"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        print(f"列出模型失败 HTTP {e.code}: {e.read().decode(errors='replace')}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"列出模型失败: {e}", file=sys.stderr)
        sys.exit(1)
    rows = payload.get("data") or []
    if not rows:
        print("(无模型条目)", file=sys.stderr)
        return
    for item in rows:
        mid = item.get("id")
        if mid:
            print(mid)


def _read_user_line(prompt_text: str = "你: ") -> str:
    """读一行用户输入；优先 prompt_toolkit 以改善宽字符行编辑。"""
    global _PT_FALLBACK_HINTED
    try:
        from prompt_toolkit.shortcuts import prompt as pt_prompt
    except ImportError:
        if not _PT_FALLBACK_HINTED:
            print(
                "[HINT] 未安装 prompt-toolkit 时，部分终端在中文/emoji 下退格可能错位；"
                "可执行: pip install 'prompt-toolkit>=3,<4' 后重试。",
                file=sys.stderr,
            )
            _PT_FALLBACK_HINTED = True
        return input(prompt_text).strip()
    try:
        return pt_prompt(prompt_text).strip()
    except (EOFError, KeyboardInterrupt):
        raise


def main() -> None:
    from anthropic import Anthropic, APIConnectionError, APIStatusError

    s = load_settings()
    session_started_at = now_iso()
    usage_totals = {k: 0 for k in USAGE_COUNT_KEYS}

    try:
        client = Anthropic(api_key=s.api_key, base_url=s.base_url, timeout=s.timeout)
        messages: list[dict[str, Any]] = []

        print("终端对话（quit / exit / Ctrl-D 退出）")
        extra = " system=已设" if s.system else ""
        print(f"Base: {s.base_url}  Model: {s.model}  max_tokens={s.max_tokens}{extra}\n")

        while True:
            try:
                line = _read_user_line("你: ")
            except (EOFError, KeyboardInterrupt):
                print("\n再见。")
                break
            if not line:
                continue
            if line.lower() in ("quit", "exit"):
                print("再见。")
                break

            messages.append({"role": "user", "content": line})
            try:
                kwargs: dict[str, Any] = {
                    "model": s.model,
                    "max_tokens": s.max_tokens,
                    "messages": messages,
                }
                if s.system:
                    kwargs["system"] = s.system
                resp = client.messages.create(**kwargs)
            except APIStatusError as e:
                messages.pop()
                print(f"API 错误 ({e.status_code}): {e.message}", file=sys.stderr)
                if e.status_code == 400 and "Invalid model" in str(e.message):
                    print(
                        f"提示: 调整 config.json 的 MODEL 或环境变量 ANTHROPIC_MODEL；"
                        f"或运行: python3 {Path(__file__).name} --list-models",
                        file=sys.stderr,
                    )
                continue
            except APIConnectionError as e:
                messages.pop()
                print(f"连接失败: {e}", file=sys.stderr)
                continue
            except Exception as e:  # noqa: BLE001
                messages.pop()
                print(f"请求异常: {e}", file=sys.stderr)
                continue

            u = usage_counts(getattr(resp, "usage", None))
            for k in usage_totals:
                usage_totals[k] += u[k]

            text = extract_text_blocks(resp)
            if not text:
                messages.pop()
                print("模型返回空内容，本轮未记入上下文。", file=sys.stderr)
                continue

            messages.append({"role": "assistant", "content": text})
            print(f"AI: {text}\n")

    finally:
        try:
            append_session_usage_log(
                s.usage_log_path,
                started_at=session_started_at,
                ended_at=now_iso(),
                model=s.model,
                input_price_per_mtok=s.input_price_per_mtok,
                output_price_per_mtok=s.output_price_per_mtok,
                counts=dict(usage_totals),
            )
            print(
                f"[INFO] 本会话 token 统计已追加写入: {s.usage_log_path}",
                file=sys.stderr,
            )
        except OSError as e:
            print(f"[WARN] 无法写入用量日志 {s.usage_log_path}: {e}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--list-models":
        cfg = load_settings()
        list_models_cli(cfg.base_url, cfg.api_key)
    else:
        main()
