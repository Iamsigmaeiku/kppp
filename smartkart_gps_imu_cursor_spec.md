# SmartKart GPS+IMU 網站功能補齊 — Cursor 實作規格書

## 背景

ESP32 韌體端已經加了 NEO-6M GPS（UART1, GPIO16/17, 5Hz），會在每筆 25Hz IMU 封包上
順便帶 `gps_lat/gps_lon/gps_speed_mps/gps_course_deg/gps_alt_m/gps_hdop/gps_satellites`
（沒訊號時整組欄位省略，不送 0）。韌體端已經完成、燒錄，不在這份規格書範圍內。

這份規格書涵蓋**網站/後端**那一側要補的東西：後端 ingest 接欄位、GPS-aided dead
reckoning 微服務、Grafana 賽道走線 panel、webapp 狀態列顯示。下面 Task1–5 是**已經在
這份 repo 實作完成**的內容，照著做／核對即可，不用重新設計。

---

## Task 1 — `services/webapp/telemetry.py`：接受並寫入 GPS 欄位

`TelemetrySample` pydantic model 加 7 個 optional 欄位：

```python
gps_lat: float | None = None
gps_lon: float | None = None
gps_speed_mps: float | None = None
gps_course_deg: float | None = None
gps_alt_m: float | None = None
gps_hdop: float | None = None
gps_satellites: int | None = None
```

`_sample_to_point()` 把這些欄位加進寫入 InfluxDB `kart_telemetry` measurement 的
`fields` dict（沿用現有「None 就不寫」的迴圈邏輯，不用另外特判）。另外：當
`gps_lat`/`gps_lon` 都有值時，加一個 tag `gps_fix=1`（方便之後用 tag 快速篩選有訊號的
時間段，不用每次都在 Flux 裡判斷欄位是否存在）。

`/api/telemetry/status` 不用改，它是直接回傳快取的 `sample.model_dump()`，新欄位會自
動帶出來。

**驗收**：POST 一筆帶 `gps_lat/gps_lon` 的 sample 到 `/api/telemetry/ingest`，Influx
`kart_telemetry` 裡該筆多出 `gps_lat`/`gps_lon` 等 field，且 tag 有 `gps_fix=1`。

---

## Task 2 — 新增 `services/dead_reckoning/` 微服務

跟現有 `services/attitude_ekf/` 同樣的結構與慣例（同一份 repo-root `.env`、同樣的
poll-loop 寫法），新增 4 個檔案：

### `services/dead_reckoning/config.py`
讀 `INFLUX_URL/TOKEN/ORG`、`INFLUX_BUCKET`（bucket 變數命名沿用 `BUCKET`）、
`DR_POLL_INTERVAL_SEC`（預設 0.05）、`DR_GPS_COURSE_MIN_SPEED_MPS`（預設 1.0，低於這
個速度 GPS course 雜訊太大不可信，只用陀螺儀 heading）。`MEASUREMENT_IMU =
"kart_telemetry"`，`MEASUREMENT_POSITION = "position_est"`。

### `services/dead_reckoning/reckoner.py`
核心融合演算法，`DeadReckoner` class：

- heading 是羅盤方位角慣例（0=北，順時針為正，跟 GPS course-over-ground 同慣例）：
  `heading += radians(gz_dps) * dt`
- 位移用 `x_east += speed*sin(heading)*dt`、`y_north += speed*cos(heading)*dt`（注意不
  是 cos/sin，是 sin/cos，因為 heading 是羅盤角不是數學角）
- 本地平面座標（x=東, y=北，公尺）用 equirectangular 近似換算成 lat/lon：
  `EARTH_M_PER_DEG_LAT = 111320.0`，`meters_per_deg_lon = 111320 * cos(radians(ref_lat))`
- 拿到第一個 GPS fix 之前回傳 `None`（還沒有本地座標系的參考原點）
- 每次偵測到「新的」GPS fix（跟前一筆 lat/lon 不同——ESP32 韌體是每個 25Hz IMU 封包
  都帶「最後已知」GPS 值，同一個 fix 會重複出現很多次，只有值真的變了才算新 fix）：
  硬校正 `x_m/y_m` 回真實 GPS 座標；車速 ≥ `DR_GPS_COURSE_MIN_SPEED_MPS` 時，heading
  也用 GPS course 校正
- 沒有新 fix 時：純 dead reckoning，`speed_mps` 沿用最後一次 GPS 給的值（沒有輪速計，
  這是目前唯一能用的速度來源）
- `update()` 回傳 `FusedState(lat, lon, heading_deg, speed_mps, source)`，`source` 是
  `"gps"`（這個點是硬校正點）或 `"dr"`（純推算點）

完整程式碼見 repo 現有的 `services/dead_reckoning/reckoner.py`，直接照抄即可，邏輯已
經過單元測試驗證（靜止不漂移、往東走 heading=90 時 lon 增加/lat 不變、新 fix 會硬
reset 回 GPS 座標）。

### `services/dead_reckoning/main.py`
跟 `attitude_ekf/main.py`幾乎一樣的 poll loop 結構：查詢

```flux
from(bucket:"{BUCKET}")
  |> range(start: -2s)
  |> filter(fn: (r) => r._measurement == "kart_telemetry")
  |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
```

逐筆取 `gz`（沒有就跳過）、`gps_lat`、`gps_lon`、`gps_speed_mps`、`gps_course_deg`，
餵給 `DeadReckoner.update()`，回傳非 None 才寫入：

```python
Point("position_est")
  .tag("source", fused.source)
  .field("lat_est", fused.lat)
  .field("lon_est", fused.lon)
  .field("heading_deg", fused.heading_deg)
  .field("speed_mps", fused.speed_mps)
  .time(ts, WritePrecision.NS)
```

