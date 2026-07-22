# ESP32 雙板遙測（sensor_node + wifi_node）

板 #1 專跑感測 + 15 維 ESKF，板 #2 專跑 WiFi 上傳，板間 UART2 @ 921600。

```
室外 / iPhone 熱點（預設）:
  … → wifi_node → HTTPS frame-ingest → Cloudflare → chuck webapp → Influx

家裡 LAN（secrets.h 設 TELEMETRY_USE_HTTP=0）:
  … → wifi_node → UDP :9500 → webapp
```

## 專案

| 目錄 | 角色 | 燒錄 |
|------|------|------|
| [`sensor_node/`](sensor_node/) | ICM SPI + MPU I2C + M10 + ESKF，**無 WiFi** | `cd sensor_node && pio run -t upload` |
| [`wifi_node/`](wifi_node/) | UART 收包 → HTTPS 或 WiFiUDP | `cd wifi_node && pio run -t upload` |
| [`common/packet.h`](common/packet.h) | 共用幀格式 `0xAA55` + CRC16-CCITT | |

```bash
copy wifi_node\include\secrets.h.example wifi_node\include\secrets.h
# 熱點：WIFI_* + INGEST_FRAME_URL + INGEST_TOKEN（= chuck .env TELEMETRY_INGEST_TOKEN）
# LAN：TELEMETRY_USE_HTTP=0 + SERVER_IP / SERVER_PORT
```

伺服器 `.env`：`TELEMETRY_INGEST_TOKEN`（HTTP）、`TELEMETRY_UDP_*`（LAN UDP）

---

## 接線

### 板 #1 sensor_node

| 模組 | ESP32 | 備註 |
|------|-------|------|
| **ICM42688 VSPI** | SCK=18 MISO=19 MOSI=23 CS=5 | 主 IMU（ESKF） |
| **GY-521 MPU6050** | SDA=21 SCL=22，位址 `0x68` | 副 IMU；`Wire` 400 kHz |
| **M10-180C** | TXD→GPIO16 RXD→GPIO17 | UART1；模組 5V |
| **→ 板 #2** | TX=GPIO25 RX=GPIO26 | UART2 921600；交叉 |

### 板 #2 wifi_node

| | ESP32 |
|--|-------|
| **← 板 #1** | RX=GPIO16 TX=GPIO17（交叉） |
| **LED** | GPIO2 |

交叉：板1 TX25 ↔ 板2 RX16；板1 RX26 ↔ 板2 TX17；共地。

---

## 封包

`[0xAA 0x55][type][len][payload][crc16 LE]`

| type | 內容 | 速率（約） |
|------|------|-----------|
| `0x01` | ICM raw（多筆合包，每筆 ts） | FIFO ~1 kHz |
| `0x04` | MPU6050 raw（同 layout，合包） | 200 Hz |
| `0x02` | UBX-NAV-PVT | 10 Hz |
| `0x03` | 融合 pose；`flags` bit3=`IMU_FAULT` | 50 Hz |

### UART 頻寬（921600 ≈ 90 KB/s）

| 流 | 估算 |
|----|------|
| 0x01 ICM 1 kHz ×5 合包 | ~20 KB/s |
| 0x04 MPU 200 Hz ×5 合包 | ~4 KB/s |
| 0x02 + 0x03 | ~2.5 KB/s |
| **合計** | **~27 KB/s**（足夠） |

雙 IMU：ESKF 以 ICM 為主；MPU 做一致性檢查（超門檻 → `IMU_FAULT`）+ 可選加權平均（預設 ICM 0.85 / MPU 0.15）。

---

## sensor_node Serial

每秒診斷；`d`/`s` 狀態、`r` reset（含 MPU bias）。
