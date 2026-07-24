# Position/timing deployment and rollback

Upgrade the server before the firmware. The server remains backward compatible
with the legacy 50-byte GPS packet.

## Server

```powershell
python -m pytest tests/test_gps_packet_v2.py tests/test_gps_jump_filter.py tests/test_gnss_clock.py tests/test_lap_timer_v2.py
$env:KPP_GPS_V2_ENABLED="1"
python -m services.webapp.main
```

Server rollback:

```powershell
$env:KPP_GPS_V2_ENABLED="0"
python -m services.webapp.main
```

Track constraints remain off until a surveyed, calibrated track JSON exists:

```powershell
$env:TRACK_USE_WGS84_ENU="1"
$env:TRACK_CONSTRAINT_ENABLED="0"
```

## ESP32-S3

Set the physical PPS GPIO only after verifying voltage, edge polarity and the
M10 TIMEPULSE configuration. `GNSS_PPS_PIN=-1` is the safe no-PPS default.

```powershell
Set-Location ESP32/sensor_node
pio run -e sensor_node_s3
pio run -e sensor_node_s3 -t upload
```

Build-time rollback flags:

```text
-DKPP_GPS_PACKET_V2=0
-DESKF_NIS_GATE_ENABLE=0
-DDUAL_IMU_FIXED_BLEND_ENABLE=1
-DIMU_MOUNT_Y_UP=0
-DGNSS_PPS_PIN=-1
```

## Verification

Capture a new full session with NAV-PVT, both IMUs, decoder tick and all drop
counters, then run:

```powershell
python scripts/audit_position_timing.py --session-id <id> --plot --json
python -m pytest
```

Review p95/p99 interval, A-K gap classes, NIS rejection reasons,
fused-vs-raw separation, boundary violations, lap error, PPS residual/age and
clock state. An improved-looking overlay alone is not acceptance evidence.
