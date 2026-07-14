# ESP32 卡丁車遙測韌體

ESP32-WROOM-32 + **ICM-42688-P** + **DHT11** + **NEO-6M** → WiFi → `POST /api/telemetry/ingest` → InfluxDB → Grafana。

預設 profile 含韌體內 **GPS-aided dead reckoning**（25Hz `dr_position`）。

## 腳位

| 模組 | ESP32 | 感測器 |
|------|-------|--------|
| ICM42688 | 3.3V | 3.3V |
| | GND | GND |
| | GPIO21 | SDA |
| | GPIO22 | SCL |
| DHT11 | 3.3V/5V | VCC |
| | GND | GND |
| | GPIO15 | DATA |
| NEO-6M | 3.3V/5V | VCC |
| | GND | GND |
| | GPIO16 | TXD |
| | GPIO17 | RXD |

ICM I2C 位址：`0x68`（AD0=GND）或 `0x69`（韌體會自動探測）。`WHO_AM_I` 應為 `0x47`。

## 燒錄

```bash
cd ESP32
copy include\secrets.h.example include\secrets.h   # 若尚無 secrets.h
# 編輯 secrets.h：WIFI_* / INGEST_URL / INGEST_TOKEN
pio run -t upload
pio device monitor -b 115200
```

`INGEST_TOKEN` 必須與伺服器 `.env` 的 `TELEMETRY_INGEST_TOKEN` 相同。

## Dead reckoning（開機校正）

開機後前 **200 筆 IMU sample**（~8s @25Hz）做 gyro `gz` bias 校正——**這段時間車子必須靜止**。

Serial 會印：

- `[dr] CALIBRATING n/200 (keep vehicle still)`
- `[dr] RUNNING bias_gz=...`
- 每次 GPS fix 修正：`[dr] gps_fix x: ... -> ... (d=...) y: ...`（`DR_DEBUG=1`）

可調常數在 `DeadReckoner.h`：`DR_POS_BLEND`、`DR_HDG_BLEND`、`DR_IMU_MOUNT_YAW_DEG`（IMU x 軸相對車頭）。

## 資料欄位

- `kart_telemetry`：`ax,ay,az,gx,gy,gz,imu_temp_c,accel_mag,dht_*` + 原始 `gps_*`（保留做 debug）
- `dr_position`：`lat_dr, lon_dr, heading_deg, speed_mps`（ESP complementary filter，~25Hz）

## Datasheet

- [ICM-42688-P DS-000347](https://product.tdk.com/system/files/dam/doc/product/sensor/mortion-inertial/imu/data_sheet/ds-000347-icm-42688-p-v1.6.pdf)
- [DHT11](https://www.circuitbasics.com/wp-content/uploads/2015/11/DHT11-Datasheet.pdf)
