#!/usr/bin/env python3
"""
串口协议测试脚本（下位机模拟器）
================================

用途
----
- 本脚本用于在“另一台设备”上模拟下位机，和上位机 `web_tool` 进行串口联调。
- 运行方式固定为：`python protocol_pid_test.py`
- 所有可调参数都在本文件首部常量区，不通过启动参数传入。

串口联调步骤（两端互连）
------------------------
1) 上位机运行 web_tool，连接一端串口（例如 `/dev/ttyUSB0`）。
2) 本脚本运行在另一端串口（例如 `/dev/ttyUSB1`，见下方 `SERIAL_PORT`）。
3) 上位机点击“连接设备 -> 启动设备”，即可看到曲线数据流。

协议速查（严格新协议）
----------------------
上位机 -> 下位机（本脚本接收）：
1. 启动并设置目标曲线
   `debug_pid_ai_tuning start [period,amplitude,offset]`
2. 修改 PID
   `set_pid[p,i,d]`
3. 修改目标曲线参数
   `set_param[period,amplitude,offset]`
4. 停止数据流
   `debug_pid_ai_tuning stop`

下位机 -> 上位机（本脚本发送）：
- `pid_tuning_param: timestamp,setpoint,input,pwm,error,p,i,d\\n`

字段说明建议
------------
- `timestamp`：秒（float，单调递增）
- `setpoint`：目标位置
- `input`：测量位置（可以叠加测量噪声）
- `pwm`：执行器当前输出（可理解为控制量）
- `error`：`setpoint - input`
- `p,i,d`：当前生效 PID 参数

给真实下位机开发者的最小实现提示
------------------------------
你只需要实现下面这条主循环就能和上位机对齐调试：
1) 循环读取串口命令；
2) 命令命中 `start/set_pid/set_param/stop` 时更新内部状态；
3) 运行中按固定周期输出 `pid_tuning_param: ...`；
4) 停止后不再发送数据（直到下一次 `start`）。
"""

from __future__ import annotations

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

# =========================
# 固定配置（只改这里）
# =========================
SERIAL_PORT = "/dev/ttyUSB1"
SERIAL_BAUD = 1_000_000
SERIAL_TIMEOUT_S = 0.25
SERIAL_WRITE_TIMEOUT_S = 2.0
VERBOSE = True

# 发送频率（建议 100~1000Hz）
SIM_DT = 0.005  # 200Hz

# 默认目标曲线参数（被 start/set_param 覆盖）
DEFAULT_PERIOD = 4.0
DEFAULT_AMPLITUDE = 10.0
DEFAULT_OFFSET = 50.0

# 默认 PID（被 set_pid 覆盖）
DEFAULT_P = 0.8
DEFAULT_I = 0.0
DEFAULT_D = 0.0

# 电机近似模型参数（可按设备手感调整）
MOTOR_GAIN = 36.0
MOTOR_DAMPING = 4.6
COULOMB_FRICTION = 0.65
STATIC_FRICTION = 0.95
STICK_VEL_EPS = 0.02
ACTUATOR_TAU = 0.04
DRIVER_DEADZONE = 0.35
PWM_LIMIT = 100.0
SPEED_LIMIT = 200.0
INTEGRAL_LIMIT = 300.0

# 扰动与测量噪声
LOAD_DISTURB_AMP = 1.6
LOAD_DISTURB_HZ = 0.62
TORQUE_NOISE_SIGMA = 0.45
MEASURE_NOISE_SIGMA = 0.08
NOISE_AR_ALPHA = 0.90

START_RE = re.compile(
    r"^\s*debug_pid_ai_tuning\s+start\s*\[\s*([^,\]]+)\s*,\s*([^,\]]+)\s*,\s*([^\]]+)\s*\]\s*$",
    re.IGNORECASE,
)
SET_PID_RE = re.compile(
    r"^\s*set_pid\s*\[\s*([^,\]]+)\s*,\s*([^,\]]+)\s*,\s*([^\]]+)\s*\]\s*$",
    re.IGNORECASE,
)
SET_PARAM_RE = re.compile(
    r"^\s*set_param\s*\[\s*([^,\]]+)\s*,\s*([^,\]]+)\s*,\s*([^\]]+)\s*\]\s*$",
    re.IGNORECASE,
)
STOP_RE = re.compile(r"^\s*debug_pid_ai_tuning\s+stop\s*$", re.IGNORECASE)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _target_sine(t: float, period: float, amp: float, offset: float) -> float:
    T = abs(period) if abs(period) > 1e-9 else 1e-9
    return offset + amp * math.sin((2.0 * math.pi * t) / T)


def _parse_triplet(regex: re.Pattern[str], line: str) -> tuple[float, float, float] | None:
    m = regex.match(line.strip())
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2)), float(m.group(3))
    except ValueError:
        return None


