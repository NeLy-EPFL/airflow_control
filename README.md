# airflow_control

Python control of Vögtlin **red-y smart** thermal mass-flow controllers over
their RS-485 digital interface (Modbus RTU), with a browser GUI and a command
line. It can replace Vögtlin's Windows *Get red-y* software for setting flows,
runs on Linux, macOS and Windows, and adds timed puffs. Optional plugins add to
the GUI; the first one tracks a ball in a behavior camera and runs a closed loop
that blows the ball to an animal on a platform (built for flies pushing a ball,
recorded with [octacam](https://github.com/NeLy-EPFL/octacam)).

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
- **Plugins** (off unless enabled): **closed-loop ball delivery**, ball tracking
  in a live camera view from octacam, and a ramp that blows the ball to the
  platform, turns the air off on contact, and starts again once the animal has
  pushed the ball away.
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

Each device needs:

- **24 Vdc power.**
- **RS-485 to the PC:** Vögtlin's PDM-U cable (USB) or any USB–RS-485 adapter.
  The bus can be wired 2-wire (half duplex) or 4-wire (full duplex; firmware
  updates need 4-wire), following §1.10 of the
  [digital communication manual](https://voegtlin.com/data/329-3042_en_manualsmart_digicom.pdf).
  Several devices share one bus by connecting them in parallel to the same lines.
- **Optional: the analog output** (and its ground), to record the measured flow
  on a DAQ. See [Recording the actual flow](#recording-the-actual-flow-optional).

Leave the analog setpoint input unconnected; the setpoint comes over RS-485.

How these signals come out of the device depends on the Vögtlin connection
accessory you use: a cable with loose leads, a breakout with screw terminals,
etc. Follow that accessory's datasheet and its terminal labels. For reference,
the red-y smart 9-pin D-sub carries (from the
[cablePAD datasheet](https://www.voegtlin.com/data/329-3024_ml_cablePAD.pdf)):

| Pin | Signal | | Pin | Signal |
|-----|--------|-|-----|--------|
| 1 | Analog ground | | 6 | RS-485 Tx+ |
| 2 | Supply ground | | 7 | RS-485 Tx− |
| 3 | Supply +24 Vdc | | 8 | RS-485 Rx− |
| 4 | Analog output (mA or V) | | 9 | RS-485 Rx+ |
| 5 | Analog setpoint input (controllers only) | | | |

Pins 1 and 2 are connected internally from hardware V6.03.

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
uv run airflow-control gui          # opens http://localhost:8766
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

## Plugin: closed-loop ball delivery

An optional plugin, off unless enabled: without it the GUI is plain MFC
control. When enabled, its panel sits above the MFC panels. It is for
experiments where an animal on a platform pushes a ball that air blows back to
it (e.g. a fly on a ball track). From a side view of the track:

```
         ball  ──────────────►  platform
        ( ◯ )                   ┌──────┐
  ═══════════════ rails ════════│      │
                                └──────┘
```

The loop drives one MFC:

1. **Ramp:** air on at a start flow, increased by a step at a fixed interval,
   until the ball touches the platform.
2. **Contact:** the ball is held at the platform for a short time → air off, so
   the animal can push.
3. **Pushed away:** the ball stays farther than a release distance (or out of
   view) for a while → back to 1, from the start flow. Arming (or resuming
   after a pause) with the ball within the release distance counts as contact,
   so a take does not start with a short burst of air.

4. **Rest:** if the ramp sits at its maximum for a while without contact (ball
   stuck, fallen off, or not tracked), air off for a rest, then back to 1.

Once armed, the loop never stops by itself: delivering the ball matters more
than saving air when something looks wrong. It stops only when you disarm, on
any manual action on its MFC (setpoint, puff, STOP), when the GUI exits, and
when the octacam recording it was armed during ends (*OFF · recording ended*;
arm again for the next recording). Armed outside a recording, e.g. for a test
with a brush, it runs until disarmed. If camera frames stop, the air goes off
(*paused*) until they return.

**Ball vanishing.** The ball normally leaves the view through the image edge it
comes from, rolling back down the track. A ball pushed away fast can cross the
view in two or three frames and is matched only weakly on its way out, so the
tracker follows it for a moment after its last confident sighting (weaker
matches count while they continue its path along the track) and extrapolates
its motion by one frame. If the ball vanishes anywhere else (the animal
covering it, or a fall), the loop waits up to 2 s: the ramp holds its flow and
a contact isn't counted as released, so a brief occlusion doesn't restart the
air. After that it carries on as if the ball had rolled back, and counts it
(*ball vanished n×* in the status). A ball that vanishes at the animal (last
seen within the release distance) while the air is off is most likely covered
by it: that wait is *at the fly* (20 s by default), so the air does not come on
while the animal still holds the ball. A ball that really fell off gets ramps
and rests until the recording ends (which disarms the loop) and you replace it.

### Tracking

The ball is found in each frame by template matching (normalized
cross-correlation), as in [scapepp](https://github.com/NeLy-EPFL/scapepp)'s
ball tracker: a sphere looks the same however it rolls, so a template cut from
one frame matches it anywhere along the track, and the match score tells
whether it is in view. The platform is located the same way, within a small
window around where it was marked. It may be partly out of view: its box can
extend past the image edge, and only the part in view is matched (both when
its template is cut and when it is searched for). The ball runs on fixed rails while the
platform can be moved between animals, so the contact position follows the
platform along the track and stays at the track's height. The ball's distance
to it is measured along the track, in ball diameters.

Frames come from a running **octacam GUI**: airflow_control connects to its
WebSocket as one more viewer and asks for one camera's preview (~30 Hz, at most
640 px, JPEG). octacam needs no change, and the camera must be grabbing (with an
external trigger: being triggered). octacam's state messages tell when a
recording starts and ends. Tracking takes 5–15 ms per frame.

### Setup

Enable it on the machine that runs it, with a `[plugins.ball_delivery]` table in
the config:

```toml
[plugins.ball_delivery]
camera = "camera_LM"            # octacam camera name with a side view of the track
octacam_url = "127.0.0.1:8765"  # where the octacam GUI runs
mfc = "air"                     # MFC that blows the ball
```

or for one run with `uv run airflow-control gui --camera camera_LM` (`--ball`
uses the configured camera, `--no-ball` turns a configured plugin off). The GUI
then shows a **Ball delivery** panel with the live camera view. Calibrate once
per camera view (it is saved under
`~/.config/airflow_control/ball_delivery/<camera>/` and kept across restarts).

**Once per setup**, without an animal, in the experiment's conditions
(lighting, covers in place). Templates are kept from animal to animal and day
to day, and NCC doesn't mind a ball that is lighter or dimmer:

1. Click **Ball** and drag a tight box around the whole ball (its width sets
   the ball diameter).
2. If the ball waits partly out of view between deliveries, click
   **+ Ball at edge** while it is there and drag a box around it, extending into
   the grey margin past the image edge. Without this, a ball mostly out of view
   isn't detected.
3. Click **Platform** and drag a box over a part of the platform that the ball
   and the animal's legs won't cover. If the platform runs out of the image,
   let the box extend into the grey margin over where it continues.
4. With the ball touching the platform, click **Set contact**. (Or, with no
   ball in view, **Set contact** puts a first guess next to the platform, to drag
   into place.) A dashed circle shows the contact position.
5. Check the readout: the ball should be *seen* with a score well above the
   threshold wherever it is, and *not seen* (score ~0.3–0.4) when it is gone.

**For each new animal**, nothing needs the ball near the animal:

- If the platform moved, drag the blue **platform box** onto it. Roughly is
  enough: the tracker searches around the box and snaps onto the platform. A
  small shift is followed without dragging; when the platform isn't found the
  box turns red and dashed (*platform not found*). The contact moves along.
  The box may be dragged partly past the image edge (its center stays in
  view) when the platform is mounted further out.
- If needed, drag the **dashed circle** to where the ball should touch.

Both can also be dragged while armed, during the recording. Re-marking a
template (**Ball**, **+ Ball at edge**, **Platform**) needs a disarm and keeps
the contact where it is.

Then **Arm**. The settings (ramp, contact and release distances and times,
score threshold) are applied with **Save**. The distance plot shows the ball's
distance to contact with the two thresholds.

The camera view is labelled for troubleshooting. Above the ball box it shows
how the loop classifies the ball: *at contact*, *near contact* (between the two
thresholds) or *away*, its distance in diameters, *at entry edge*, and the
match score. When there is no ball, it says whether the ball left through the
entry edge or *vanished on the track*. The top-right corner shows the loop
state and what it is waiting for, e.g. `CONTACT · air off · pushed away
0.4/1 s`, `RAMP 0.90 ln/min · ball missing 1.3/2 s` or `REST · air off · ramp
again in 6 s`.

Mark a template only when the ball is actually in the box: a template of empty
background matches everywhere.

### Testing without the rig

```bash
uv run airflow-control gui --sim --ball                  # simulated MFCs and ball
uv run airflow-control gui --sim --video recording.mp4   # track a recorded video
```

`--video` plays the file in real time at the preview resolution octacam would
send (needs `ffmpeg`), to calibrate and check the tracker on real images.

### Logs

Next to the setpoint log, each session writes `track_<date>_<time>.jsonl`: one
line per tracked frame with the ball position, score, distance, the platform
position, the loop state and flow. Loop state changes (`loop_ramp_start`,
`loop_step`, `loop_contact`, `loop_released`, `loop_paused`, `loop_rest`,
`loop_retry`, `loop_ball_vanished`, …)
appear in the setpoint log. Times are the PC's wall clock when the frame
arrived (the octacam preview carries its own frame counter, not the camera's
frame index).

## Recording the actual flow (optional)

The MFC's analog output reports the measured flow independently of the PC. This
lets an acquisition system (DAQ, ThorSync, …) record what the gas actually did,
on its own clock. Only two connections are used: the **analog output** and its
**ground** (D-sub pins 4 and 1, usually labelled *Output* and *GND* on
screw-terminal breakouts).

A 4–20 mA output must be turned into a voltage with a shunt resistor *R*
across Output and GND. A simple build, with the resistor in the terminals of a
BNC-to-screw-terminal adapter:

```
MFC screw terminals         BNC-to-screw-terminal adapter
  Output ── jumper wire ──── centre ──┬──┐
                                      R  ├── BNC cable ── DAQ analog input
  GND ───── jumper wire ──── shield ──┴──┘
```

```
V = I · R          flow = (V / R − 4 mA) / 16 mA × full scale
```

For example, R = 270 Ω gives 1.08 V at zero flow and 5.40 V at full scale. Pick
*R* so that 20 mA × R fits the DAQ input range. Devices with a voltage output
need no resistor. If a differential DAQ input picks up mains hum, try
single-ended.

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
  plugin.py      plugin interface: a panel, API routes, hooks
  frames.py      camera frame sources for plugins: octacam preview, video file
  ball_delivery/ the ball delivery plugin
    track.py     template-matching ball / platform tracker, calibration
    loop.py      closed-loop state machine, tracking pipeline, track log
    sim.py       simulated ball for --sim --ball
    static/      its panel (HTML fragment, script, styles)
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
