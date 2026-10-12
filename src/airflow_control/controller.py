"""Manual flow control on top of the MFC driver: continuous setpoint + puffs.

A channel holds one *continuous* setpoint (0 = off). A puff temporarily
overrides it with a higher/lower flow for a fixed duration, then the channel
goes back to the continuous setpoint. Setting the continuous value, stopping,
or starting another puff cancels a running puff immediately.

Every setpoint write is appended to a JSONL event log with wall-clock and
monotonic timestamps, so the intended flow can be lined up against the actual
flow ThorSync records on AI3.
"""

from __future__ import annotations

import collections
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from airflow_control.mfc import Reading, RedySmartMFC

log = logging.getLogger("airflow_control")


class EventLog:
    """Thread-safe JSONL log of setpoint changes; also kept in memory for the GUI."""

    def __init__(self, log_dir: Path | None, keep: int = 200):
        self.path: Path | None = None
        self._f = None
        self._lock = threading.Lock()
        self.recent: collections.deque[dict] = collections.deque(maxlen=keep)
        if log_dir is not None:
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.path = log_dir / f"airflow_{stamp}.jsonl"
            self._f = open(self.path, "a", buffering=1)  # line-buffered

    def write(self, channel: str, event: str, **fields) -> dict:
        rec = {
            "t": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "mono": round(time.monotonic(), 4),
            "channel": channel,
            "event": event,
            **fields,
        }
        with self._lock:
            self.recent.append(rec)
            if self._f:
                self._f.write(json.dumps(rec) + "\n")
        return rec

    def close(self) -> None:
        with self._lock:
            if self._f:
                self._f.close()
                self._f = None


class Channel:
    """One MFC under manual control."""

    def __init__(
        self, mfc: RedySmartMFC, events: EventLog, *, label: str = "", max_puff_s: float = 30.0
    ):
        self.mfc = mfc
        self.name = mfc.name
        self.label = label or mfc.name
        self.events = events
        self.max_puff_s = max_puff_s
        self.continuous = 0.0
        self.reading: Reading | None = None
        # (t, flow, setpoint) for the GUI plot; ~60 s at 10 Hz
        self.history: collections.deque[tuple[float, float, float]] = collections.deque(maxlen=600)
        self.error: str | None = None  # last communication/safety error, for the GUI
        self._lock = threading.RLock()
        self._puff_cancel: threading.Event | None = None
        self._puff: dict | None = None  # {"setpoint", "duration_s", "t_end"}

    # --- writes -----------------------------------------------------------
    def _write(self, setpoint: float, event: str, **fields) -> None:
        self.mfc.set_flow(setpoint)
        self.events.write(self.name, event, setpoint=setpoint, **fields)

    def _cancel_puff(self) -> bool:
        if self._puff_cancel is None:
            return False
        self._puff_cancel.set()
        self._puff_cancel = None
        self._puff = None
        return True

    def set_continuous(self, setpoint: float) -> None:
        with self._lock:
            self._check(setpoint)
            cancelled = self._cancel_puff()
            self._write(setpoint, "continuous", **({"puff_cancelled": True} if cancelled else {}))
            self.continuous = setpoint
            self.error = None

    def puff(self, setpoint: float, duration_s: float) -> None:
        if not 0 < duration_s <= self.max_puff_s:
            raise ValueError(f"puff duration must be in (0, {self.max_puff_s}] s")
        with self._lock:
            self._check(setpoint)
            self._cancel_puff()
            cancel = threading.Event()
            self._write(setpoint, "puff_start", duration_s=duration_s)
            self._puff_cancel = cancel
            self._puff = {
                "setpoint": setpoint,
                "duration_s": duration_s,
                "t_end": time.time() + duration_s,
            }
            self.error = None
        threading.Thread(
            target=self._end_puff, args=(cancel, duration_s), name=f"puff-{self.name}", daemon=True
        ).start()

    def _end_puff(self, cancel: threading.Event, duration_s: float) -> None:
        if cancel.wait(duration_s):
            return  # whoever cancelled has already written the new setpoint
        with self._lock:
            if cancel.is_set():
                return
            self._puff_cancel = None
            self._puff = None
            try:
                self._write(self.continuous, "puff_end")
            except Exception as e:  # the flow is stuck at the puff value: escalate
                log.exception("%s: failed to end puff", self.name)
                self.error = f"failed to end puff ({e}); retrying stop"
                self._safe_stop("puff_end_failed")

    def stop(self) -> None:
        with self._lock:
            self._cancel_puff()
            self.continuous = 0.0
            self.mfc.stop()
            self.events.write(self.name, "stop", setpoint=0.0)
            self.error = None

    def _safe_stop(self, reason: str) -> None:
        """Stop without raising (used from shutdown and background threads)."""
        try:
            with self._lock:
                self._cancel_puff()
                self.continuous = 0.0
                self.mfc.stop(retries=5)
                self.events.write(self.name, "stop", setpoint=0.0, reason=reason)
        except Exception as e:
            log.exception("%s: STOP FAILED", self.name)
            self.error = f"STOP FAILED: {e}"

    def _check(self, setpoint: float) -> None:
        if not 0 <= setpoint <= self.mfc.cap:
            raise ValueError(
                f"{self.label}: {setpoint} {self.mfc.info.unit} outside [0, {self.mfc.cap:g}]"
            )

    # --- reads ------------------------------------------------------------
    def poll(self) -> None:
        try:
            self.reading = r = self.mfc.read()
            self.history.append((round(r.t, 3), r.flow, r.setpoint))
            if self.error and self.error.startswith("comm"):
                self.error = None
        except Exception as e:
            self.error = f"comm error: {e}"

    def status(self) -> dict:
        puff = self._puff
        return {
            "name": self.name,
            "label": self.label,
            "cap": self.mfc.cap,
            "max_puff_s": self.max_puff_s,
            "info": self.mfc.info.as_dict(),
            "continuous": self.continuous,
            "puff": None
            if puff is None
            else {**puff, "remaining_s": max(0.0, puff["t_end"] - time.time())},
            "reading": self.reading.as_dict() if self.reading else None,
            "error": self.error,
        }


