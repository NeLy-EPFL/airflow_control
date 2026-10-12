# airflow_control

Python control of Vögtlin **red-y smart** thermal mass-flow controllers over
their RS-485 digital interface (Modbus RTU), with a browser GUI and a command
line. It can replace Vögtlin's Windows *Get red-y* software for setting flows,
runs on Linux, macOS and Windows, and adds timed puffs.

## Features

- **Continuous flow:** set a flow and hold it, from a slider or a number box.
- **Puffs:** a flow for a fixed duration, after which the MFC returns to the
  continuous setpoint (0 if none). Starting a new puff, changing the continuous
  value, or stopping cancels a running puff.
- **Live readout and plot** of measured flow, setpoint and valve opening for
  each MFC, with the last 30 s plotted.
- **Several MFCs on one bus**, each with its own name, label and cap. Units,
  ranges and step sizes follow each device's calibration (ln/min, ml/min, …).
- **Safety:** setpoints outside `[0, cap]` are refused; every MFC is set to 0
  when the program exits; only one program can use the bus at a time.
- **Setpoint log:** each setpoint change is written to a JSONL file with
  wall-clock and monotonic timestamps.
- **Setup tools:** scan the bus, write a config, and change device addresses
  without Get red-y.
- **Simulation mode** (`--sim`) to try the GUI without hardware.

## Requirements

- One or more red-y smart **controllers** (GSC/GSB series, SMART firmware 4+,
  serial number ≥ 160 000). Flow meters (GSM) are untested.
- A **24 Vdc** supply for the devices.
- An **RS-485 ↔ USB adapter**: Vögtlin's **PDM-U** interface cable (autodetected),
  or any USB–RS-485 adapter.
