#!/usr/bin/env python3
"""终端多轮对话：通过自定义 Anthropic 兼容 base_url 调用 Claude。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from anthropic import Anthropic, APIConnectionError, APIStatusError

DEFAULT_BASE_URL = "https://c.loonaai.cn"
DEFAULT_MODEL = "claude-3-5-sonnet-20241022"
CONFIG_NAME = "config.json"


def _script_dir() -> Path:
    return Path(__file__).resolve().parent


def load_settings() -> tuple[str, str, str]:
    """返回 (api_key, base_url, model_id)。"""
    base_url = os.environ.get("ANTHROPIC_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    model = os.environ.get("ANTHROPIC_MODEL", "").strip()

    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    config_path = _script_dir() / CONFIG_NAME
    if config_path.is_file():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"读取 {CONFIG_NAME} 失败: {e}", file=sys.stderr)
            data = {}
        if not api_key:
            api_key = str(data.get("LLM_API_KEY", "")).strip()
        if not model:
            model = str(data.get("MODEL", "")).strip()
    if not model:
        model = DEFAULT_MODEL
    if not api_key:
        print(
            "未找到 API 密钥：请在 config.json 中设置 LLM_API_KEY，"
            "或设置环境变量 ANTHROPIC_AUTH_TOKEN。",
            file=sys.stderr,
        )
        sys.exit(1)
    return api_key, base_url, model


def extract_text_blocks(message) -> str:
    parts: list[str] = []
    for block in message.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts).strip()


def main() -> None:
    api_key, base_url, model = load_settings()
    client = Anthropic(api_key=api_key, base_url=base_url)
    messages: list[dict] = []

    print("终端对话（输入 quit / exit 或 Ctrl-D 退出）")
    print(f"Base: {base_url}  Model: {model}\n")

    while True:
        try:
            line = input("你: ").strip()
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
            resp = client.messages.create(
                model=model,
                max_tokens=4096,
                messages=messages,
            )
        except APIStatusError as e:
            messages.pop()
            print(f"API 错误 ({e.status_code}): {e.message}", file=sys.stderr)
            continue
        except APIConnectionError as e:
            messages.pop()
            print(f"连接失败: {e}", file=sys.stderr)
            continue
        except Exception as e:  # noqa: BLE001 — 保持 REPL 不退出
            messages.pop()
            print(f"请求异常: {e}", file=sys.stderr)
            continue

        text = extract_text_blocks(resp)
        if not text:
            messages.pop()
            print("模型返回空内容，本轮未记入上下文。", file=sys.stderr)
            continue

        messages.append({"role": "assistant", "content": text})
        print(f"AI: {text}\n")


if __name__ == "__main__":
    main()
