"""Vögtlin red-y smart mass-flow controller over Modbus RTU (RS-485).

Register map: red-y smart series II digital communication manual
(smart_digi_com_E1_5, section 1.14), valid for SMART4 firmware and up
(serial numbers >= 160000). Multi-register values are big-endian, hi-word
first, which is minimalmodbus' default byte order.

Several MFCs share one RS-485 bus behind one USB-serial adapter, so every
transaction goes through a single per-bus lock.
"""

from __future__ import annotations

import math
import struct
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

import minimalmodbus
import serial
from serial.tools import list_ports

REG_FLOW = 0x0000  # f32, measured flow [unit]
REG_TEMPERATURE = 0x0002  # f32, gas temperature [°C]
REG_SETPOINT = 0x0006  # f32, active setpoint [unit]
REG_VALVE = 0x000A  # f32, valve control signal [%]
REG_ALARMS = 0x000C  # u16 bitfield
REG_HW_ERRORS = 0x000D  # u16 bitfield
REG_CONTROL_MODE = 0x000E  # u16, 0 = automatic, 1 = digital, 2 = analog
REG_ADDRESS = 0x0013  # u16
REG_SERIAL = 0x001E  # u32
REG_SW_VERSION = 0x0021  # u16
REG_POWERUP_ALARM = 0x4040  # u16, 0 = active, 1 = inactive (sic)
REG_SAVE_MODE = 0x4050  # u16, 0 = manual, 1 = setpoint auto-saved to EEPROM
REG_FULL_SCALE = 0x6020  # f32, calibrated measuring range [unit]
REG_GAS = 0x6042  # s8
REG_UNIT = 0x6046  # s8

# Registers 0x0000..0x000D in one read: flow, temperature, totaliser, setpoint,
# analog input, valve, alarms, hardware errors.
_STATE_BLOCK = 14


class Instrument(Protocol):
    """The subset of ``minimalmodbus.Instrument`` the driver uses (fakeable)."""

    def read_registers(self, registeraddress: int, number_of_registers: int) -> list[int]: ...
    def read_register(self, registeraddress: int) -> int: ...
    def read_float(self, registeraddress: int) -> float: ...
    def read_long(self, registeraddress: int) -> int: ...
    def read_string(self, registeraddress: int, number_of_registers: int) -> str: ...
    def write_float(self, registeraddress: int, value: float) -> None: ...


def find_adapter() -> str:
    """Return the serial port of the (single) Vögtlin USB adapter, on any OS.

    Matches the USB manufacturer/product strings of the PDM-U cable. On Linux the
    stable ``/dev/serial/by-id/...`` path is returned when it exists.
    """
    ports = [
        p
        for p in list_ports.comports()
        if "voegtlin" in (p.manufacturer or "").lower()
        or "vögtlin" in (p.manufacturer or "").lower()
        or "pdm-u" in (p.product or p.description or "").lower()
    ]
    if not ports:
        usb = [f"{p.device} ({p.description})" for p in list_ports.comports() if p.vid]
        raise FileNotFoundError(
            "no Vögtlin USB adapter found. USB serial ports: "
            f"{', '.join(usb) or 'none'}. Pass --port or set [bus].port in the config."
        )
    if len(ports) > 1:
        raise RuntimeError(
            f"several Vögtlin adapters found ({', '.join(p.device for p in ports)}); "
            "pass --port or set [bus].port in the config"
        )
    device = ports[0].device
    by_id = Path("/dev/serial/by-id")
    if by_id.is_dir():
        for link in sorted(by_id.iterdir()):
            if link.resolve() == Path(device).resolve():
                return str(link)
    return device


class PortInUseError(RuntimeError):
    pass


