from __future__ import annotations

from fastapi import APIRouter, HTTPException
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
    baudrate: int = 115200


@router.post("/serial/connect")
def serial_connect(body: SerialConnectBody) -> dict[str, str]:
    return serial_bridge.connect_stub(body.port, body.baudrate)


@router.post("/serial/disconnect")
def serial_disconnect() -> dict[str, str]:
    return serial_bridge.disconnect_stub()


@router.post("/serial/send")
def serial_send(body: SerialSendBody) -> dict[str, str]:
    _ = body.line
    raise HTTPException(status_code=501, detail="serial send not implemented (stub)")


@router.get("/serial/status")
def serial_status() -> dict[str, str]:
    return serial_bridge.status_stub()
