# Deployment: NeLy 2P rig

Notes on how airflow_control is set up on the NeLy two-photon rig, as a worked
example.

## Machines

- **Windows PC:** ThorImage + ThorSync. Get red-y stays installed for occasional
  device reconfiguration (move the USB adapter back to use it).
- **Ubuntu 24.04 behavior PC:** behavior cameras (octacam), ball tracking, and
  airflow_control. The Vögtlin PDM-U adapter is plugged in here.

## Devices

| address | name | SN     | reported gas | full scale | control mode | save mode | power-up alarm |
|---------|------|--------|--------------|------------|--------------|-----------|----------------|
| 1       | air  | 218385 | Air *        | 2.0 ln/min | 0 (auto)     | RAM       | on             |
| 2       | co2  | 218382 | Air *        | 2.0 ln/min | 0 (auto)     | RAM       | on             |

Model GSC-B4SA-BB23, firmware 1548, both on one RS-485 bus behind the PDM-U
(FTDI 0403:6001). Address 1 was identified as air by puffing it while watching
AI3. None of the device settings needed changing when control moved off Get red-y.

User config (`~/.config/airflow_control/config.toml`):

```toml
[bus]
port = "/dev/serial/by-id/usb-Voegtlin_Instruments_PDM-U_Modul-if00-port0"

[[mfc]]
name = "air"
label = "Air"
address = 1

[[mfc]]
name = "co2"
label = "CO2"
address = 2
```

## Flow recorded in ThorSync

Only the air MFC's analog output is recorded. On the MFC side, its connector
has three screw terminals: **Output**, **GND** and **Signal**.

| MFC terminal | Connection |
|--------------|------------|
| Output | jumper wire → BNC-to-screw-terminal adapter, centre terminal |
| GND    | jumper wire → BNC-to-screw-terminal adapter, shield terminal |
| Signal | not connected |

- A **270 Ω** resistor sits in the BNC adapter's screw terminals, across
  centre and shield, clamped together with the jumper wires. It turns the
  4–20 mA output into 1.08–5.40 V.
- The BNC goes straight to ThorSync's breakout box.
- DAQ input **AI3** as labelled on the breakout box. The box numbers inputs from 0
  while ThorSync's channel list numbers from 1, so mind the off-by-one.
- The breakout switch for AI3 is set to **SE**: DIFF put ~1.6 V of 50 Hz hum on
  the neighbouring PID channel. The ThorSync channel is analog, ±10 V.
- Conversion: `flow_L_min = (V − 1.08) / 2.16`. Onset detection: first rising
  crossing of ~1.5 V.
- Measured when this was set up:

  | Flow | Expected | Measured |
  |------|----------|----------|
  | off | 1.08 V | ~1.0 V |
  | 1 L/min | 3.24 V | ~3.2 V |
  | 2 L/min | 5.40 V | ~5 V |
- Typical experimental range is 0.8–1.8 L/min; about 1.2 L/min moves the ball.