- [uv](https://docs.astral.sh/uv/). It installs a suitable Python (≥ 3.12) by
  itself.

## Setup

### 1. Wire the devices

Each red-y has a 9-pin D-sub connector for power, analog and digital signals.
With Vögtlin's *cablePAD* (PAD 328-2103, loose ends), the leads are:

| Pin | Colour | Signal (red-y smart) |
|-----|--------|----------------------|
| 1 | white  | Analog ground |
| 2 | brown  | Supply ground |
| 3 | green  | Supply +24 Vdc |
| 4 | yellow | Analog output (mA or V) |
| 5 | grey   | Analog setpoint input (controllers only) |
| 6 | pink   | RS-485 Tx+ |
| 7 | blue   | RS-485 Tx− |
| 8 | red    | RS-485 Rx− |
| 9 | black  | RS-485 Rx+ |

(Pins 1 and 2 are connected internally from hardware V6.03.) Older PAC/PDC
cables use different colours, so check the pin numbers, not the colours. Pinout:
[cablePAD datasheet](https://www.voegtlin.com/data/329-3024_ml_cablePAD.pdf).

- **Power:** 24 Vdc between pins 3 and 2.
- **RS-485:** the bus can be wired 2-wire (half duplex) or 4-wire (full duplex;
  firmware updates need 4-wire). Follow the wiring diagrams in §1.10 of the
  [digital communication manual](https://voegtlin.com/data/329-3042_en_manualsmart_digicom.pdf).
  With the PDM-U cable this wiring is already done. Several devices share one
  bus by connecting them in parallel to the same lines.
- **Analog setpoint (pin 5):** leave it unconnected. The setpoint comes over
  RS-485.

### 2. Give each device a unique address

Devices leave the factory at Modbus address **247**. Devices sharing a bus need
distinct addresses (1–246), and the bus can't tell two devices with the same
address apart. So connect **one device at a time** and readdress it:

```bash
uv run airflow-control scan                 # should list address 247
uv run airflow-control set-address 247 1    # then connect the next device → 2, …
```

### 3. Operating system

| OS | Serial port | Notes |
|---|---|---|
| Linux | `/dev/ttyUSB*`, stable `/dev/serial/by-id/...` | Add yourself to the `dialout` group (`sudo usermod -aG dialout $USER`, then log out and back in). FTDI adapters use the built-in `ftdi_sio` driver. |
| macOS | `/dev/cu.usbserial-*` | Built-in FTDI driver. |
| Windows | `COMx` | Uses the FTDI VCP driver (Windows Update usually installs it). Close Get red-y first: a COM port has one user at a time. |

The PDM-U is found automatically from its USB manufacturer/product strings.
For any other adapter, pass `--port` or set `[bus].port` in the config.

### 4. Install

```bash
git clone https://github.com/NeLy-EPFL/airflow_control.git
cd airflow_control
uv sync
```

### 5. Write the config

```bash
uv run airflow-control init     # scans the bus, writes the user config file
```

This writes one `[[mfc]]` entry per device it finds to
`~/.config/airflow_control/config.toml` (Windows: `%APPDATA%\airflow_control\config.toml`).
Edit it to give each MFC a `name` (used by the CLI and API, e.g. `air`) and a
`label` (shown in the GUI), and optionally a lower `cap`. All options are listed
in [`configs/example.toml`](configs/example.toml).

### 6. Check the devices

```bash
uv run airflow-control info
```

Two device settings matter. Both can be changed in Get red-y.

- **Control mode** must be `0` (automatic: a digital setpoint takes over
  from the analog input) or `1` (digital). In mode `2` (analog) the device
  ignores setpoints sent over RS-485.
- **Save mode** should be `RAM`. In `EEPROM auto-save` mode every setpoint is
  written to EEPROM, which survives about a million writes. `info` warns
  about this; frequent puffs would wear it out.

### 7. First run

```bash
uv run airflow-control gui --sim   # try the GUI without hardware
uv run airflow-control gui         # the real thing
```

Start with a small puff and check that the measured flow follows the setpoint.

## Usage

### Browser GUI

```bash
uv run airflow-control gui          # opens http://localhost:8765
```

Each MFC gets a card with a **Continuous** panel and a **Puff** panel side by
side, plus a live plot. The *step* selectors set the increment of the spinner
arrows, the arrow keys and the slider. Typed values don't have to match the
step.

| Control | Action |
|---|---|
| **Enter** in a field | apply it (continuous) / fire the puff |
| **STOP ALL** or **Esc** | set every MFC to 0 |
| **Quit** | set every MFC to 0 and stop the server (same as Ctrl-C) |

The GUI listens on `127.0.0.1` only. It has no authentication, so only set
`[gui].host = "0.0.0.0"` on a trusted network.

### Command line

```bash
uv run airflow-control info                   # SN, gas, full scale, device modes
uv run airflow-control read --watch           # live flow / setpoint / valve
uv run airflow-control set 1.2 -m air         # hold 1.2 (device unit); stays on after exit
uv run airflow-control puff 1.5 0.5 -m air    # 1.5 for 0.5 s, then 0
uv run airflow-control stop                   # every MFC to 0
```

Without `-m`, `set` and `puff` act on the first configured MFC. `stop` acts on
all of them unless `-m` is given, and `info` and `read` always show all of them.
Flows are always in the device's own unit, as shown by `info`.

Only one program can use the bus at a time. While the GUI is running, other
`airflow-control` commands report that the port is in use.

### Logs

Each GUI session writes `~/airflow_control_logs/airflow_<date>_<time>.jsonl`, one
line per setpoint change:

```json
{"t": "2026-10-12T11:37:24.573+02:00", "mono": 81234.5678, "channel": "air", "event": "puff_start", "setpoint": 1.6, "duration_s": 2}
```

## Recording the actual flow (optional)

The MFC's analog output (pin 4 against analog ground, pin 1) reports the measured
flow independently of the PC. This lets an acquisition system (DAQ,
ThorSync, …) record what the gas actually did, on its own clock. With a 4–20 mA
output, put a shunt resistor *R* across pins 4 and 1 and record the voltage:

```
V = I · R          flow = (V / R − 4 mA) / 16 mA × full scale
```

For example, R = 270 Ω gives 1.08 V at zero flow and 5.40 V at full scale. Pick
*R* so that 20 mA × R fits the DAQ input range. Devices with a voltage output
need no resistor. If a differential DAQ input picks up mains hum, try
single-ended, referenced to analog ground.

## How it works

- **Protocol:** Modbus RTU, 9600 baud, 8N2 (factory settings). Holding registers
  are read with function 3 and written with function 16.
- **Registers:** measured flow 0x0000, setpoint 0x0006, valve 0x000A, alarms
  0x000C/0x000D. The map is in
  [`src/airflow_control/mfc.py`](src/airflow_control/mfc.py), from the red-y smart
  [digital communication manual](https://voegtlin.com/data/329-3042_en_manualsmart_digicom.pdf).
- **Polling:** the GUI reads each MFC in one transaction (registers
  0x0000–0x000D) at `poll_hz`. A setpoint write takes ~10–20 ms, and the MFC
  responds within a few hundred ms.

```
src/airflow_control/
  mfc.py         Modbus driver: Bus, RedySmartMFC.read() / set_flow() / stop()
  controller.py  Channel (continuous + puff), Rig (poller, stop_all), EventLog
  config.py      config file loading and `init` output
  sim.py         simulated MFC (first-order lag) for --sim and the tests
  cli.py         command line (typer)
  web/           FastAPI app (REST + WebSocket) and the static GUI
```

Tested on Ubuntu 24.04 with GSC-B4SA-BB23 controllers (firmware 1548) and the
PDM-U adapter. [`docs/nely-2p-rig.md`](docs/nely-2p-rig.md) describes one
deployment.

## Development

```bash
uv run pytest
uv run ruff check src tests && uv run ruff format src tests
```

Contributions are welcome: open an issue or a pull request.
