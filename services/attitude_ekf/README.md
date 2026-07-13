# Attitude EKF

讀 Influx `decoder` / `kart_telemetry`（`ax..gz`），寫回 measurement `attitude`（`roll_deg`, `pitch_deg`）。

## 跑

在 repo root（需 `.env` 的 `INFLUX_*`）：

```bash
python -m services.attitude_ekf.main
```

可選：`EKF_POLL_INTERVAL_SEC=0.05`（預設）。

## 驗收

- Influx 出現 `attitude`
- 靜止時 roll/pitch ≈ 0；人為傾斜誤差約 ±2°
- 連續跑 10 分鐘不 crash、靜態不持續飄移
