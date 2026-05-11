#!/usr/bin/env python3
"""终端多轮对话：Anthropic 兼容 API（自定义 base_url）。"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "config.json"
DEFAULT_BASE_URL = "https://c.loonaai.cn"
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT = 120.0


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model: str
    max_tokens: int
    system: str | None
    timeout: float


def _script_dir() -> Path:
    return Path(__file__).resolve().parent


def _read_config() -> dict:
    path = _script_dir() / CONFIG_NAME
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"读取 {CONFIG_NAME} 失败: {e}", file=sys.stderr)
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


def _optional_system(env_name: str, data: dict) -> str | None:
    if os.environ.get(env_name) is not None:
        s = os.environ.get(env_name, "").strip()
        return s or None
    raw = data.get("SYSTEM")
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip()
        return s or None
    return None


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
    system = _optional_system("ANTHROPIC_SYSTEM", data)
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
    )


def list_models_cli(base_url: str, api_key: str) -> None:
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


def extract_text_blocks(message) -> str:
    parts: list[str] = []
    for block in message.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts).strip()


def main() -> None:
    from anthropic import Anthropic, APIConnectionError, APIStatusError

    s = load_settings()
    client = Anthropic(api_key=s.api_key, base_url=s.base_url, timeout=s.timeout)
    messages: list[dict] = []

    print("终端对话（quit / exit / Ctrl-D 退出）")
    extra = f" system=已设" if s.system else ""
    print(f"Base: {s.base_url}  Model: {s.model}  max_tokens={s.max_tokens}{extra}\n")

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
            kwargs: dict = {
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

        text = extract_text_blocks(resp)
        if not text:
            messages.pop()
            print("模型返回空内容，本轮未记入上下文。", file=sys.stderr)
            continue

        messages.append({"role": "assistant", "content": text})
        print(f"AI: {text}\n")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--list-models":
        cfg = load_settings()
        list_models_cli(cfg.base_url, cfg.api_key)
    else:
        main()
