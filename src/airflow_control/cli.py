"""``airflow-control`` command line.

Setup: scan, init, set-address. Use: info, read, set, puff, stop, gui.
"""

from __future__ import annotations

import threading
import time
import webbrowser
from pathlib import Path
from typing import Annotated

import typer

from airflow_control import config as config_mod
from airflow_control.controller import build_rig
from airflow_control.mfc import Bus, RedySmartMFC, set_address

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Control Vögtlin red-y smart mass-flow controllers over RS-485 Modbus.",
)

ConfigOpt = Annotated[str | None, typer.Option("--config", "-c", help="Config TOML.")]
PortOpt = Annotated[
    str | None, typer.Option("--port", "-p", help="Serial port (default: config, else autodetect).")
]
BaudOpt = Annotated[int | None, typer.Option("--baudrate", help="Default: config, else 9600.")]
MFCOpt = Annotated[
    str | None, typer.Option("--mfc", "-m", help="MFC name (default: first configured).")
]


def _cfg(path):
    return config_mod.load(path)


def _bus(cfg, port, baudrate) -> Bus:
    return Bus(port or cfg.resolve_port(), baudrate=baudrate or cfg.baudrate)


def _scan(bus: Bus) -> list[RedySmartMFC]:
    typer.echo(f"scanning {bus.port} at {bus.baudrate} baud (addresses 1-247, ~15 s) ...")
    found = []
    for address in bus.scan():
        dev = RedySmartMFC(bus.instrument(address), address=address, lock=bus.lock)
        i = dev.info
        typer.echo(
            f"  address {address}: SN {i.serial_number}, {i.gas}, "
            f"full scale {i.full_scale:g} {i.unit}"
        )
        found.append(dev)
    if not found:
        typer.secho(
            "  no device answered: check power (24 Vdc), RS-485 wiring and baud rate",
            fg="yellow",
        )
    return found


@app.command()
def scan(config: ConfigOpt = None, port: PortOpt = None, baudrate: BaudOpt = None):
    """List the devices that answer on the bus."""
    _scan(_bus(_cfg(config), port, baudrate))


@app.command()
def init(
    port: PortOpt = None,
    baudrate: BaudOpt = None,
    output: Annotated[
        str | None, typer.Option("--output", "-o", help="Default: the user config file.")
    ] = None,
    force: Annotated[bool, typer.Option(help="Overwrite an existing config.")] = False,
):
    """Scan the bus and write a config with one [[mfc]] entry per device found."""
    out = Path(output).expanduser() if output else config_mod.user_config_path()
    if out.exists() and not force:
        typer.secho(f"{out} exists; pass --force to overwrite", fg="red")
        raise typer.Exit(1)
    bus = _bus(config_mod.Config(), port, baudrate)
    devices = _scan(bus)
    if not devices:
        raise typer.Exit(1)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(config_mod.render_config(bus.port, bus.baudrate, [d.info for d in devices]))
    typer.echo(f"wrote {out}; edit the MFC names/labels, then `airflow-control gui`")


@app.command("set-address")
def set_address_(
    old: Annotated[int, typer.Argument(help="Current address (factory default: 247).")],
    new: Annotated[int, typer.Argument(help="New address, 1-246.")],
    port: PortOpt = None,
    baudrate: BaudOpt = None,
    config: ConfigOpt = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
):
    """Change a device's Modbus address. Connect one new device at a time."""
    bus = _bus(_cfg(config), port, baudrate)
    if not bus.scan([old]):
        typer.secho(f"no device answers at address {old} on {bus.port}", fg="red")
        raise typer.Exit(1)
    i = RedySmartMFC(bus.instrument(old), address=old, lock=bus.lock).info
    typer.echo(f"address {old}: SN {i.serial_number}, {i.gas}, {i.full_scale:g} {i.unit}")
    if not yes:
        typer.confirm(f"change its address {old} -> {new}?", abort=True)
    set_address(bus, old, new)
    typer.echo(f"done: SN {i.serial_number} now answers at address {new}")


@app.command()
def info(config: ConfigOpt = None):
    """Print identity and configuration of each configured MFC."""
    rig = build_rig(_cfg(config), log_events=False)
    for c in rig.channels.values():
        i = c.mfc.info
        typer.echo(
            f"{c.name} ({c.label}) @{i.address}: SN {i.serial_number}, fw {i.sw_version}, "
            f"{i.gas}, full scale {i.full_scale:g} {i.unit}, cap {c.mfc.cap:g}, "
            f"control mode {i.control_mode}, save mode {i.save_mode} "
            f"({'EEPROM auto-save!' if i.save_mode else 'RAM'}), "
            f"power-up alarm {'on' if i.powerup_alarm_active else 'off'}"
        )
        if i.save_mode:
            typer.secho(
                "  warning: every setpoint write goes to EEPROM (limited cycles)", fg="yellow"
            )


