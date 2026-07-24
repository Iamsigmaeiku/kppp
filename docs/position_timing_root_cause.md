# Position/timing root-cause audit

## Evidence inspected

Real Influx session `sess-20260723-054723` was replayed with:

```powershell
python scripts/audit_position_timing.py --session-id sess-20260723-054723 --plot
```

The audit read 20,954 rows: 13,582 `kart_telemetry`, 7,337
`dr_position`, and 35 decoder raw-event rows. It found 5,263 accepted legacy
GPS positions across 1,142.017 seconds.

| Metric | Legacy real session |
|---|---:|
| Median GPS interval | 100 ms |
| p95 interval | 700 ms |
| p99 interval | 5,285.7 ms |
| Maximum interval | 19.846 s |
| Intervals > 1 s | 74 |
| Intervals > 2 s | 73 |
| Implied speed p99 / max | 17.29 / 28.49 m/s |
| Implied acceleration p99 | 12.94 m/s² |
| Implied acceleration min / max | -186.18 / 170.30 m/s² |
| Satellite count p50 / p95 | 12 / 14 |
| Fused-vs-raw separation p50 | 3.82 m |
| Fused-vs-raw separation p95 / p99 | 41.52 / 53.44 m |

The extreme acceleration/yaw-rate values are computed from adjacent recorded
positions and are evidence of time gaps/noisy adjacent points, not vehicle
dynamics truth.

## Confirmed software causes

1. Legacy GPS frames contained only iTOW, position/velocity and limited
   accuracy fields. Full UTC, nano/tAcc/valid flags, ESP capture time and
   sequence were discarded.
2. Server time was reconstructed by anchoring iTOW to `time.time()` at packet
   arrival. Network latency therefore contaminated measurement time.
3. The legacy jump filter unconditionally accepted the fifth consecutive
   outlier as a new anchor. A coherent multipath cluster could therefore move
   the displayed line into trees/grass.
4. Rejected GPS frames retained only `gps_fresh=0` and satellite count.
   Their coordinates, iTOW and rejection reason were lost, preventing exact
   after-the-fact attribution.
5. The ESKF outlier test rejected a large position innovation only at low
   speed. A high-speed outlier bypassed that gate.
6. ICM and MPU data were mixed with fixed 0.85/0.15 weights even though no
   per-sensor noise covariance/Allan calibration justified those weights; the
   code also computed an inconsistency fault and still blended the faulty MPU.
7. The RTS implementation is a constant-velocity model and emits coordinates
   through long gaps. It marks a gap, but downstream rendering can still draw
   a solid line unless it honours that flag. No `track_smoothed` rows existed
   inside the audited session range.
8. The coordinate transform uses fixed metres-per-degree and a manually
   registered satellite image. Its translation/rotation/scale have not been
   surveyed, so image alignment is not positioning ground truth.
9. Decoder/GPS timebase matching used a fixed start-offset search. It could
   not robustly recover after an internal missed/duplicate crossing.
10. The recorded installation and web motion path use stationary `+Y≈1g`,
    while the ESKF treated the unrotated samples as a Z-axis-up installation.
    Its level-frame gravity sign was also inconsistent with NED specific force.
    This is consistent with the measured fused-vs-raw p95 separation of 41.52 m.

## What the legacy data cannot decide

The 73 gaps longer than two seconds cannot be assigned uniquely to A–G because
the capture has no iTOW, packet sequence, UART/parser/queue/ring counters or
write acknowledgements. Claiming a specific layer would be guesswork. The v2
protocol and audit retain those discriminators for the next session.

No RTK/survey truth, physical gate event, PPS capture, or complete decoder tick
series was available. Consequently this report makes no absolute position
accuracy or ±1 ms timing claim. A smoothed overlay is diagnostic output only.

The same session's downsampled IMU records show ICM42688 acceleration norm
median 1.776 g, p95 15.75 g and max 19.2 g; MPU6050 values are 0.928 g,
2.26 g and 6.49 g. This is not Allan variance, but it is enough to reject the
assumption that a fixed 0.85/0.15 mix is always beneficial. Range, mounting,
saturation and field units need a controlled static/dynamic capture.

For three transponders, tick frequency estimated against noisy TCP elapsed time
had medians 256025.6, 256016.7 and 256018.0 Hz. This supports 256 kHz as a
plausible configuration, not a calibration. A long, loss-free raw tick capture
is still required to estimate decoder frequency and drift.
