# Dead Reckoning（GPS-aided）

## ESP32 端（主力，Grafana geomap 用這個）

韌體 `DeadReckoner` 在 IMU 迴圈（25Hz）輸出平滑軌跡，經 ingest 寫入 measurement
**`dr_position`**（`lat_dr`, `lon_dr`, `heading_deg`, `speed_mps`）。原始 GPS 仍寫在
`kart_telemetry.gps_*`，Grafana 可疊圖調 blend α。

詳見 [`ESP32/README.md`](../../ESP32/README.md)（開機靜止 bias 校正）。

## Pi 端（可選對照，舊路徑）

讀 Influx `kart_telemetry`（`gz`, `gps_lat`, `gps_lon`, `gps_speed_mps`, `gps_course_deg`），
用陀螺儀 yaw rate 積分 heading，配合最後已知 GPS 速度往前推算位置，每次新 GPS fix 進來時
硬校正回真實座標（蓋掉漂移）。寫回 measurement `position_est`（`lat_est`, `lon_est`,
`heading_deg`, `speed_mps`，tag `source`=`gps`|`dr`）。

**Grafana 主圖已改查 `dr_position`。** 此 Pi service 可保留當對照，或在 ESP DR 驗收後停掉。

單車假設：目前只有一台 ESP32/GPS，全域維護一個 `DeadReckoner` 狀態（跟 `attitude_ekf` 同樣的
簡化，多車需要改成 per-device_id 一個 reckoner）。

## 跑（Pi 對照）

在 repo root（需 `.env` 的 `INFLUX_*`）：

```bash
python -m services.dead_reckoning.main
```

可選：`DR_POLL_INTERVAL_SEC=0.05`（預設）、`DR_GPS_COURSE_MIN_SPEED_MPS=1.0`
（低於這個速度不信任 GPS course，只用陀螺儀 heading）。

## 原理（Pi）

- GPS 沒有新 fix 時：純 dead reckoning（`heading += gz*dt`，`x/y += speed*sin/cos(heading)*dt`）
- 每次 GPS fix 更新（跟前一筆 lat/lon 不同）：位置硬校正回 GPS 座標；車速夠快
  （≥ `DR_GPS_COURSE_MIN_SPEED_MPS`）時 heading 也用 GPS course 校正
- 本地平面座標用 equirectangular 近似換算（賽道尺度 <1km 誤差可忽略）

## Yaw rate 正負號校正

`gz` 的正負號跟「順時針/逆時針」的對應取決於 ICM-42688 實際安裝方向，程式無法自動判斷。
上路測試時原地往右轉一圈，看 heading 是不是也往順時針方向跑；
方向相反的話：

- **ESP**：在 `DeadReckoner::tick` 對 `gz_cal` 取負，或調 `DR_IMU_MOUNT_YAW_DEG`
- **Pi**：把 `reckoner.py` 裡 `gz_dps` 加負號

## 限制（Pi）

- 沒有第一個 GPS fix 之前不會輸出任何 `position_est` 點
- ESP32 開機時會送 UBX-CFG-RATE 把 NEO-6M 拉到 5Hz（模組沒回 ACK 或不支援時會停在預設 1Hz），
  兩次 fix 之間全靠陀螺儀（無輪速計）外推車速不變，
  加減速時位置會偏；等接了測速霍爾感測器後可把 `gps_speed_mps` 換成輪速再餵進來
- 跟 `attitude_ekf` 一樣是單車全域狀態，多車上場需要重構成 per-device_id

## 驗收（ESP `dr_position`）

- 靜止不動：漂移 < 0.5 m / 分鐘
- 跑一圈：終點與 GPS 原始終點差距 < 3 m
- 髮夾彎：軌跡連續平滑（非 1Hz 折線）
- Serial：每次 GPS fix 有 `x/y before→after` 修正量 log
