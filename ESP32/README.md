# ESP32 卡丁車遙測韌體

ESP32-WROOM-32 → WiFi → `POST /api/telemetry/ingest` → InfluxDB → Grafana。

## Profiles

| env | 硬體 | 說明 |
|-----|------|------|
| `esp32dev`（預設） | ICM **SPI** + DHT11 + M10-180C UART1 | 2D ESKF GPS+IMU → `dr_position` |
| `esp32-imu2-gps` | GY-85 + MPU6050@0x69 + NEO-6M | `DEVICE_ID=esp32-kart-02`；`main_imu2.cpp` |
| `esp32-dual-gps` | M10 UART0 + NEO UART2 + ICM **I2C**（GPIO21/22） | Hybrid：`DEVICE_ID=esp32-kart-dual`，ingest 寫 `gps_track`；**無 DHT**；本次未改 SPI |

```bash
pio run -e esp32dev -t upload
pio run -e esp32-imu2-gps -t upload
pio run -e esp32-dual-gps -t upload   # 燒錄前拔 M10 線！
```

---

## 腳位（esp32dev）

| 模組 | ESP32 | 備註 |
|------|-------|------|
| **ICM42688 (VSPI)** | 3.3V / GND / **SCK=18** / **MISO=19** / **MOSI=23** / **CS=5** | **AD0→GND**；CS 不可浮接 |
| DHT11 | GPIO15 DATA（+ **4.7k** pull-up；不穩可改 GPIO4） | |
| **M10-180C** | GPIO16 ← TXD，GPIO17 → RXD | **UART1（16/17）**；不要跟 `esp32-dual-gps` 的 UART0（GPIO1/3）搞混 |

> 舊 I2C（SDA=21 / SCL=22）請拔掉，改接上表 SPI。`WHO_AM_I` 應為 `0x47`。

## 腳位（esp32-imu2-gps）

| 模組 | ESP32 | 備註 |
|------|-------|------|
| **GY-85 I2C** | SDA=GPIO21 / SCL=GPIO22 | ADXL345 `0x53`、ITG3205 `0x68`、HMC5883L `0x1E` |
| **GY-521 MPU6050** | 同 bus | **AD0→3.3V** → 位址 **`0x69`**（避開 ITG 的 0x68） |
| **NEO-6M** | GPIO16 ← TXD，GPIO17 → RXD | UART1；legacy UBX-CFG |

開機 log 會掃 `0x53/0x68/0x1E/0x69`，四個都應 `ACK`。

---

## 腳位（esp32-dual-gps）

| 模組 | ESP32 | 感測器 |
|------|-------|--------|
| **M10-180C** | 5V/GND，**TX0(GPIO1)→GPS RX**，**RX0(GPIO3)←GPS TX** | UART0（與燒錄共用） |
| **NEO-6M** | 3.3V/5V/GND，GPIO17→RX，GPIO16←TX | UART2 |
| **ICM42688 (I2C)** | 3.3V/GND，SDA=GPIO21，SCL=GPIO22 | **仍 I2C**（本輪未改） |

### 燒錄 / UART0 警告

1. **燒錄前拔掉 M10 的 TX/RX（或斷電）**，否則 NMEA 會干擾 upload。
2. 正式 build **沒有** Serial Monitor debug（UART0 給 GPS）。
3. 若要開 debug：在 `platformio.ini` 加 `-DDUAL_GPS_DEBUG`——此時**不要接 M10**，UART0 改印 log。

### 資料流（Hybrid）

```
M10 + NEO + ICM → ESP32 → POST /api/telemetry/ingest
  → kart_telemetry (device_id, IMU + 主 gps_*)
  → gps_track (tag device=m10180c | neo6m)  ← Grafana 雙 Route
```

Grafana dashboard uid：`kart-telemetry`（F1 風格總覽；看板內 `$device` 切 #1/#2，Fleet 區雙車疊圖）。

---

## 燒錄（通用）

```bash
cd ESP32
copy include\secrets.h.example include\secrets.h
# 編輯 WIFI_* / INGEST_URL / INGEST_TOKEN
pio run -e <env> -t upload
```

`INGEST_URL` 請指 Pi 區網 `http://<pi-ip>:5000/api/telemetry/ingest`；`INGEST_TOKEN` = 伺服器 `.env` 的 `TELEMETRY_INGEST_TOKEN`。

## ESKF（僅 esp32dev）

開機後前 **200** 筆 IMU（~4s @50Hz）估 gyro Z bias + 水平 accel bias——**車子必須靜止**。

融合（參考 [zm0612 ESKF](https://github.com/zm0612/eskf-gps-imu-fusion) 架構，地面車 8-state 簡化）：

1. **Predict** @50Hz：IMU 推 yaw / 速度 / 位置，協方差傳播
2. **Correct** @GPS 新 fix：位置量測（R ∝ HDOP）+ 速度量測（course 有效且 v>2m/s）
3. Outlier gate：低速時 innov > 3σ 跳過該次 GPS
4. 低速 ZVU 抑制靜止漂移

輸出欄位仍寫 `dr_position`（`lat_dr/lon_dr/heading_deg/speed_mps`），Grafana 不用改。

調參：`GpsImuEskf.h` 的 `ESKF_HDOP_SCALE` / `ESKF_POS_SIGMA_MIN_M`；安裝角 `DR_IMU_MOUNT_YAW_DEG`（`platformio.ini`）。

### 帶回板子後驗收 checklist

1. 開機 log：`WHO_AM_I=0x47`，靜止 `|a|≈1.0g`
2. `[eskf] RUNNING` 後靜止 30s，Grafana `dr_position` 漂移 < ~2m
3. 跑一圈：軌跡比 raw GPS 平滑；短暫樹蔭仍連續
4. 原地右轉一圈：`heading_deg` 順時針增加；反了就改 `DR_IMU_MOUNT_YAW_DEG` 或 gz 符號

## 資料欄位

- `kart_telemetry`（esp32dev）：`ax..gz,imu_temp_c,...` + 原始 `gps_*`
- `kart_telemetry`（esp32-imu2-gps）：`gy85_ax..gz/mx..mz/heading_deg` + `mpu_ax..gz/temp_c` + `gps_*`（不送 ICM 的 `ax/ay/az`）
- `dr_position`：`lat_dr, lon_dr, heading_deg, speed_mps`（僅 default / ESKF）
- `gps_track`：`lat, lon, alt, speed, course, hdop, sats` + tag `device=m10180c|neo6m` + `device_id`（dual-gps）

## Datasheet

- [ICM-42688-P](https://product.tdk.com/system/files/dam/doc/product/sensor/mortion-inertial/imu/data_sheet/ds-000347-icm-42688-p-v1.6.pdf)
- [DHT11](https://www.circuitbasics.com/wp-content/uploads/2015/11/DHT11-Datasheet.pdf)
