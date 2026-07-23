# Cursor 指令：GPS 虛擬起跑線分圈 + 遙測面板分圈資料庫

## 背景（先讀這些檔案）

- `services/decoder_ingest/influx_reader.py` — `get_lap_tracks()` 目前用 decoder 圈次時間戳（`get_lap_history`）去切 GPS 走線。decoder 時間戳和 GPS 取樣有偏移，切出來的圈首尾會歪。
- `services/webapp/track_coords.py` — 已有 `latlng_to_local_m()` / `local_m_to_latlng()` / `px_to_local_m()`，賽道底圖 `tks_qiaotou_track.png`（1280×1280, MPP=0.1377）。
- `services/webapp/history.py` — 現有 API `/api/sessions/{id}/track-laps/{tid}` 的寫法照抄風格。
- `services/webapp/templates/session_detail.html` — 現有 lap chip + Leaflet 走線地圖的 Alpine.js 元件，UI 直接複用這套。
- `services/webapp/templates/telemetry.html` — 遙測數據面板，新 UI 加在這裡。

## 目標

1. **GPS 分圈**：不靠 decoder 訊號，改用「GPS 軌跡穿越虛擬起跑線」來切圈。起跑線是賽道右側直線上的白色橫線。穿越點用線段相交 + 時間內插，精度可到 sub-sample（GPS 10Hz 也能切出 ~ms 級圈速）。
2. **單圈秒數**：起跑線到起跑線的時間差。
3. **遙測面板新增「GPS 分圈」區塊**：列出每一圈（新到舊），點一圈就在衛星圖上畫該圈走線，用來目視檢查切得好不好。

---

## Part 1：`services/decoder_ingest/gps_lap_splitter.py`（新檔，純函式，無 IO）

```python
@dataclass(frozen=True)
class GateCrossing:
    crossed_at: datetime      # 內插後的精確穿越時間
    point_index: int          # 穿越發生在 points[i] -> points[i+1] 之間，記 i

@dataclass(frozen=True)
class GpsLap:
    lap_number: int           # 1-based，依時間先後編
    lap_time: float           # 秒，crossing-to-crossing
    started_at: datetime      # 本圈起跑線穿越時間（內插）
    ended_at: datetime
    points: list[TrackPoint]  # 本圈 GPS 點（含首尾各補一個內插到線上的點）
    is_complete: bool         # out-lap / in-lap（未閉合段）為 False
```

### 演算法（`split_laps_by_gate(points, gate) -> list[GpsLap]`）

1. 全部座標先用 `track_coords.latlng_to_local_m()` 轉本地公尺座標再運算（不要在 lat/lng 空間做幾何）。
2. 起跑線 = 本地座標線段 `(A, B)`。對每對相鄰 GPS 點 `P1→P2` 做 2D 線段相交測試（標準 cross-product 法，含端點容差）。
3. **方向過濾**：定義起跑線法向量 `N`（指向行進方向）。只有 `dot(P2-P1, N) > 0` 的穿越才算，反向穿越（倒車、pit 進出誤觸）直接忽略。行進方向從現場資料確認：右側直線是由南往北還是由北往南跑，寫成常數 `GATE_FORWARD_BEARING_DEG` 並在校準腳本輸出裡驗證。
4. **時間內插**：相交參數 `s ∈ [0,1]`（P1→P2 上的位置），`crossed_at = t1 + s * (t2 - t1)`。這就是比 decoder 切法漂亮的關鍵，不要偷懶取最近點。
5. **防抖**：
   - `MIN_LAP_TIME_SEC = 25.0`（本場地最快約 47s，25 秒內的重複穿越是 GPS 抖動，丟棄）。
   - `MAX_LAP_TIME_SEC = 300.0`：超過視為中斷（進 pit / 失鎖），該段標 `is_complete=False`。
   - 相鄰兩點時間差 > 5s（GPS 斷訊）時，跨越該缺口的「相交」不算穿越。
6. 第一個穿越之前的點是 out-lap（`is_complete=False`, `lap_time` 用段長但不列入最佳圈）；最後一個穿越之後是 in-lap，同理。
7. 每圈 `points` 首尾各插入一個內插到起跑線上的合成 `TrackPoint`（時間 = 穿越時間，速度線性內插），這樣畫出來的圈是閉合的，不會頭尾缺一截。

### 起跑線定義（`services/webapp/track_coords.py` 加常數）

```python
# 起跑線：賽道右側直線白色橫線。座標為本地公尺 (x_m, y_m)，
# 由 tks_qiaotou_track.png 像素座標經 px_to_local_m() 換算。
# ⚠️ 下面是 PLACEHOLDER，要用校準腳本定出真值後回填。
START_GATE_A_M = (55.0, -20.0)   # 線段端點（賽道內側）
START_GATE_B_M = (70.0, -20.0)   # 線段端點（賽道外側）
GATE_FORWARD_BEARING_DEG = 0.0   # 行進方向方位角，過濾反向穿越
```

