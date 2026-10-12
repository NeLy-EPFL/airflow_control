# TODO

## Closed-loop ball delivery
- [ ] Get live behavior-camera frames from octacam. octacam has no per-frame
      plugin hook yet, and only one process can open the camera. Options:
      octacam shares frames with other processes (e.g. through shared memory),
      or the tracker runs as an octacam plugin.
- [ ] Reuse the existing ball tracking from
      [scapepp `twop`](https://github.com/NeLy-EPFL/scapepp/tree/twop), live
      and frame by frame.
- [ ] State machine driving the air MFC:
      ```
      WAIT_FOR_BALL_LOSS: ball absent for T_absent        → RAMP
      RAMP:               start at e.g. 0.8 L/min, +step every Δt, up to the cap
                          ball at X_max (contact)         → set 0 → HOLD
                          at the cap for too long         → set 0, alert
      HOLD:               ball leaves X_max (pushed away) → RAMP
      always:             on exit or crash                → set 0
      ```
      Plus a watchdog that sets 0 if tracking frames stop arriving.
- [ ] Define the parameters: `T_absent`, ramp start/step/Δt, ramp cap, `X_max`
      contact threshold + hysteresis, max ramp duration. Typical working range
      is 0.8–1.8 L/min; ~1.2 L/min moves the ball.
- [ ] Timing is not a concern for second-scale ramps: a Modbus write takes
      ~10–20 ms and the MFC responds within a few hundred ms.
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
