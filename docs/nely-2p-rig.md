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

## Ball delivery

The ball delivery plugin is enabled on this PC (in its user config). octacam
runs on the same PC (GUI on its default port 8765, airflow_control on
8766). The ball track is seen from the side by **camera_LM**: the ball comes
from the left, the fly and its platform are on the right. The front camera
looks at the ball from below through a mirror and is less precise.

```toml
[plugins.ball_delivery]
camera = "camera_LM"
octacam_url = "127.0.0.1:8765"
mfc = "air"
```

camera_LM is 960 × 580, so the preview is 480 × 290. On the recording
`261007_AllPPL1xCI80/Fly1/Recording4_Synced_Streaming` (metal ball on rails),
with these boxes in preview pixels (x0, x1, y0, y1):

| mark | box | taken when |
|---|---|---|
| Ball | 97, 248, 75, 228 | ball against the platform (~33–43 s) |
| Platform | 260, 360, 225, 290 | any frame (its lower-left block, below the fly's feet) |
| + Ball at edge | −50, 101, 75, 228 | ball waiting at the left edge (~22–31 s) |

the ball scored 0.75–1.0 whenever it was in view (also partly out of view at
the left edge) and ≤ 0.41 when gone. Its distance to contact was 0–0.12 ball
diameters against the platform and ~1.0 at the left edge, and the platform
stayed within 4 px. In that recording the ball drops below the rails at
~43 s and leaves the view. Replayed through the loop, it reached contact at
31.9 s (air off) and vanished at 43.4 s. The loop held until 45.4 s, then
carried on as if the ball had rolled back: ramp from 46.4 s, rest at the
maximum from 84.4 s, ramp again 10 s later, and so on until the end.

In a brush test on the rig (cover removed, so a darker image), flicking the
ball out of view to the left took 2–3 frames at 25 fps: its last confident
sighting was ~0.4 diameters from the edge, and the frames after it only matched
the ball at 0.45–0.60. Following the weak matches and the ball's speed, both
count as leaving through the entry edge. The templates had been cut with the
cover on: without it the ball scored only ~0.65 at the platform and wasn't
detected anywhere else, so calibrate in the conditions of the experiment.

First session with a fly (2026-10-13, `261013_THxCI80/Fly1`, six recordings):
17 deliveries reached contact, each with the ball center at the contact
position. The platform there runs out of the image at the right and bottom
(platform box 252, 480, 207, 290), which is why the platform box may now extend
past the image edge. After recording 005 ended, the loop, still armed, cycled
ramps and rests for 12 minutes until disarmed by hand; it now disarms at the
end of the recording. During several ramps in recordings 002 and 005 the ball
was not in view at all (best score ≤ 0.40), and the loop alternated ramps and
rests as intended.

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
