# GNSS/decoder timing accuracy and error budget

Status: engineering budget, not a claim of achieved accuracy. The legacy real
session audited on 2026-07-24 has no PPS capture, full NAV-PVT UTC, tAcc,
packet sequence, or raw decoder-tick ground truth in the telemetry series.

## Measurement architecture

Three timestamps are separate:

- `sensor_time_us`: ESP monotonic time captured at the sensor/parser.
- `gnss_time_ns`: NAV-PVT measurement epoch (UTC+nano), or PPS-disciplined
  mapping for IMU/fused state.
- `receive_time_ns`: server/network arrival time, monitoring only.

NTP/PTP may detect a server-clock fault or provide an explicitly marked
fallback. It must not overwrite either sensor or GNSS measurement time.
Decoder tick time is authoritative for decoder laps; TCP receive time is
retained only to measure transport latency.

## Error budget

The ranges below are design estimates pending oscilloscope/session
measurement. They must be replaced with p95/p99 measured values before an
acceptance claim.

| Source | Typical design range | How it is verified |
|---|---:|---|
| M10 TIMEPULSE edge relative to GNSS time | sub-us to tens of ns class, configuration/module dependent | TIMEPULSE configuration plus scope against reference PPS |
| ESP GPIO ISR capture jitter | 1–20 us expected under load | PPS fan-out to scope/logic analyser; histogram captured edges |
| ESP oscillator drift in holdover | board/temperature dependent, often ppm scale | fitted clock drift/residual and thermal test |
| IMU sample timestamp | FIFO/driver dependent, 0.1–several ms if host-read stamped | compare sensor FIFO timestamp to ESP capture |
| Standard single-frequency GNSS horizontal position | commonly metre scale and environment dependent | surveyed control/RTK comparison, not the smoothed line |
| Antenna-to-IMU lever arm | calibration error × yaw rate | surveyed antenna/IMU vector and turn tests |
| Gate geometry | survey error, image registration error | surveyed endpoints and directed crossing reference |
| Crossing interpolation/dynamics | sample rate, acceleration, yaw and model dependent | physical gate event replay |
| Decoder loop/transponder trigger | installation and proprietary detector dependent | repeated physical crossings against independent gate |
| 256 kHz decoder tick quantisation | 3.90625 us if 256000 Hz is confirmed | estimate tick frequency from long multi-lap captures |
| Decoder oscillator drift | unknown until measured | tick delta versus stable reference over hours |

Position error converts directly into crossing-time error along the direction
normal to the gate:

`time_error = normal_position_error / normal_speed`

| Normal speed | 1 ms distance | 0.5 m error | 1 m error | 3 m error |
|---:|---:|---:|---:|---:|
| 5 m/s | 0.5 cm | 100 ms | 200 ms | 600 ms |
| 10 m/s | 1 cm | 50 ms | 100 ms | 300 ms |
| 20 m/s | 2 cm | 25 ms | 50 ms | 150 ms |
| 30 m/s | 3 cm | 16.7 ms | 33.3 ms | 100 ms |

At 20 m/s, an independent ±1 ms result requires the combined normal position,
gate, lever-arm and dynamics error to be about ±2 cm. PPS fixes the clock
alignment problem; it does not turn ordinary M10 positions into centimetre
positions.

## Honest feasibility conclusion

An M10 PPS input is necessary and useful for clock synchronisation, IMU
alignment and stable holdover. Ordinary M10 positioning alone cannot support
a repeatable independent ±0.001 s crossing claim. A single accidental
sub-millisecond lap residual is not acceptance evidence.

For independent, repeatable ±1 ms timing, the minimum recommended upgrade is:

1. ZED-F9P or equivalent dual-frequency RTK rover.
2. A surveyed local RTK base or a correction service demonstrated reliable at
   the track.
3. A roof-mounted antenna with suitable ground plane.
4. PPS wired directly to an ESP32-S3 GPIO capture input.
5. Surveyed antenna/IMU lever-arm calibration.
6. Preferably an independent physical crossing sensor: optical gate,
   magnetic-loop pickup, or suitable RFID/loop event.

Until RTK or a physical crossing event is available, acceptance should be
statistical, initially around p95 20–50 ms against decoder ticks, and tightened
only after multiple held-out sessions/days support it. Report signed error,
MAE, RMSE, p95, p99, maximum and drift; never only the aggregate mean.

## Required wiring and capture work

- Confirm the exact M10-180C board exposes a TIMEPULSE/1PPS pad and its voltage.
- Select an unused ESP32-S3 input pin; the firmware feature remains disabled
  until `GNSS_PPS_PIN` is explicitly configured.
- Use a short ground-referenced wire (level shift/protection if required).
- Capture PPS and a debug toggle on a logic analyser during Wi-Fi and IMU load.
- Record raw decoder payload (`decoder_tick`) and `tcp_receive_time` together.

