from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from web_tool.services import serial_bridge

router = APIRouter(tags=["serial"])


class SerialSendBody(BaseModel):
    line: str = ""


@router.get("/serial/ports")
def serial_ports() -> dict[str, list[str]]:
    return {"ports": serial_bridge.list_ports()}


class SerialConnectBody(BaseModel):
    port: str = ""
    baudrate: int = 1_000_000


@router.post("/serial/connect")
def serial_connect(body: SerialConnectBody) -> dict[str, object]:
    return serial_bridge.connect(body.port, body.baudrate)


@router.post("/serial/disconnect")
def serial_disconnect() -> dict[str, object]:
    return serial_bridge.disconnect()


@router.post("/serial/send")
def serial_send(body: SerialSendBody) -> dict[str, object]:
    r = serial_bridge.send_line(body.line)
    if r.get("status") != "ok":
        raise HTTPException(status_code=400, detail=str(r.get("message") or "发送失败"))
    return r


@router.websocket("/serial/stream")
async def serial_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            batch = await asyncio.to_thread(serial_bridge.pop_events_batch, 48, 0.15)
            if not batch:
                continue
            if len(batch) == 1:
                await websocket.send_text(json.dumps(batch[0], ensure_ascii=False))
            else:
                await websocket.send_text(
                    json.dumps({"type": "batch", "events": batch}, ensure_ascii=False)
                )
    except WebSocketDisconnect:
        pass