@app.command()
def read(
    config: ConfigOpt = None,
    watch: Annotated[bool, typer.Option("--watch", "-w", help="Repeat at 5 Hz.")] = False,
):
    """Read flow / setpoint / valve of each MFC."""
    rig = build_rig(_cfg(config), log_events=False)
    try:
        while True:
            parts = []
            for c in rig.channels.values():
                r = c.mfc.read()
                parts.append(
                    f"{c.name}: flow {r.flow:6.3f}  set {r.setpoint:5.3f}  valve {r.valve:5.1f}%"
                )
            typer.echo("   ".join(parts))
            if not watch:
                return
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass


@app.command("set")
def set_(
    setpoint: Annotated[float, typer.Argument(help="Flow, in the device's unit (see `info`).")],
    mfc: MFCOpt = None,
    config: ConfigOpt = None,
):
    """Set a continuous flow. It stays on after the command exits."""
    cfg = _cfg(config)
    name = cfg.mfc(mfc).name
    rig = build_rig(cfg, names=[name])
    rig.channel(name).set_continuous(setpoint)
    if setpoint > 0:
        typer.secho(
            f"{name}: flow stays at {setpoint} {rig.channel(name).mfc.info.unit}; "
            "`airflow-control stop` to turn off",
            fg="yellow",
        )
    rig.events.close()


@app.command()
def puff(
    setpoint: Annotated[float, typer.Argument(help="Flow, in the device's unit (see `info`).")],
    duration_s: Annotated[float, typer.Argument(help="Duration in s.")],
    mfc: MFCOpt = None,
    config: ConfigOpt = None,
):
    """Puff: set a flow for DURATION_S seconds, then back to 0."""
    cfg = _cfg(config)
    name = cfg.mfc(mfc).name
    rig = build_rig(cfg, names=[name])
    try:
        rig.channel(name).set_continuous(setpoint)
        time.sleep(duration_s)
    finally:
        rig.close()  # sets 0 even on Ctrl-C


@app.command()
def stop(mfc: MFCOpt = None, config: ConfigOpt = None):
    """Set the flow to 0 (all MFCs unless --mfc is given)."""
    cfg = _cfg(config)
    rig = build_rig(cfg, names=[cfg.mfc(mfc).name] if mfc else None)
    rig.close()
    typer.echo(f"stopped: {', '.join(rig.channels)}")


@app.command()
def gui(
    config: ConfigOpt = None,
    host: Annotated[str | None, typer.Option(help="Bind address.")] = None,
    port: Annotated[int | None, typer.Option(help="HTTP port.")] = None,
    browser: Annotated[bool, typer.Option(help="Open a browser tab.")] = True,
    sim: Annotated[bool, typer.Option(help="Simulated MFCs, no hardware.")] = False,
):
    """Start the browser GUI. All MFCs are set to 0 when it exits."""
    import uvicorn

    from airflow_control.web.app import create_app

    cfg = _cfg(config)
    rig = build_rig(cfg, sim=sim)
    host = host or cfg.host
    port = port or cfg.gui_port
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"
    typer.echo(
        f"airflow_control GUI on {url}  ({'SIMULATED MFCs' if sim else f'log: {rig.events.path}'})"
    )
    if browser:  # give uvicorn a moment to bind before the tab loads
        threading.Timer(1.0, webbrowser.open, [url]).start()
    server: uvicorn.Server | None = None

    def request_exit():
        typer.echo("shutdown requested from the GUI")
        server.should_exit = True

    app_ = create_app(rig, request_exit=request_exit)
    server = uvicorn.Server(
        uvicorn.Config(app_, host=host, port=port, log_level="warning", timeout_graceful_shutdown=2)
    )
    try:
        server.run()
    finally:
        rig.close()
        typer.echo("all MFCs set to 0; bye")


def main():
    try:
        app()
    except (LookupError, FileNotFoundError, RuntimeError, ValueError, OSError) as e:
        typer.secho(f"error: {e}", fg="red", err=True)
        raise SystemExit(1) from None