線段長度取白線實際寬 + 兩側各外擴 ~3m 緩衝（GPS 誤差 2-3m，太短會漏切）。

### 校準腳本 `scripts/calibrate_start_gate.py`

1. 讀入一個現有 session 的全部 GPS 點（複用 `InfluxReader._query_track_points`）。
2. 把軌跡疊畫在 `tks_qiaotou_track.png` 上輸出 PNG（matplotlib 即可），並印出滑鼠指定像素 → `px_to_local_m()` 的換算，讓我在圖上點白線兩端拿到 `START_GATE_*_M`。
3. 帶入 gate 後跑 `split_laps_by_gate`，印出每圈秒數，和同 session 的 decoder 圈速（`get_lap_history`）並排比較，diff 應在 ±1s 內、圈數一致。這是驗收標準。

---

## Part 2：Reader + API

### `InfluxReader.get_gps_lap_tracks(session_id) -> list[GpsLap]`

- 抓整個 session 時間範圍的 GPS 點（複用 `_query_track_points` 的 gps_track → kart_telemetry → dr_position fallback 邏輯與 `source` 標記；session 起訖從 `list_sessions`/session metadata 拿，**不要**依賴 `get_lap_history`——decoder 沒資料時 GPS 分圈也要能動）。
- 丟進 `split_laps_by_gate`。
- 不新增持久化；跟現有 `get_lap_tracks` 一樣即時算。若之後太慢再談快取。

### 新 API（`services/webapp/history.py`）

```
GET /api/sessions/{session_id}/gps-laps
```

回傳（**新到舊排序**）：

```json
{
  "gate": {"a": [lat, lng], "b": [lat, lng]},
  "source": "gps_track",
  "laps": [
    {
      "lap_number": 8,
      "lap_time": 49.228,
      "lap_time_label": "49.228s",
      "started_at": "...", "ended_at": "...",
      "is_complete": true,
      "point_count": 492,
      "points": [{"lat":..., "lon":..., "recorded_at":"...", "speed_mps":...}]
    }
  ]
}
```

`lap_time_label` 用現有 `laptime` Jinja filter。錯誤處理照 `session_lap_tracks_api` 的 503 模式。

---

## Part 3：遙測面板 UI（`telemetry.html`）

新增一個 **「GPS 分圈」** 卡片區塊，設計：

- 上方列表（**新圈在最上面**），每列顯示：`#圈號`、圈速（best 圈綠色高亮）、與 best 的差值（`+0.512`）、點數、`is_complete=false` 的圈灰掉並標「未閉合」。
- 點選一列 → 下方 Leaflet 衛星圖畫該圈走線。地圖元件直接複用 `session_detail.html` 現有的 lap-map 寫法（同底圖、同速度著色 0–72 km/h 漸層、同 legend）。
- 地圖上永遠畫出起跑線（gate 兩端點連線，白色粗線 + 對比描邊），這樣我一眼能看出切點對不對。
- 圈的首尾點各畫一個小 marker（綠=start、紅=end）；切得好的圈兩個 marker 應該重疊在起跑線上。
- 列表上方一行摘要：`共 N 圈（有效 M）· 最佳 49.228s · 來源 gps_track`。
- Alpine.js 風格、命名、loading/error 處理全部照 `session_detail.html` 現有元件，不要引新框架。
- session 選擇：面板現有的 session 脈絡怎麼拿就怎麼拿；若 telemetry.html 目前沒有 session 概念，加一個 session 下拉（資料來源 `/api/sessions`，預設最新）。

---

## Part 4：測試（`tests/test_gps_lap_splitter.py`）

純函式好測，至少涵蓋：

1. 合成矩形軌跡繞 3 圈穿越 gate → 切出 3 圈 + out-lap/in-lap，圈速誤差 < 取樣間隔。
2. 時間內插正確性：已知穿越時刻的合成資料，`lap_time` 誤差 < 1ms。
3. 反向穿越被忽略。
4. `MIN_LAP_TIME_SEC` 防抖：gate 附近來回抖動不會多切。
5. GPS 斷訊缺口跨越不算穿越。
6. 空輸入 / 從未穿越 → 回單一 `is_complete=False` 段或空列表（擇一定義清楚）。

## 驗收

- [ ] `pytest tests/test_gps_lap_splitter.py` 全綠
- [ ] 校準腳本對既有 session：GPS 分圈圈數 == decoder 圈數，圈速 diff ±1s 內
- [ ] 遙測面板能列出分圈（新→舊）、點選畫走線、起跑線與首尾 marker 可見
- [ ] decoder 完全沒資料的 session，GPS 分圈仍可運作

## 約束

- 不改動現有 decoder 分圈路徑（`get_lap_tracks` / `lap_tracker.py`），GPS 分圈是平行的新資料來源，之後比較滿意再談取代。
- 幾何運算全在本地公尺座標系做。
- 註解風格照 repo 現況（繁中、解釋「為什麼」）。