有 `device_id` tag 就一併帶上（跟 attitude_ekf 一樣）。**單車假設**：全域一個
`DeadReckoner` 實例，不是 per-device_id（跟 attitude_ekf 同樣的簡化，多車上場需要重
構）。

### `services/dead_reckoning/requirements.txt` + `__init__.py` + `README.md`
比照 `attitude_ekf` 那組檔案的格式（`requirements.txt` 只列 `influxdb-client` /
`python-dotenv`，因為 repo-root `requirements.txt` 已經有了）。README 要包含：怎麼跑
（`python -m services.dead_reckoning.main`）、演算法原理、**yaw rate 正負號校正步
驟**（原地往右轉一圈看 `heading_deg` 是否也順時針跑，方向反了就把 `reckoner.py` 裡
`gz_dps` 那行加負號）、已知限制（沒輪速計、單車假設）。

**驗收**：`services/dead_reckoning/main.py` 跑起來後，Influx 出現 `position_est`
measurement；靜止不動時位置收斂不飄移；連續跑 10 分鐘不 crash。

---

## Task 3 — `infra/grafana/dashboards/kart-telemetry.json`：geomap 接上真實軌跡

原本 `id: 12` 的 geomap panel 是空殼（`"layers": []`, `"targets": []`，標題寫「GPS
data layer 待補」）。改成：

- `targets`：一個 InfluxDB Flux query，查 `position_est`，pivot 後 keep
  `_time/lat_est/lon_est/speed_mps/heading_deg`
- `options.layers`：一個 `type: "markers"` layer，`location.mode: "coords"`，
  `location.latitude: "lat_est"`，`location.longitude: "lon_est"`，顏色綁
  `speed_mps` 欄位
- `fieldConfig.defaults`：`color.mode: "continuous-GrYlRd"`，`min: 0`，`max: 20`
  （卡丁車速度上限抓 20 m/s ≈ 72km/h），`unit: "velocitymps"`
- 標題改成「賽道走線（NEO-6M GPS + gz dead reckoning，車速著色）」

basemap（Google Satellite tile）、`view` 的固定中心座標都不用動，只是加 layer/target
上去。

**驗收**：`position_est` 有資料後，Grafana 這個 panel 應該畫出彩色的軌跡點，顏色隨車
速變化。

---

## Task 4 — `services/webapp/templates/telemetry.html`：狀態列顯示 GPS 資訊

原本狀態列只有 ESP32 連線狀態 + `|a|` + DHT。加一段：

```html
<span x-show="last?.sample" x-cloak>
  <span class="dot" :class="hasGpsFix ? 'ok' : 'warn'"></span>
  <template x-if="hasGpsFix">
    <span>
      GPS <span x-text="fmt(last?.sample?.gps_lat, 5)"></span>,
      <span x-text="fmt(last?.sample?.gps_lon, 5)"></span>
      · <span x-text="fmt(last?.sample?.gps_speed_mps)"></span>m/s
      · <span x-text="last?.sample?.gps_satellites ?? '—'"></span>顆衛星
    </span>
  </template>
  <template x-if="!hasGpsFix">
    <span>GPS 尚未定位</span>
  </template>
</span>
```

Alpine component（`telemetryStatus()`）加一個 computed getter：

```js
get hasGpsFix() {
  const lat = this.last?.sample?.gps_lat;
  const lon = this.last?.sample?.gps_lon;
  return typeof lat === 'number' && typeof lon === 'number';
},
```

`fmt(v, decimals = 2)` 加第二個參數（原本只有 2 位小數，經緯度要顯示 5 位小數才有意
義）。

**驗收（已在瀏覽器實測過）**：`/telemetry` 頁面，有 GPS fix 時顯示座標/速度/衛星
數＋綠燈；沒有 fix 時顯示「GPS 尚未定位」＋黃燈；console 沒有錯誤。

---

## Task 5 — `.env.example` / `infra/pi/README.md`：補文件

`.env.example` 加：

```
# Dead reckoning (services/dead_reckoning): GPS-aided yaw-rate 積分，
# 融合 kart_telemetry 的 gz + gps_lat/gps_lon/gps_speed_mps/gps_course_deg，
# 寫回 position_est。跑法見 services/dead_reckoning/README.md。
DR_POLL_INTERVAL_SEC=0.05
DR_GPS_COURSE_MIN_SPEED_MPS=1.0
```

`infra/pi/README.md` 在 Attitude EKF 那段後面加一段 Dead Reckoning 的啟動指令
（`python -m services.dead_reckoning.main`，另開一個 terminal，跟 attitude_ekf 平行
跑）。

---

## 明確排除（這份規格書不涵蓋）

- ESP32 韌體（GPS UART 接線、TinyGPS++ parse、5Hz UBX 設定）已經完成，不在這裡
- 多車（per-device_id 的 dead reckoning / attitude EKF）目前是單車假設，之後要多車再
  重構
- AI 教練（`ai_coach.py`）目前仍只吃圈速，沒有把 GPS/position_est 餵進去，這份規格書
  沒有動它
- 輪速感測器（霍爾）尚未接上，`dead_reckoning` 目前用 GPS 最後已知速度做外推，之後接
  了霍爾要把 `gps_speed_mps` 換成輪速

## 整體驗收

1. `pytest tests/test_webapp_auth_and_bindings.py -q` 全過（telemetry ingest 相關）
2. `python -m services.dead_reckoning.main` 跑起來不 crash，Influx 出現
   `position_est`
3. Grafana `kart-telemetry` dashboard 的走線 panel 有畫出彩色軌跡
4. `/telemetry` 頁面狀態列在有/沒有 GPS fix 時都正確顯示
