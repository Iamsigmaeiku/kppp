# ESP32 卡丁車遙測韌體

ESP32-WROOM-32 + **ICM-42688-P** + **DHT11** → WiFi → `POST /api/telemetry/ingest` → InfluxDB → Grafana。

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

## 資料欄位

`kart_telemetry`：`ax,ay,az,gx,gy,gz,imu_temp_c,accel_mag,dht_temp_c,dht_humidity`

## Datasheet

- [ICM-42688-P DS-000347](https://product.tdk.com/system/files/dam/doc/product/sensor/mortion-inertial/imu/data_sheet/ds-000347-icm-42688-p-v1.6.pdf)
- [DHT11](https://www.circuitbasics.com/wp-content/uploads/2015/11/DHT11-Datasheet.pdf)
