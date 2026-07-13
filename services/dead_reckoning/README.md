# Dead Reckoning（GPS-aided）

讀 Influx `kart_telemetry`（`gz`, `gps_lat`, `gps_lon`, `gps_speed_mps`, `gps_course_deg`），
用陀螺儀 yaw rate 積分 heading，配合最後已知 GPS 速度往前推算位置，每次新 GPS fix 進來時
硬校正回真實座標（蓋掉漂移）。寫回 measurement `position_est`（`lat_est`, `lon_est`,
`heading_deg`, `speed_mps`，tag `source`=`gps`|`dr`）。

單車假設：目前只有一台 ESP32/GPS，全域維護一個 `DeadReckoner` 狀態（跟 `attitude_ekf` 同樣的
簡化，多車需要改成 per-device_id 一個 reckoner）。

## 跑

在 repo root（需 `.env` 的 `INFLUX_*`）：

```bash
python -m services.dead_reckoning.main
```

可選：`DR_POLL_INTERVAL_SEC=0.05`（預設）、`DR_GPS_COURSE_MIN_SPEED_MPS=1.0`
（低於這個速度不信任 GPS course，只用陀螺儀 heading）。

## 原理

- GPS 沒有新 fix 時：純 dead reckoning（`heading += gz*dt`，`x/y += speed*cos/sin(heading)*dt`）
- 每次 GPS fix 更新（跟前一筆 lat/lon 不同）：位置硬校正回 GPS 座標；車速夠快
  （≥ `DR_GPS_COURSE_MIN_SPEED_MPS`）時 heading 也用 GPS course 校正
- 本地平面座標用 equirectangular 近似換算（賽道尺度 <1km 誤差可忽略）

## Yaw rate 正負號校正

`gz` 的正負號跟「順時針/逆時針」的對應取決於 ICM-42688 實際安裝方向，程式無法自動判斷。
上路測試時原地往右轉一圈，看 `position_est` 的 `heading_deg` 是不是也往順時針方向跑；
方向相反的話，把 `reckoner.py` 裡 `self._heading_rad += math.radians(gz_dps) * dt` 那行的
`gz_dps` 加負號。

## 限制

- 沒有第一個 GPS fix 之前不會輸出任何 `position_est` 點
- ESP32 端 GPS 只有 ~1Hz 更新，兩次 fix 之間全靠陀螺儀（無輪速計）外推車速不變，
  加減速時位置會偏；等接了測速霍爾感測器後可把 `gps_speed_mps` 換成輪速再餵進來
- 跟 `attitude_ekf` 一樣是單車全域狀態，多車上場需要重構成 per-device_id

## 驗收

- 靜止不動：`position_est` 應該收斂在同一點附近，不持續漂移
- 移動一小段再靜止：`source=gps` 的點應該跟 GPS 原始 `gps_lat/gps_lon` 接近
- 連續跑 10 分鐘不 crash
