# Deployment: NeLy 2P rig

Notes on how airflow_control is set up on the NeLy two-photon rig, as a worked
example. [`airflow-mfc-handoff.md`](airflow-mfc-handoff.md) is the original
planning document. It is partly outdated: the 1.8 L/min cap there came from a
wiring fault and is now 2.0.

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

The air MFC's 4–20 mA analog output goes through a 270 Ω shunt to ThorSync.

| MFC terminal  | Connection |
|---------------|------------|
| Analog output | coax centre |
| Analog ground | coax braid |
| —             | 270 Ω across centre ↔ braid |

- DAQ input **AI3** as labelled on the breakout box. The box numbers inputs from 0
  while ThorSync's channel list numbers from 1, so mind the off-by-one.
- The breakout switch for AI3 is set to **SE**: DIFF put ~1.6 V of 50 Hz hum on
  the neighbouring PID channel. The ThorSync channel is analog, ±10 V.
- Conversion: `flow_L_min = (V − 1.08) / 2.16`. Onset detection: first rising
  crossing of ~1.5 V.
- Typical experimental range is 0.8–1.8 L/min; about 1.2 L/min moves the ball.
