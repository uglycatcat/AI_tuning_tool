#!/usr/bin/env python3
"""
Anthropic Messages 兼容网关的终端多轮对话。

配置优先级：同名环境变量 > 脚本目录下的 config.json > 本文件内默认值。
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# 常量（默认值可被 config / 环境变量覆盖）
# ---------------------------------------------------------------------------
CONFIG_NAME = "config.json"
DEFAULT_BASE_URL = "https://c.loonaai.cn"
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT = 120.0
DEFAULT_USAGE_LOG = "token_usage_sessions.jsonl"

# Anthropic usage 中可能出现的分项；用于累加与写 JSONL（网关可能返回缓存相关字段）
USAGE_COUNT_KEYS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
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


def _script_dir() -> Path:
    return Path(__file__).resolve().parent


def _read_config() -> dict[str, Any]:
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
        p = _script_dir() / p
    return p


def _optional_system(data: dict[str, Any]) -> str | None:
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


def load_settings() -> Settings:
    """合并 config.json 与环境变量，缺少密钥时退出进程。"""
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
    system = _optional_system(data)
    in_price = _optional_price(
        os.environ.get("MODEL_INPUT_PRICE_PER_MTOK", "")
        or data.get("MODEL_INPUT_PRICE_PER_MTOK")
    )
    out_price = _optional_price(
        os.environ.get("MODEL_OUTPUT_PRICE_PER_MTOK", "")
        or data.get("MODEL_OUTPUT_PRICE_PER_MTOK")
    )
    usage_log_path = _resolve_usage_log_path(data)
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


def extract_text_blocks(message: Any) -> str:
    """从 Messages 响应中拼接所有 text 块。"""
    parts: list[str] = []
    for block in message.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts).strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


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
    """仅用常规 input/output 单价估算；不含缓存阶梯价。"""
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
    """追加一行 JSON（UTF-8），记录本会话累计用量。"""
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
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)


def main() -> None:
    from anthropic import Anthropic, APIConnectionError, APIStatusError

    s = load_settings()
    session_started_at = _now_iso()
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

            u = _usage_counts(getattr(resp, "usage", None))
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
                ended_at=_now_iso(),
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
