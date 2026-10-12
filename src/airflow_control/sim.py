"""Simulated red-y smart MFC for running the GUI and tests without hardware."""

from __future__ import annotations

import math
import random
import struct
import time

from airflow_control import mfc as m


def _regs(value: float) -> list[int]:
    return list(struct.unpack(">HH", struct.pack(">f", value)))


class SimInstrument:
    """Implements the ``mfc.Instrument`` protocol in memory.

    With ``tau_s > 0`` the measured flow follows the setpoint as a first-order
    lag plus a little noise; with ``tau_s = 0`` it tracks it exactly (tests).
    """

    def __init__(self, *, full_scale=2.0, unit="ln/min", serial_number=0, tau_s=0.0, noise=0.0):
        self.unit = unit
        self.setpoint = 0.0
        self.full_scale = full_scale
        self.serial_number = serial_number
        self.tau_s = tau_s
        self.noise = noise
        self.writes: list[float] = []
        self.fail_writes = 0  # fail the next N writes with OSError
        self._flow = 0.0
        self._t = time.monotonic()

    def _measured(self) -> float:
        if self.tau_s <= 0:
            return self.setpoint
        now = time.monotonic()
        self._flow += (self.setpoint - self._flow) * (1 - math.exp(-(now - self._t) / self.tau_s))
        self._t = now
        return max(0.0, self._flow + random.gauss(0, self.noise))

    def read_registers(self, registeraddress, number_of_registers):
        assert registeraddress == m.REG_FLOW
        flow = self._measured()
        regs = [0] * number_of_registers
        regs[0:2] = _regs(flow)
        regs[2:4] = _regs(24.0)
        regs[6:8] = _regs(self.setpoint)
        regs[10:12] = _regs(min(100.0, flow / self.full_scale * 100))
        return regs

    def read_register(self, registeraddress):
        return {
            m.REG_SW_VERSION: 1548,
            m.REG_CONTROL_MODE: 0,
            m.REG_SAVE_MODE: 0,
            m.REG_POWERUP_ALARM: 0,
        }[registeraddress]

    def read_float(self, registeraddress):
        if registeraddress == m.REG_FULL_SCALE:
            return self.full_scale
        assert registeraddress == m.REG_FLOW
        return self._measured()

    def read_long(self, registeraddress):
        return self.serial_number

    def read_string(self, registeraddress, number_of_registers):
        return {m.REG_UNIT: self.unit, m.REG_GAS: "Air *\x00\x00\x00"}[registeraddress]

    def write_float(self, registeraddress, value):
        assert registeraddress == m.REG_SETPOINT
        if self.fail_writes:
            self.fail_writes -= 1
            raise OSError("simulated serial failure")
        self._measured()  # advance the lag up to now before the step
        self.setpoint = value
        self.writes.append(value)


def sim_mfc(name="mfc", address=1, cap=None, **kw) -> m.RedySmartMFC:
    return m.RedySmartMFC(SimInstrument(**kw), address=address, name=name, cap=cap)
