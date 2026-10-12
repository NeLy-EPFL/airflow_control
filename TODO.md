# TODO

## Closed-loop ball delivery
- [ ] Get live behavior-camera frames from octacam. octacam has no per-frame
      plugin hook yet, and only one process can open the camera. Options:
      octacam shares frames with other processes (e.g. through shared memory),
      or the tracker runs as an octacam plugin.
- [ ] Reuse the existing ball tracking from
      [scapepp `twop`](https://github.com/NeLy-EPFL/scapepp/tree/twop), live
      and frame by frame.
- [ ] State machine (see `docs/airflow-mfc-handoff.md`):
      WAIT_FOR_BALL_LOSS → RAMP → HOLD, set 0 on exit or crash, and a watchdog
      that stops the flow if tracking frames stop.
- [ ] Define the parameters: `T_absent`, ramp start/step/Δt, ramp cap, `X_max`
      contact threshold + hysteresis, max ramp duration.
- [ ] Decide how behavior frames are synced to ThorSync (camera trigger → ThorSync).

## CO2 (2P rig)
- [ ] Not tested on the rig yet (not needed for current experiments).
- [ ] The CO2 MFC reports its gas as "Air \*". Check its calibration and gas
      table: an air-calibrated MFC measuring CO2 reads off by the gas
      conversion factor.
- [ ] Optionally wire its analog output to a free ThorSync AI the same way as
      air (270 Ω shunt, SE).

## Package
- [ ] Commands to set the control mode (0x000E) and save mode (0x4050), so
      Get red-y isn't needed for those either.
- [ ] Test with flow meters (GSM) and with non-PDM-U RS-485 adapters.
- [ ] CI (GitHub Actions: ruff + pytest on Linux/macOS/Windows).
