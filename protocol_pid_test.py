#!/usr/bin/env python3
"""
伪装下位机：通过串口与 web_tool 串口调试模式对话（见 web_tool/readme.md §2）。

典型用法（两根 USB 转串口互连：PC 上为 ttyUSB0 ↔ ttyUSB1）：
  - web_tool 连接 /dev/ttyUSB0
  - 本程序：  python protocol_pid_test.py --port /dev/ttyUSB1
            python3 protocol_pid_test.py --port /dev/ttyUSB1 --noise-sigma 0.5 --load-amp 0.35 --load-hz 0.65

协议（与 web_tool 一致）：
  接收：  debug_pid_ai_tuning start <周期> <振幅> <偏置>
         set_pid[p,i,d]
         debug_pid_ai_tuning stop
  发送：  pid_tuning_param[timestamp,setpoint,input,pwm,error,p,i,d]\\n

内部 plant 与前端虚拟 PID 对齐：位置误差经 PID 得到速度指令，速度一阶滞后跟上，
位置由速度积分；另加 AR(1) 噪声与低频正弦负载扰动。
"""

from __future__ import annotations

import argparse
import math
import queue
import random
import re
import sys
import threading
import time

try:
    import serial
except ImportError as e:  # pragma: no cover
    print("需要 pyserial：pip install pyserial", file=sys.stderr)
    raise SystemExit(1) from e

# 与 web_tool/static/js/app.js 中虚拟 PID 一致
SIM_DT = 0.005
TAU_V = 0.08

SET_PID_RE = re.compile(
    r"^\s*set_pid\s*\[\s*([^,\]]+)\s*,\s*([^,\]]+)\s*,\s*([^\]]+)\s*\]\s*$",
    re.IGNORECASE,
)


def _target_sine(t: float, period: float, amp: float, offset: float) -> float:
    T = abs(period) if abs(period) > 1e-9 else 1e-9
    return offset + amp * math.sin((2.0 * math.pi * t) / T)


def _parse_start(line: str) -> tuple[float, float, float] | None:
    prefix = "debug_pid_ai_tuning start"
    s = line.strip()
    if not s.lower().startswith(prefix.lower()):
        return None
    rest = s[len(prefix) :].strip()
    parts = rest.split()
    if len(parts) != 3:
        return None
    try:
        return float(parts[0]), float(parts[1]), float(parts[2])
    except ValueError:
        return None


def _parse_set_pid(line: str) -> tuple[float, float, float] | None:
    m = SET_PID_RE.match(line.strip())
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2)), float(m.group(3))
    except ValueError:
        return None


class FakeLowerController:
    def __init__(
        self,
        *,
        noise_sigma: float,
        load_amp: float,
        load_hz: float,
    ) -> None:
        self._lock = threading.Lock()
        self._cmd_q: queue.Queue[str] = queue.Queue()
        self._running = False
        self._period = 4.0
        self._amp = 10.0
        self._offset = 50.0
        self._p, self._i, self._d = 0.8, 0.0, 0.0
        self._sim_t = 0.0
        self._y = 0.0
        self._v = 0.0
        self._int_e = 0.0
        self._prev_e: float | None = None
        self._noise_state = 0.0
        self._noise_sigma = noise_sigma
        self._load_amp = load_amp
        self._load_hz = load_hz

    def push_line(self, line: str) -> None:
        self._cmd_q.put(line)

    def _drain_commands(self) -> None:
        while True:
            try:
                raw = self._cmd_q.get_nowait()
            except queue.Empty:
                break
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                low = line.lower()
                if low == "debug_pid_ai_tuning stop" or low.startswith(
                    "debug_pid_ai_tuning stop "
                ):
                    with self._lock:
                        self._running = False
                    continue
                sp = _parse_start(line)
                if sp is not None:
                    period, amp, off = sp
                    with self._lock:
                        self._period = period
                        self._amp = amp
                        self._offset = off
                        self._reset_plant_unlocked()
                        self._running = True
                    continue
                pid = _parse_set_pid(line)
                if pid is not None:
                    pp, ii, dd = pid
                    with self._lock:
                        self._p, self._i, self._d = pp, ii, dd
                    continue

    def _reset_plant_unlocked(self) -> None:
        self._sim_t = 0.0
        self._y = 0.0
        self._v = 0.0
        self._int_e = 0.0
        self._prev_e = None
        self._noise_state = 0.0

    def step_and_format_line(self) -> str | None:
        self._drain_commands()
        with self._lock:
            if not self._running:
                return None
            dt = SIM_DT
            t = self._sim_t
            period, amp, off = self._period, self._amp, self._offset
            kp, ki, kd = self._p, self._i, self._d

            r = _target_sine(t, period, amp, off)
            e = r - self._y
            self._int_e += e * dt
            if self._prev_e is None or dt < 1e-12:
                dedt = 0.0
            else:
                dedt = (e - self._prev_e) / dt
            self._prev_e = e

            v_cmd = kp * e + ki * self._int_e + kd * dedt

            # AR(1) 速度扰动 + 低频负载（使曲线略难跟，便于手调参）
            self._noise_state = 0.88 * self._noise_state + 0.12 * random.gauss(
                0.0, self._noise_sigma
            )
            load = self._load_amp * math.sin(2.0 * math.pi * self._load_hz * t)
            disturb_v = self._noise_state + load

            self._v += ((v_cmd - self._v) / TAU_V) * dt + disturb_v * dt
            self._y += self._v * dt
            self._sim_t += dt

            err_plot = r - self._y
            # 时间戳用秒（与 serial_bridge 在 |ts|<1e9 时的秒制一致，曲线横轴正常）
            ts = t
            inner = ",".join(
                f"{x:.6g}"
                for x in (
                    ts,
                    r,
                    self._y,
                    self._v,
                    err_plot,
                    kp,
                    ki,
                    kd,
                )
            )
            return f"pid_tuning_param[{inner}]\n"