def _lock_port(ser: serial.Serial, port: str) -> None:
    """Take an exclusive advisory lock on the open port (POSIX).

    Two programs polling the same RS-485 bus interleave their frames and both get
    garbage, so a second airflow_control (GUI, CLI) must fail cleanly instead.
    Windows already opens COM ports exclusively. Re-locking the same open file
    (minimalmodbus shares one Serial per port within a process) is a no-op.
    """
    try:
        import fcntl
    except ImportError:  # Windows
        return
    try:
        fcntl.flock(ser.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise PortInUseError(
            f"{port} is in use by another program (is the airflow_control GUI running?)"
        ) from None


class Bus:
    """One RS-485 bus (one serial port) shared by every MFC on it."""

    def __init__(self, port: str, *, baudrate: int = 9600, timeout_s: float = 0.2):
        self.port = port
        self.baudrate = baudrate
        self.timeout_s = timeout_s
        self.lock = threading.RLock()

    def instrument(self, address: int) -> minimalmodbus.Instrument:
        # minimalmodbus caches the serial.Serial object per port name, so all
        # instruments on this bus share one open port.
        inst = minimalmodbus.Instrument(self.port, address)
        inst.serial.baudrate = self.baudrate
        inst.serial.bytesize = 8
        inst.serial.parity = serial.PARITY_NONE
        inst.serial.stopbits = 2  # red-y: 9600 8N2
        inst.serial.timeout = self.timeout_s
        inst.clear_buffers_before_each_transaction = True
        _lock_port(inst.serial, self.port)
        return inst

    def scan(self, addresses=range(1, 248), timeout_s: float = 0.05) -> list[int]:
        """Return the Modbus addresses that answer on this bus."""
        found = []
        with self.lock:
            for address in addresses:
                inst = self.instrument(address)
                inst.serial.timeout = timeout_s
                try:
                    inst.read_register(REG_ADDRESS)
                except (OSError, minimalmodbus.ModbusException, ValueError):
                    continue
                finally:
                    inst.serial.timeout = self.timeout_s
                found.append(address)
        return found


def set_address(bus: Bus, old: int, new: int) -> None:
    """Change a device's Modbus address (register 0x0013) and verify it answers.

    Only one device may answer at ``old``: devices leave the factory at 247, so
    connect and readdress them one at a time.
    """
    if not 1 <= new <= 246:
        raise ValueError("new address must be in 1..246")
    with bus.lock:
        if bus.scan([new]):
            raise RuntimeError(f"address {new} is already in use on the bus")
        try:
            bus.instrument(old).write_register(REG_ADDRESS, new, functioncode=6)
        except minimalmodbus.NoResponseError:
            pass  # the reply may already come from (or be dropped by) the new address
        time.sleep(0.2)
        if not bus.scan([new], timeout_s=0.3):
            raise RuntimeError(f"no device answers at {new} after readdressing")


@dataclass(frozen=True)
class MFCInfo:
    address: int
    serial_number: int
    sw_version: int
    full_scale: float
    unit: str
    gas: str
    control_mode: int
    save_mode: int
    powerup_alarm_active: bool

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Reading:
    t: float  # time.time() when the read completed
    flow: float
    setpoint: float
    temperature: float
    valve: float
    alarms: int
    hw_errors: int

    def as_dict(self) -> dict:
        return asdict(self)


def _f32(hi: int, lo: int) -> float:
    return struct.unpack(">f", struct.pack(">HH", hi, lo))[0]


class RedySmartMFC:
    """Driver for one red-y smart controller (e.g. GSC-B4SA-BB23).

    Flows are in the device's own unit (``info.unit``, e.g. ``ln/min``).
    ``set_flow`` refuses anything outside ``[0, cap]``; the cap defaults to, and
    is always limited to, the device's calibrated full scale.
    """

    def __init__(
        self,
        instrument: Instrument,
        *,
        address: int,
        name: str = "mfc",
        cap: float | None = None,
        lock: threading.RLock | None = None,
    ):
        self.inst = instrument
        self.address = address
        self.name = name
        self.lock = lock or threading.RLock()
        self.info = self.read_info()
        fs = self.info.full_scale
        self.cap = fs if cap is None else min(cap, fs)

    def read_info(self) -> MFCInfo:
        with self.lock:
            return MFCInfo(
                address=self.address,
                serial_number=self.inst.read_long(REG_SERIAL),
                sw_version=self.inst.read_register(REG_SW_VERSION),
                full_scale=self.inst.read_float(REG_FULL_SCALE),
                unit=self.inst.read_string(REG_UNIT, 4).rstrip("\x00 "),
                gas=self.inst.read_string(REG_GAS, 4).rstrip("\x00 "),
                control_mode=self.inst.read_register(REG_CONTROL_MODE),
                save_mode=self.inst.read_register(REG_SAVE_MODE),
                powerup_alarm_active=self.inst.read_register(REG_POWERUP_ALARM) == 0,
            )

    def read(self) -> Reading:
        """Flow, setpoint, temperature, valve and alarms in one transaction."""
        with self.lock:
            r = self.inst.read_registers(REG_FLOW, _STATE_BLOCK)
        return Reading(
            t=time.time(),
            flow=_f32(r[0], r[1]),
            temperature=_f32(r[2], r[3]),
            setpoint=_f32(r[6], r[7]),
            valve=_f32(r[10], r[11]),
            alarms=r[12],
            hw_errors=r[13],
        )

    def read_flow(self) -> float:
        with self.lock:
            return self.inst.read_float(REG_FLOW)

    def set_flow(self, setpoint: float) -> float:
        setpoint = float(setpoint)
        if math.isnan(setpoint) or setpoint < 0 or setpoint > self.cap:
            raise ValueError(
                f"{self.name}: setpoint {setpoint} outside [0, {self.cap:g}] {self.info.unit}"
            )
        with self.lock:
            self.inst.write_float(REG_SETPOINT, setpoint)
        return setpoint

    def stop(self, retries: int = 3) -> None:
        """Set the flow to 0, retrying: this is the safety path."""
        for attempt in range(retries):
            try:
                self.set_flow(0.0)
                return
            except (OSError, minimalmodbus.ModbusException):
                if attempt == retries - 1:
                    raise
                time.sleep(0.05)


def open_mfc(bus: Bus, address: int, *, name: str, cap: float | None = None) -> RedySmartMFC:
    return RedySmartMFC(
        bus.instrument(address),
        address=address,
        name=name,
        cap=cap,
        lock=bus.lock,
    )
