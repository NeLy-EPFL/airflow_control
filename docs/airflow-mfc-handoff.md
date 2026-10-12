# Airflow MFC: ThorSync logging + automated ball delivery (handoff)

Handoff notes for continuing on the Ubuntu experimental workstation.
Written 2026-10-12. Hardware state below was verified on the rig; everything
under "Next steps" is still to do.

## Rig overview

- **Windows PC**: ThorImage (2-photon) + ThorSync. Currently also runs
  Vögtlin **Get red-y** to control the MFCs. Goal: keep only ThorImage/ThorSync here.
- **Ubuntu PC**: behavior camera recording + ball tracking. Goal: move airflow
  control here.
- **Air MFC**: Vögtlin red-y smart *controller* **GSC-B4SA-BB23** (thermal mass flow
  controller, not an on/off solenoid). Full scale **2 L/min**. Label: analog
  **Out 4–20 mA**, **In 4–20 mA**.
- **CO2 MFC**: presumably same series, not yet wired for logging (check its label
  for full scale + signal type).
- Both MFCs share one **RS-485 bus**, connected via an **FTDI USB-serial adapter**
  (shows as COM4 "USB Serial Port", manufacturer FTDI, on Windows). Protocol is
  **Modbus RTU**.

## Done: airflow logged in ThorSync (verified)

### Wiring
MFC cablePAD terminal block → coax → ThorSync breakout BNC.

| MFC terminal | Connection |
|---|---|
| Output | coax centre |
| Signal GND | coax braid |
| — | **270 Ω** resistor across Output ↔ Signal GND (converts 4–20 mA → voltage) |
| 24 Vdc, Setpoint, BAM | not connected |

- DAQ input: **AI3** as labelled on the breakout box (box numbers from 0;
  ThorSync's channel list numbers from 1, so watch the off-by-one).
- Breakout switch for AI3: **SE**. DIFF caused 50 Hz hum (~1.6 V) on the
  neighbouring PID channel (AI5 on the box); SE fixed it.
- ThorSync channel: analog, ±10 V.
- Planned/ongoing tidy-up: resistor soldered across centre/braid inside the coax
  fan-out under heat-shrink, ferrules on the MFC end. Cable labelled "4–20 mA / 270 Ω".

### Signal
| Flow | Expected V | Measured |
|---|---|---|
| off | 1.08 | ~1.0 |
| 1 L/min | 3.24 | ~3.2 |
| 2 L/min | 5.40 | ~5 |

- Conversion: `flow_L_min = (V - 1.08) / 2.16` (270 Ω, 2 L/min FS).
- Onset detection: first rising crossing of ~1.5 V.
- At 2 L/min (100 % FS) the MFC oscillates. Experimental range is
  **0.8–1.8 L/min**, typically ~1.2 L/min is enough to move the ball.
- Galvo-position channel sits slightly above the others: pre-existing, unrelated.

## Next: move airflow control to Ubuntu (Python)

### Hardware change
Only one: move the **FTDI USB plug** from the Windows PC to the Ubuntu PC.
24 V supply, RS-485 bus, and the analog AI3 logging line stay as they are, so
ThorSync keeps recording the actual flow on the imaging clock.
USB ≤ ~5 m (active extension if needed); keep the adapter near the MFCs.

### Before unplugging from Windows (in Get red-y)
Record for each MFC:
- [ ] Modbus slave address
- [ ] Baud rate / parity / stop bits
- [ ] Setpoint source = **digital** (RS-485), not analog
- [ ] Gas, units, full scale
- [ ] Serial number (needed for Vögtlin support / manual version, driver valid from SN ≥ 110 000)

Keep Get red-y installed on Windows for occasional reconfiguration (plug adapter back).

### Ubuntu setup
- [ ] `lsusb` → FTDI device visible
- [ ] `ls /dev/serial/by-id/` → stable port path (use this, not `/dev/ttyUSB0`)
- [ ] If no `/dev/ttyUSB*` appears: cable may use a Vögtlin-specific USB PID →
      bind `ftdi_sio` via udev rule / `new_id`
- [ ] User in `dialout` group
- [ ] Python: `minimalmodbus` (or PyExpLabSys Vögtlin driver as reference)
- [ ] Look up registers in the digital communication manual: setpoint, measured
      flow, and whether setpoint writes go to **RAM or EEPROM** (ramping writes
      often; EEPROM has limited cycles)
- [ ] First test: read flow → set 0.8 L/min → read back → set 0, while watching
      AI3 in ThorSync on Windows

### Software design
Separate the MFC driver from the tracking logic.

```
camera → ball tracking → state machine → MFC driver (Modbus, /dev/serial/by-id/...)
                                             ↘ actual flow logged by ThorSync (AI3)
```

MFC driver (small class): `read_flow()`, `set_flow(l_min)`, `stop()`.
- Hard cap at **1.8 L/min** inside the driver.
- `stop()` on exit, exception (`try/finally`), and via watchdog if tracking frames stop.
- Log every setpoint change with a timestamp (intended vs. actual on AI3).

State machine:
```
WAIT_FOR_BALL_LOSS: ball absent for T_absent       → RAMP
RAMP:               start 0.8 L/min, +step every Δt, cap 1.8
                    ball at X_max (contact)        → set 0 → HOLD
                    at cap for too long            → set 0, alert
HOLD:               ball leaves X_max (pushed away) → RAMP
always:             on exit/crash → set 0
```
Timing: Modbus write ~10–20 ms + MFC response ~100s ms, fine for second-scale ramps.

Parameters to define: `T_absent`, ramp start/step/Δt, cap, `X_max` contact
threshold (+ hysteresis), max ramp duration.

### Open questions
- Does ball detection already run **live, frame by frame**, on the Ubuntu PC, or
  only offline? Live is required.
- How are behavior camera frames synced to ThorSync (camera trigger BNC → ThorSync)?
- CO2 MFC: wire its analog output the same way (resistor + coax + free AI in SE)?

### Fallback
If Linux can't talk to the adapter: small Python server on Windows owning COM4,
receiving setpoints from Ubuntu over ZeroMQ/TCP. Works, but adds a network hop
and a second process to keep alive.

## References
- Vögtlin red-y smart digital communication manual (Modbus registers):
  https://voegtlin.com/data/329-3042_en_manualsmart_digicom.pdf
- Get red-y 5 manual: https://voegtlin.com/data/329-3038_en_manualgetredy5.pdf
- cablePAD pinout: https://www.voegtlin.com/data/329-3024_ml_cablePAD.pdf
- PyExpLabSys Vögtlin driver (minimalmodbus, GSM/GSC/GSP/GSB):
  https://pyexplabsys.readthedocs.io/drivers-autogen-only/vogtlin.html