class FakeLowerController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cmd_q: queue.Queue[str] = queue.Queue()
        self._running = False

        self._period = DEFAULT_PERIOD
        self._amp = DEFAULT_AMPLITUDE
        self._offset = DEFAULT_OFFSET
        self._p = DEFAULT_P
        self._i = DEFAULT_I
        self._d = DEFAULT_D

        self._sim_t = 0.0
        self._y = 0.0
        self._v = 0.0
        self._u_act = 0.0
        self._int_e = 0.0
        self._prev_e: float | None = None
        self._noise_state = 0.0

    def push_line(self, line: str) -> None:
        self._cmd_q.put(line)

    def _log(self, text: str) -> None:
        if VERBOSE:
            print(text, file=sys.stderr)

    def _reset_plant_unlocked(self) -> None:
        self._sim_t = 0.0
        self._y = 0.0
        self._v = 0.0
        self._u_act = 0.0
        self._int_e = 0.0
        self._prev_e = None
        self._noise_state = 0.0

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
                if STOP_RE.match(line):
                    with self._lock:
                        self._running = False
                    self._log("[CMD] stop")
                    continue

                start_cmd = _parse_triplet(START_RE, line)
                if start_cmd is not None:
                    period, amp, off = start_cmd
                    with self._lock:
                        self._period = period
                        self._amp = amp
                        self._offset = off
                        self._reset_plant_unlocked()
                        self._running = True
                    self._log(f"[CMD] start period={period} amp={amp} offset={off}")
                    continue

                pid_cmd = _parse_triplet(SET_PID_RE, line)
                if pid_cmd is not None:
                    pp, ii, dd = pid_cmd
                    with self._lock:
                        self._p, self._i, self._d = pp, ii, dd
                    self._log(f"[CMD] set_pid p={pp} i={ii} d={dd}")
                    continue

                param_cmd = _parse_triplet(SET_PARAM_RE, line)
                if param_cmd is not None:
                    period, amp, off = param_cmd
                    with self._lock:
                        self._period = period
                        self._amp = amp
                        self._offset = off
                    self._log(f"[CMD] set_param period={period} amp={amp} offset={off}")
                    continue

                self._log(f"[CMD] ignore invalid: {line}")

    def step_and_format_line(self) -> str | None:
        self._drain_commands()
        with self._lock:
            if not self._running:
                return None

            dt = SIM_DT
            t = self._sim_t
            period, amp, off = self._period, self._amp, self._offset
            kp, ki, kd = self._p, self._i, self._d

            setpoint = _target_sine(t, period, amp, off)
            measured_input = self._y + random.gauss(0.0, MEASURE_NOISE_SIGMA)
            error = setpoint - measured_input

            self._int_e = _clamp(self._int_e + error * dt, -INTEGRAL_LIMIT, INTEGRAL_LIMIT)
            dedt = 0.0 if (self._prev_e is None or dt < 1e-12) else (error - self._prev_e) / dt
            self._prev_e = error

            u_pid = kp * error + ki * self._int_e + kd * dedt
            u_cmd = _clamp(u_pid, -PWM_LIMIT, PWM_LIMIT)
            if abs(u_cmd) < DRIVER_DEADZONE:
                u_cmd = 0.0

            # 执行器动态：命令不会瞬时到达电机
            alpha = _clamp(dt / max(ACTUATOR_TAU, 1e-6), 0.0, 1.0)
            self._u_act += alpha * (u_cmd - self._u_act)

            # 外界扰动：低频负载 + AR(1) 随机扭矩
            self._noise_state = NOISE_AR_ALPHA * self._noise_state + (
                1.0 - NOISE_AR_ALPHA
            ) * random.gauss(0.0, TORQUE_NOISE_SIGMA)
            load = LOAD_DISTURB_AMP * math.sin(2.0 * math.pi * LOAD_DISTURB_HZ * t)
            disturb = self._noise_state + load

            drive_force = MOTOR_GAIN * self._u_act + disturb

            # 静摩擦 + 粘滞阻尼 + 库仑摩擦
            if abs(self._v) < STICK_VEL_EPS and abs(drive_force) < STATIC_FRICTION:
                acc = 0.0
                self._v = 0.0
            else:
                if abs(self._v) > STICK_VEL_EPS:
                    sign_v = math.copysign(1.0, self._v)
                else:
                    sign_v = math.copysign(1.0, drive_force)
                friction = COULOMB_FRICTION * sign_v
                acc = drive_force - MOTOR_DAMPING * self._v - friction
                self._v = _clamp(self._v + acc * dt, -SPEED_LIMIT, SPEED_LIMIT)

            self._y += self._v * dt
            self._sim_t += dt

            inner = ",".join(
                f"{x:.6g}"
                for x in (
                    t,
                    setpoint,
                    measured_input,
                    self._u_act,
                    error,
                    kp,
                    ki,
                    kd,
                )
            )
            return f"pid_tuning_param: {inner}\n"


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
        now = time.monotonic()
        sleep_s = next_tick - now
        if sleep_s > 0:
            time.sleep(min(sleep_s, 0.5))
        elif sleep_s < -0.25:
            next_tick = now


def main() -> int:
    ctrl = FakeLowerController()
    stop = threading.Event()

    try:
        ser = serial.Serial(
            port=SERIAL_PORT,
            baudrate=SERIAL_BAUD,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=SERIAL_TIMEOUT_S,
            write_timeout=SERIAL_WRITE_TIMEOUT_S,
        )
    except (serial.SerialException, OSError) as e:
        print(f"无法打开串口 {SERIAL_PORT!r}: {e}", file=sys.stderr)
        return 1

    print(
        (
            "[protocol_pid_test] 已启动："
            f"port={SERIAL_PORT}, baud={SERIAL_BAUD}, dt={SIM_DT}s, "
            "strict_protocol=start[]/set_param[]/set_pid[]/stop"
        ),
        file=sys.stderr,
    )

    rth = threading.Thread(target=_reader_loop, args=(ser, ctrl), daemon=True)
    wth = threading.Thread(target=_writer_loop, args=(ser, ctrl, stop), daemon=True)
    rth.start()
    wth.start()

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("[protocol_pid_test] 退出中...", file=sys.stderr)
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
