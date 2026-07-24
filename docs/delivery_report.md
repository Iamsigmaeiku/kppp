# GNSS/INS position and timing delivery report

## Implemented

- Backward-compatible GPS packet v2 carries complete NAV-PVT UTC/nano/validity,
  tAcc and accuracy fields, velocity, ESP monotonic capture, packet sequence and
  PPS capture metadata.
- Server keeps sensor, GNSS measurement and receive timestamps separately.
- Auditable CRC/queue/ring/parser/UART/server/Influx counters and A-K gap
  evidence.
- Jump recovery state machine no longer promotes the fifth coherent flypoint
  to an anchor.
- Dynamic GNSS covariance and 6D position/velocity NIS gating; ESKF covariance
  symmetry/diagonal stabilization and corrected configured mounting/gravity.
- Fixed dual-IMU mixing is disabled by default; MPU is a fault fallback and the
  active/rejected source is recorded.
- PPS linear clock model with drift, residual, holdover and micros wrap tests.
- Directed finite gate LapTimer with arm/disarm, pit suppression, Hermite root
  finding, covariance uncertainty and explicit invalid quality.
- Decoder tick and TCP receive time are separate; tick wrap remains supported;
  GPS/decoder lap sequences use dynamic alignment.
- Exact WGS84/ECEF/ENU conversion and a versioned track model that refuses
  uncalibrated constraints.
- ML split leakage guard checks every available session/day/kart group.

All changed behaviors have rollback flags. Track constraint remains disabled
by default.

## Real replay baseline and acceptance table

Session: `sess-20260723-054723`.

| Metric | Legacy baseline | New firmware/session |
|---|---:|---:|
| GPS points | 5,263 | blocked: redeploy/capture required |
| median / p95 / p99 interval | 100 / 700 / 5,285.73 ms | blocked |
| gaps >1 s / >2 s | 74 / 73 | blocked |
| maximum gap | 19.846 s | blocked |
| implied speed p99 / max | 17.29 / 28.49 m/s | blocked |
| implied acceleration p99 | 12.94 m/s² | blocked |
| fused-to-raw separation p50 | 3.82 m | blocked |
| fused-to-raw separation p95 / p99 | 41.52 / 53.44 m | blocked |
| boundary-outside ratio | unavailable: no surveyed boundary | blocked |
| lap MAE/RMSE/p95/p99 | unavailable: legacy GNSS crossing epochs absent | blocked |
| PPS residual/age/drift | unavailable: PPS not wired | blocked |

The baseline overlay visibly contains fused infield-crossing segments, but the
quantitative separation above—not appearance—is the evidence. There is no
after claim until a v2 session is captured.

## Remaining engineering and data blockers

- Firmware delayed-state ESKF history/repropagation is not yet implemented;
  the existing postprocessor only supplies fixed-lag RTS behavior.
- Existing RTS remains CV, not CTRV/IMM. Long gaps are marked, but every
  consumer must render them as segmented/dashed uncertainty.
- The schema and safety gate for a formal track model exist, but no surveyed
  centerline/boundary/pit geometry was available, so HMM/Frenet production map
  matching remains disabled.
- The standalone deterministic LapTimer is not yet wired into live persistence.
- No controlled IMU static/temperature/Allan capture exists for bias, scale,
  misalignment, noise density or bias-instability calibration.
- No new packet-v2 session, RTK/survey truth, physical gate event or PPS capture
  exists. Exact A-G attribution and before/after accuracy are therefore blocked.
- Existing ML predicts whole-lap time and is not production-eligible. Only the
  group-leakage guard was added; residual labels and leave-day/kart evaluation
  require richer captures.

## Hardware wiring required

- Route M10 TIMEPULSE/1PPS to a verified 3.3 V-compatible ESP32-S3 GPIO; use a
  short ground-referenced connection and configure the correct edge/pulse.
- Put the GNSS antenna on the kart roof with a suitable ground plane; measure
  antenna-to-IMU lever arm and both IMU installation matrices.
- For independent repeatable millisecond-class timing, use F9P-class dual-band
  RTK with base/corrections and preferably a physical finish event (optical,
  loop/RFID or equivalent) captured by the ESP timer.
- Confirm USR-W610 raw tick byte width, rollover and nominal 256 kHz using a
  long, loss-free capture. TCP arrival time is not decoder event time.

See `timing_accuracy_report.md` for the error budget and
`deployment_and_rollback.md` for commands.
