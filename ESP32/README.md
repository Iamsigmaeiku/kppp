# ESP32 單板遙測（sensor_node）

一顆 ESP32 同時跑感測 + 15 維 ESKF + WiFi 上傳（無板間 UART）。

```
室外 / iPhone 熱點（預設）:
  ICM + GY-521 + M10 → sensor_node → HTTPS frame-ingest → Cloudflare → chuck → Influx

家裡 LAN（secrets.h 設 TELEMETRY_USE_HTTP=0）:
  … → sensor_node → UDP :9500 → webapp
```

## 專案

| 目錄 | 角色 | 燒錄 |
|------|------|------|
| [`sensor_node/`](sensor_node/) | ICM SPI + MPU I2C + M10 + ESKF + HTTPS/UDP 上傳；ArduinoOTA | `cd sensor_node && pio run -t upload` |
| [`common/packet.h`](common/packet.h) | 共用幀格式 `0xAA55` + CRC16-CCITT | |
| [`common/KppOta.h`](common/KppOta.h) | ArduinoOTA helper | |
| [`wifi_node/`](wifi_node/) | **legacy** 雙板 UART 中繼（已不建議） | |

```bash
copy sensor_node\include\secrets.h.example sensor_node\include\secrets.h
# 熱點：WIFI_* + INGEST_FRAME_URL + INGEST_TOKEN（= chuck .env TELEMETRY_INGEST_TOKEN）
# LAN：TELEMETRY_USE_HTTP=0 + SERVER_IP / SERVER_PORT
```

伺服器 `.env`：`TELEMETRY_INGEST_TOKEN`（HTTP）、`TELEMETRY_UDP_*`（LAN UDP）

HTTP 模式會抽樣 raw IMU（ICM ~1/20、MPU ~1/10），**GPS / fused 全保留**；POST 失敗會重試不清 batch。

---

## ArduinoOTA

首次 USB 燒錄後可無線更新：

```bash
cd sensor_node && pio run -t upload   # 首次 USB
# Serial 看 [wifi] ok <IP> 後：
pio run -e sensor_node_ota -t upload --upload-port <IP>
```

密碼 `kpp-ota`。比賽可在 `secrets.h` 設 `#define OTA_ENABLE 0`。

---

## 接線（單板）

| 模組 | ESP32 | 備註 |
|------|-------|------|
| **ICM42688 VSPI** | SCK=18 MISO=19 MOSI=23 CS=5 | 主 IMU（ESKF） |
| **GY-521 MPU6050** | SDA=21 SCL=22，位址 `0x68` | 副 IMU；`Wire` 400 kHz |
| **M10-180C** | TXD→GPIO16 RXD→GPIO17 | UART1；模組 5V |
| **LED** | GPIO2 | WiFi/TX 狀態 |

不需再接第二顆 ESP32。GPIO25/26 已釋放。

---

## 封包

`[0xAA 0x55][type][len][payload][crc16 LE]`

| type | 內容 | 速率（約） |
|------|------|-----------|
| `0x01` | ICM raw（多筆合包，每筆 ts） | FIFO ~1 kHz（HTTP 上傳抽樣） |
| `0x04` | MPU6050 raw（同 layout，合包） | 200 Hz（HTTP 上傳抽樣） |
| `0x02` | UBX-NAV-PVT | 10 Hz |
| `0x03` | 融合 pose；`flags` bit3=`IMU_FAULT` | 50 Hz |

雙 IMU：ESKF 以 ICM 為主；MPU 做一致性檢查（超門檻 → `IMU_FAULT`）+ 可選加權平均（預設 ICM 0.85 / MPU 0.15）。

---

## sensor_node Serial

每秒診斷（含 `up_B` / `up_fail` / `wifi`）；`d`/`s` 狀態、`r` reset（含 MPU bias）。

---

## Legacy：wifi_node（雙板）

舊架構：sensor_node UART2 @ 921600 → wifi_node → 上傳。  
僅作參考；新部署請用單板路徑。
