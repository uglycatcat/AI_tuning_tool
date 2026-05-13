from __future__ import annotations

import os
from pathlib import Path

from tuning_tool.llm_settings import (
    read_root_config,
    web_tool_host_from_config,
    web_tool_port_from_config,
)


def _package_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def static_dir() -> Path:
    return _package_dir() / "static"


def default_host() -> str:
    """优先级：环境变量 WEB_TOOL_HOST > 项目根 config.json > 127.0.0.1"""
    env = os.environ.get("WEB_TOOL_HOST", "").strip()
    if env:
        return env
    data = read_root_config()
    cfg_host = web_tool_host_from_config(data)
    if cfg_host:
        return cfg_host
    return "127.0.0.1"


def default_port() -> int:
    """优先级：环境变量 WEB_TOOL_PORT > 项目根 config.json > 8765"""
    raw = os.environ.get("WEB_TOOL_PORT", "").strip()
    if raw:
        try:
            return max(1, min(65535, int(raw)))
        except ValueError:
            return 8765
    data = read_root_config()
    p = web_tool_port_from_config(data)
    if p is not None:
        return p
    return 8765
