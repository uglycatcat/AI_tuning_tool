from __future__ import annotations

import queue
import threading
from typing import Any

import serial
from serial.tools import list_ports as list_ports_mod

from tuning_tool.llm_settings import read_root_config
from web_tool.services.protocol import parse_pid_tuning_line

_lock = threading.Lock()
_ser: serial.Serial | None = None
_reader: threading.Thread | None = None
_stop = threading.Event()
_out_q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=512)
_ts_scale: float | None = None


def _coerce_bool(v: object, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return default


def _serial_settings_from_config() -> dict[str, Any]:
    data = read_root_config()
    bs = int(data.get("SERIAL_BYTESIZE") or 8)
    bs = max(5, min(8, bs))
    size_map = {
        5: serial.FIVEBITS,
        6: serial.SIXBITS,
        7: serial.SEVENBITS,
        8: serial.EIGHTBITS,
    }
    byte_size = size_map.get(bs, serial.EIGHTBITS)
    stop = int(data.get("SERIAL_STOPBITS") or 1)
    if stop not in (1, 2):
        stop = 1
    parity_raw = data.get("SERIAL_PARITY")
    if parity_raw is None or str(parity_raw).strip().lower() in ("none", "null", ""):
        parity = serial.PARITY_NONE
    else:
        ch = str(parity_raw).strip().upper()[:1]
        parity = {
            "N": serial.PARITY_NONE,
            "E": serial.PARITY_EVEN,
            "O": serial.PARITY_ODD,
            "M": serial.PARITY_MARK,
            "S": serial.PARITY_SPACE,
        }.get(ch, serial.PARITY_NONE)
    return {
        "bytesize": byte_size,
        "parity": parity,
        "stopbits": serial.STOPBITS_ONE if stop == 1 else serial.STOPBITS_TWO,
        "xonxoff": _coerce_bool(data.get("SERIAL_XONXOFF"), False),
        "rtscts": _coerce_bool(data.get("SERIAL_RTSCTS"), False),
        "dsrdtr": _coerce_bool(data.get("SERIAL_DSRDTR"), False),
        "set_dtr_low": _coerce_bool(data.get("SERIAL_SET_DTR_LOW"), True),
        "set_rts_low": _coerce_bool(data.get("SERIAL_SET_RTS_LOW"), True),
    }


def _timestamp_scale(first_raw: float) -> float:
    """将下位机时间戳统一为秒：|ts|>=1e12 视为微秒，>=1e9 视为毫秒，否则视为秒。"""
    a = abs(first_raw)
    if a >= 1e12:
        return 1e-6
    if a >= 1e9:
        return 1e-3
    return 1.0


def list_ports() -> list[str]:
    try:
        return [p.device for p in list_ports_mod.comports()]
    except OSError:
        return []


def _enqueue(ev: dict[str, Any]) -> None:
    try:
        _out_q.put_nowait(ev)
    except queue.Full:
        try:
            _out_q.get_nowait()
        except queue.Empty:
            pass
        try:
            _out_q.put_nowait(ev)
        except queue.Full:
            pass


def _reader_loop() -> None:
    global _ts_scale
    with _lock:
        ser = _ser
    if ser is None:
        return
    while not _stop.is_set():
        try:
            raw = ser.readline()
        except serial.SerialException:
            _enqueue({"type": "io_error", "message": "串口读取异常"})
            break
        if not raw:
            continue
        try:
            line = raw.decode("utf-8", errors="replace")
        except Exception:
            continue
        parsed = parse_pid_tuning_line(line)
        if parsed is None:
            _enqueue({"type": "parse_error"})
            continue
        ts_raw = float(parsed["timestamp"])
        with _lock:
            if _ts_scale is None:
                _ts_scale = _timestamp_scale(ts_raw)
            scale = _ts_scale or 1.0
        t_sec = ts_raw * scale
        _enqueue(
            {
                "type": "sample",
                "t": t_sec,
                "setpoint": float(parsed["setpoint"]),
                "input": float(parsed["input"]),
                "error": float(parsed["error"]),
                "pwm": float(parsed["pwm"]),
                "p": float(parsed["p"]),
                "i": float(parsed["i"]),
                "d": float(parsed["d"]),
            }
        )


def _start_reader() -> None:
    global _reader
    t = threading.Thread(target=_reader_loop, name="serial_reader", daemon=True)
    _reader = t
    t.start()


def connect(port: str, baudrate: int) -> dict[str, Any]:
    global _ser, _ts_scale
    port = (port or "").strip()
    if not port:
        return {"status": "error", "message": "端口为空", "port": "", "baudrate": str(int(baudrate))}
    baud = int(baudrate)
    if baud <= 0:
        baud = 1_000_000
    cfg = _serial_settings_from_config()
    disconnect_unlocked()
    with _lock:
        try:
            ser = serial.Serial(
                port=port,
                baudrate=baud,
                bytesize=cfg["bytesize"],
                parity=cfg["parity"],
                stopbits=cfg["stopbits"],
                timeout=0.25,
                write_timeout=2.0,
                xonxoff=cfg["xonxoff"],
                rtscts=cfg["rtscts"],
                dsrdtr=cfg["dsrdtr"],
            )
        except (serial.SerialException, OSError, ValueError) as e:
            return {"status": "error", "message": str(e), "port": port, "baudrate": str(baud)}
        try:
            if cfg["set_dtr_low"] and hasattr(ser, "dtr"):
                ser.dtr = False
            if cfg["set_rts_low"] and hasattr(ser, "rts"):
                ser.rts = False
        except (AttributeError, serial.SerialException):
            pass
        _ser = ser
        _ts_scale = None
    _start_reader()
    return {"status": "ok", "message": "已连接", "port": port, "baudrate": str(baud)}


def disconnect_unlocked() -> None:
    global _ser, _reader, _ts_scale
    _stop.set()
    if _reader is not None:
        th = _reader
        _reader = None
        th.join(timeout=2.5)
    with _lock:
        if _ser is not None:
            try:
                _ser.close()
            except Exception:
                pass
            _ser = None
        _ts_scale = None
    while True:
        try:
            _out_q.get_nowait()
        except queue.Empty:
            break
    _stop.clear()


def disconnect() -> dict[str, Any]:
    disconnect_unlocked()
    return {"status": "ok", "message": "已断开", "port": "", "baudrate": "0"}


def send_line(line: str) -> dict[str, Any]:
    text = (line or "").strip()
    if not text:
        return {"status": "error", "message": "空行"}
    if not text.endswith("\n"):
        text = text + "\n"
    with _lock:
        if _ser is None or not _ser.is_open:
            return {"status": "error", "message": "串口未连接"}
        ser = _ser
    try:
        ser.write(text.encode("utf-8"))
    except (serial.SerialException, OSError, ValueError) as e:
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "message": "已发送"}


def pop_event(timeout: float) -> dict[str, Any] | None:
    try:
        return _out_q.get(timeout=timeout)
    except queue.Empty:
        return None
