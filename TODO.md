# TODO

## Closed-loop ball delivery
- [ ] Check the next 2P day's logs (`airflow_*.jsonl` + `track_*.jsonl`) with
      the 2026-10-23 settings (release_hold_s 5, release_dist 1,
      occluded_hold_s 20): every air onset during a recording should follow a
      ball pushed beyond 1 diameter or rolled back (no release of a ball last
      seen at the fly, no burst at arming), and a ball out of reach should get
      air within release_hold_s. Look at the takes where a covered ball held
      the full 20 s: covered, or really gone? Tune occluded_hold_s from that.
- [ ] First run on the rig with a fly: calibrate camera_LM in the live view,
      tune the ramp (start, step, interval, maximum) and the contact / release
      distances and times.
- [ ] Ramp: optionally hold the level while the ball is visibly approaching,
      and/or start the next ramp near the flow of the last delivery.
- [ ] Sync to ThorSync: the track log has the PC wall clock per frame; octacam's
      preview has no camera frame index. Line up with the recording via
      octacam's recording start time, or ask octacam to send the frame index.
- [ ] Ball vanished or repeated rests: alert the operator (sound,
      notification) so the ball can be replaced after the recording.
- [ ] Platform not found (moved further than its search window): offer a wider
      search to find it automatically instead of dragging the box.
- [ ] Guard against a ball template marked on empty background (it matches
      everywhere): e.g. refuse a template that scores high on frames without
      the ball.

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