def _reader_loop(ser: serial.Serial, ctrl: FakeLowerController) -> None:
    while True:
        try:
            raw = ser.readline()
        except serial.SerialException:
            break
        if not raw:
            continue
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            continue
        ctrl.push_line(text)


def _writer_loop(ser: serial.Serial, ctrl: FakeLowerController, stop: threading.Event) -> None:
    next_tick = time.monotonic()
    while not stop.is_set():
        line = ctrl.step_and_format_line()
        if line is not None:
            try:
                ser.write(line.encode("utf-8"))
            except (serial.SerialException, OSError):
                break
            next_tick += SIM_DT
        else:
            next_tick += SIM_DT
        now = time.monotonic()
        sleep_s = next_tick - now
        if sleep_s > 0:
            time.sleep(min(sleep_s, 0.5))
        elif sleep_s < -0.25:
            # 落后太多则重新对齐，避免恶性循环
            next_tick = now


def main() -> int:
    ap = argparse.ArgumentParser(description="串口协议下位机模拟（配合 web_tool 串口调试）")
    ap.add_argument(
        "--port",
        default="/dev/ttyUSB1",
        help="与 web_tool 相对的另一端串口（默认 /dev/ttyUSB1）",
    )
    ap.add_argument("--baud", type=int, default=1_000_000, help="波特率，默认 1000000")
    ap.add_argument(
        "--noise-sigma",
        type=float,
        default=0.35,
        help="AR(1) 扰动高斯强度（速度量纲，默认 0.35）",
    )
    ap.add_argument(
        "--load-amp",
        type=float,
        default=0.22,
        help="正弦负载扰动幅值（速度量纲/步，默认 0.22）",
    )
    ap.add_argument("--load-hz", type=float, default=0.65, help="负载扰动频率 Hz（默认 0.65）")
    ap.add_argument("-v", "--verbose", action="store_true", help="stderr 简要日志")
    args = ap.parse_args()

    ctrl = FakeLowerController(
        noise_sigma=args.noise_sigma,
        load_amp=args.load_amp,
        load_hz=args.load_hz,
    )
    stop = threading.Event()

    try:
        ser = serial.Serial(
            port=args.port,
            baudrate=args.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.25,
            write_timeout=2.0,
        )
    except (serial.SerialException, OSError) as e:
        print(f"无法打开串口 {args.port!r}: {e}", file=sys.stderr)
        return 1

    if args.verbose:
        print(f"已打开 {args.port} @ {args.baud}，等待 debug_pid_ai_tuning start …", file=sys.stderr)

    rth = threading.Thread(target=_reader_loop, args=(ser, ctrl), daemon=True)
    wth = threading.Thread(target=_writer_loop, args=(ser, ctrl, stop), daemon=True)
    rth.start()
    wth.start()

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        if args.verbose:
            print("退出中…", file=sys.stderr)
    finally:
        stop.set()
        try:
            wth.join(timeout=1.0)
        except Exception:
            pass
        try:
            ser.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