class Rig:
    """All channels on the bus plus a background poller."""

    def __init__(self, channels: list[Channel], events: EventLog, poll_hz: float = 10.0):
        self.channels = {c.name: c for c in channels}
        self.events = events
        self.poll_hz = poll_hz
        self._stop_polling = threading.Event()
        self._poller: threading.Thread | None = None

    def channel(self, name: str) -> Channel:
        try:
            return self.channels[name]
        except KeyError:
            raise KeyError(f"no MFC named {name!r}") from None

    def start_polling(self) -> None:
        self._stop_polling.clear()
        self._poller = threading.Thread(target=self._poll_loop, name="poller", daemon=True)
        self._poller.start()

    def _poll_loop(self) -> None:
        period = 1.0 / self.poll_hz
        while not self._stop_polling.is_set():
            t0 = time.monotonic()
            for c in self.channels.values():
                c.poll()
            self._stop_polling.wait(max(0.0, period - (time.monotonic() - t0)))

    def stop_all(self, reason: str = "user") -> None:
        for c in self.channels.values():
            c._safe_stop(reason)

    def close(self) -> None:
        """Stop polling, set every MFC to 0, close the log. Idempotent."""
        self._stop_polling.set()
        if self._poller:
            self._poller.join(timeout=2)
            self._poller = None
        self.stop_all("shutdown")
        self.events.close()

    def status(self) -> dict:
        return {
            "channels": [c.status() for c in self.channels.values()],
            "events": list(self.events.recent)[-30:],
            "log_path": str(self.events.path) if self.events.path else None,
        }


def build_rig(
    cfg, *, log_events: bool = True, names: list[str] | None = None, sim: bool = False
) -> Rig:
    """Open the bus and every configured MFC (or only ``names``).

    ``sim=True`` uses simulated MFCs (no serial port) and logs nothing; with no
    MFC configured it simulates two 2 ln/min devices.
    """
    from airflow_control.config import MFCConfig
    from airflow_control.mfc import Bus, open_mfc
    from airflow_control.sim import sim_mfc

    if sim:
        bus = None
        mfcs = cfg.mfcs or [
            MFCConfig("sim1", 1, "Simulated 1"),
            MFCConfig("sim2", 2, "Simulated 2"),
        ]
    else:
        mfcs = cfg.require_mfcs()
        bus = Bus(cfg.resolve_port(), baudrate=cfg.baudrate, timeout_s=cfg.timeout_s)
    events = EventLog(cfg.log_dir if log_events and not sim else None)
    channels = []
    for m in mfcs:
        if names is not None and m.name not in names:
            continue
        if bus is None:
            mfc = sim_mfc(
                m.name, m.address, m.cap, tau_s=0.15, noise=0.004, serial_number=m.address
            )
        else:
            mfc = open_mfc(bus, m.address, name=m.name, cap=m.cap)
        channels.append(Channel(mfc, events, label=m.label, max_puff_s=cfg.max_puff_s))
    return Rig(channels, events, poll_hz=cfg.poll_hz)
