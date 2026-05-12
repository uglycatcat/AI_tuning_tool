from __future__ import annotations

from collections.abc import AsyncIterator


def list_ports() -> list[str]:
    """预留：未来使用 pyserial 工具列出端口。"""
    return []


def connect_stub(port: str = "", baudrate: int = 115200) -> dict[str, str]:
    return {
        "status": "stub",
        "message": "未实现真实连接",
        "port": port or "",
        "baudrate": str(int(baudrate)),
    }


def disconnect_stub() -> dict[str, str]:
    return {"status": "stub", "message": "未实现断开"}


def status_stub() -> dict[str, str]:
    return {"status": "disconnected", "message": "串口桥接为占位实现"}


async def stream_lines() -> AsyncIterator[str]:
    """
    预留：未来在后台读串口并通过 WebSocket 推送到前端。
    当前不产出任何行。
    """
    return
    yield ""  # pragma: no cover
