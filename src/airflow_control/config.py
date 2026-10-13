"""Configuration: serial bus, the MFCs on it, GUI, logging and plugin settings.

Lookup order: ``--config PATH``, ``$AIRFLOW_CONTROL_CONFIG``, then the user
config file (:func:`user_config_path`). ``airflow-control init`` scans the bus
and writes that file. Without a config, the adapter is autodetected and no MFC
is configured.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from airflow_control.mfc import MFCInfo, find_adapter


def user_config_path() -> Path:
    """``~/.config/airflow_control/config.toml`` (``%APPDATA%\\...`` on Windows)."""
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"])
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "airflow_control" / "config.toml"


@dataclass
class MFCConfig:
    name: str  # short id used in the CLI and API, e.g. "air"
    address: int  # Modbus address on the bus
    label: str = ""  # shown in the GUI; defaults to the name
    cap: float | None = None  # max setpoint in the device's unit; None = full scale

    def __post_init__(self):
        self.label = self.label or self.name


@dataclass
class Config:
    port: str = ""  # empty = autodetect the Vögtlin USB adapter
    baudrate: int = 9600  # red-y factory setting (8N2)
    timeout_s: float = 0.2
    mfcs: list[MFCConfig] = field(default_factory=list)
    host: str = "127.0.0.1"
    gui_port: int = 8766  # octacam's GUI uses 8765
    poll_hz: float = 10.0
    max_puff_s: float = 30.0
    log_dir: Path = field(default_factory=lambda: Path.home() / "airflow_control_logs")
    # [plugins.<name>] tables, each parsed by its plugin; absent = plugin off
    plugins: dict[str, dict] = field(default_factory=dict)
    source: Path | None = None  # file this config was loaded from

    def resolve_port(self) -> str:
        return self.port or find_adapter()

    def require_mfcs(self) -> list[MFCConfig]:
        if not self.mfcs:
            where = self.source or user_config_path()
            raise LookupError(
                f"no MFC configured in {where}. Run `airflow-control init` to scan the "
                "bus and write a config."
            )
        return self.mfcs

    def mfc(self, name: str | None) -> MFCConfig:
        mfcs = self.require_mfcs()
        if name is None:
            return mfcs[0]
        for m in mfcs:
            if m.name == name:
                return m
        raise KeyError(f"no MFC named {name!r}; configured: {[m.name for m in mfcs]}")


def load(path: str | Path | None = None) -> Config:
    path = path or os.environ.get("AIRFLOW_CONTROL_CONFIG")
    if path is None:
        path = user_config_path()
        if not path.exists():
            return Config()
    path = Path(path).expanduser()
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    cfg = Config(source=path)
    bus = raw.get("bus", {})
    cfg.port = bus.get("port", cfg.port)
    cfg.baudrate = bus.get("baudrate", cfg.baudrate)
    cfg.timeout_s = bus.get("timeout_s", cfg.timeout_s)
    cfg.mfcs = [MFCConfig(**m) for m in raw.get("mfc", [])]
    gui = raw.get("gui", {})
    cfg.host = gui.get("host", cfg.host)
    cfg.gui_port = gui.get("port", cfg.gui_port)
    cfg.poll_hz = gui.get("poll_hz", cfg.poll_hz)
    cfg.max_puff_s = raw.get("puff", {}).get("max_duration_s", cfg.max_puff_s)
    if "dir" in raw.get("log", {}):
        cfg.log_dir = Path(raw["log"]["dir"]).expanduser()
    cfg.plugins = dict(raw.get("plugins", {}))
    if "tracking" in raw:  # before plugins existed
        raise ValueError(f"{path}: rename the [tracking] section to [plugins.ball_delivery]")
    names = [m.name for m in cfg.mfcs]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate MFC names in {path}: {names}")
    return cfg


def render_config(port: str, baudrate: int, devices: list[MFCInfo]) -> str:
    """A commented config TOML for the devices found by a bus scan."""
    lines = [
        "# airflow_control config, written by `airflow-control init`.",
        "# Rename each MFC (`name` is used by the CLI and API, `label` by the GUI)",
        "# and optionally lower `cap` (max setpoint, in the device's unit).",
        "",
        "[bus]",
        f'port = "{port}"',
        f"baudrate = {baudrate}",
        "",
    ]
    for d in devices:
        lines += [
            "[[mfc]]",
            f'name = "mfc{d.address}"',
            f'label = "MFC {d.address}"',
            f"address = {d.address}",
            f"cap = {d.full_scale:g}  # full scale: {d.full_scale:g} {d.unit}, "
            f"gas: {d.gas}, SN {d.serial_number}",
            "",
        ]
    lines += [
        "[gui]",
        'host = "127.0.0.1"  # "0.0.0.0" to reach the GUI from other machines',
        "port = 8766  # octacam uses 8765",
        "poll_hz = 10",
        "",
        "[puff]",
        "max_duration_s = 30",
        "",
        "[log]",
        'dir = "~/airflow_control_logs"',
        "",
        "# Optional plugin: closed-loop ball delivery from a running octacam GUI's preview",
        "# [plugins.ball_delivery]",
        '# camera = "camera_LM"           # octacam camera name',
        '# octacam_url = "127.0.0.1:8765"',
        '# mfc = "mfc1"                   # MFC that blows the ball',
        "",
    ]
    return "\n".join(lines)
