from __future__ import annotations

import os
from pathlib import Path


def _package_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def static_dir() -> Path:
    return _package_dir() / "static"


def default_host() -> str:
    return os.environ.get("WEB_TOOL_HOST", "127.0.0.1").strip() or "127.0.0.1"


def default_port() -> int:
    raw = os.environ.get("WEB_TOOL_PORT", "").strip()
    if not raw:
        return 8765
    try:
        return max(1, min(65535, int(raw)))
    except ValueError:
        return 8765
