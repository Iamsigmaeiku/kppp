# Cursor 指令：離線 RTS Smoother 走線平滑 + Decoder 時基校準

> 依賴前一份 spec（`cursor-instructions-gps-lap-split.md`）的 `gps_lap_splitter.py` 與
> `START_GATE_*` 常數。本份新增兩條平行功能，一樣不動現有 decoder 分圈路徑。

## 背景（先讀）

- `services/decoder_ingest/influx_reader.py` — `_query_track_points()` 的來源 fallback 鏈、`get_lap_history()`。
- `services/decoder_ingest/influx_writer.py` — decoder 過線事件寫入 `decoder_raw_events`（tag `session_id` / `decoder_id`，field 含 `last_lap_time`）。
- `services/dead_reckoning/reckoner.py` — 現有前向融合；已知問題：EKF 是因果濾波，`dr_position` 會飄出賽道。
- `kart_telemetry` fields：`gps_lat/gps_lon`（tag `gps_fix=="1"` 才是 fresh fix）、`gps_speed_mps`、`gps_hdop`、`hall_hz`、`a_lat/a_lon`。IMU ≈25Hz。
- `services/webapp/track_coords.py` — `latlng_to_local_m()` 等，幾何一律在本地公尺座標做。
- SQLite：`services/webapp/models.py` + `migrations/`（校準結果持久化用這套）。

---

# Part A：Decoder 時基校準

## 問題

MyLaps decoder 的過線時間戳與 ESP32 GPS 時間戳沒有可靠對齊（smartkart-lstm README 已確認）。
要解出兩個時基間的**常數偏移 + 線性漂移**，讓 decoder 圈速能當 GPS 分圈的 ground truth 用。

## 新檔 `services/decoder_ingest/timebase_calibration.py`（純函式，無 IO）

```python
@dataclass(frozen=True)
class TimebaseResult:
    offset_sec: float          # decoder_time - gps_time（正 = decoder 鐘快）
    drift_sec_per_hour: float  # 線性漂移
    matched_pairs: int
    residual_std_sec: float
    quality: str               # "good" | "marginal" | "failed"
```

### 演算法 `calibrate(decoder_passings, gps_crossings) -> TimebaseResult`

1. **配對不能靠絕對時間**（偏移未知），改用**圈速序列比對**：兩邊各取相鄰事件差分
   （= 各自量到的圈速序列），做 sliding alignment（對每個 lag 算圈速差的中位絕對誤差，
   取最小者）。圈速對時鐘偏移不變，這就是配對的錨。
2. 對齊後得到 N 組 (decoder_passing_i, gps_crossing_i)。
   `offset = median(decoder_i - gps_i)`（median 抗漏抓/多抓的離群配對）。
3. **漂移**：對 matched pairs 的 `(gps_time, decoder_time - gps_time)` 做
   robust 線性回歸（Theil–Sen，手寫即可，別引 sklearn），斜率 → `drift_sec_per_hour`。
4. 品質判定：`matched_pairs >= 5` 且 `residual_std <= 0.15s` → good；
   `>= 3` 對且 `<= 0.5s` → marginal；否則 failed。failed 就不要拿去用，回報就好。
5. 邊界：decoder 漏抓（GPS 有圈 decoder 沒有）、GPS 失鎖漏圈，配對演算法要容忍
   單邊缺項（差分序列比對時允許 skip，簡單 DP 或直接暴力 lag 掃描 ±3 圈皆可，
   資料量小，選好寫好測的）。

### 資料來源（`InfluxReader` 新方法）

- `get_decoder_passings(session_id, transponder_id) -> list[datetime]`：
  從 `decoder_raw_events` 撈原始過線時間（不是 lap_history 的合成結果）。
- GPS 側直接用前一份 spec 的 `split_laps_by_gate` 產出的 crossing 時間。

### 持久化 + API

- SQLite 新表 `timebase_calibration`（migration 照現有風格）：
  `session_id PK, offset_sec, drift_sec_per_hour, matched_pairs, residual_std_sec, quality, calibrated_at`。
- `POST /api/sessions/{session_id}/timebase-calibration` → 跑校準並存檔。
- `GET  /api/sessions/{session_id}/timebase-calibration` → 讀結果。
- **用途落地**：前一份 spec 的 GPS 分圈面板，在有校準結果時每圈多顯示一欄
  「vs decoder」diff（decoder 圈速經 offset/drift 修正後對齊比較）。
  兩套計時互相驗證，diff 穩定在 ±0.1s 內就代表 gate 位置和內插都對了。

### 測試 `tests/test_timebase_calibration.py`

1. 合成兩序列，已知 offset=+37.2s、drift=0 → 解出誤差 < 0.05s。
2. 加 drift（如 +2s/hr）→ Theil–Sen 抓得到。
3. decoder 漏 1 圈 / GPS 漏 1 圈 → 仍能配對，quality 正確降級。
4. 完全不相關的兩序列 → quality == "failed"，不會硬給數字。

---

# Part B：離線 RTS Smoother 走線平滑

## 問題

前向濾波（ESKF/DR）只看過去，走線會飄；賽後分析可以看未來 → 用
forward Kalman + backward RTS pass，零延遲、平滑、能橋接 GPS 短暫失鎖。

## 新 package `services/postprocess/`

```
services/postprocess/__init__.py
services/postprocess/rts_smoother.py   # 純演算法，numpy only，不引 filterpy
services/postprocess/session_smoother.py  # IO 編排：Influx 讀 → 平滑 → Influx 寫
```

### 模型（`rts_smoother.py`）

- 狀態 `[x, y, vx, vy]`（本地公尺，`track_coords.latlng_to_local_m`），
  等速模型 + 加速度白噪聲。過程噪聲 `q_accel` 預設 3.0 m/s²（kart 縱向極限量級），
  設成參數。
- 線性 KF + RTS 就夠，**不要**上 UKF/自寫四元數 ESKF——2D 平面問題，複雜度不換精度。
- 量測更新：
  1. **GPS 位置**（`kart_telemetry` 的 `gps_lat/gps_lon`，只取 tag `gps_fix=="1"`）。
     R 由 `gps_hdop` 動態算：`sigma = max(1.5, hdop * 2.5)` m（參數化）。
  2. **速度大小**（可選，flag 開關）：`gps_speed_mps` 與 `hall_hz` 融合。
     hall 先做尺度校準：全 session 對 `(hall_hz, gps_speed_mps)` 做過原點
     Theil–Sen 回歸得 `m_per_rev`，R² < 0.9 就放棄 hall 只用 GPS 速度。
     速度大小是非線性量測（|v|），線性化一步（一階泰勒）即可。
- **離群剔除**：每次更新前算 Mahalanobis 距離，chi-square gating
  （2 自由度 5.99）超標就丟該點並記數。GPS 多路徑跳點是常態，這步不能省。
- **失鎖段**：無量測時純 predict，P 自然膨脹；backward pass 會把缺口兩端拉順。
  缺口 > 10s 的段輸出時標 `gap=true`（品質欄位），畫圖時可虛線顯示。
- 變動 dt：照實際時間戳算 F/Q，不要假設等間隔。
- 輸出每點：平滑後 lat/lon（`local_m_to_latlng` 轉回）、speed（|v|）、
  位置標準差 `sigma_m = sqrt(trace(P_pos))`。

### IO 編排（`session_smoother.py`）

- 輸入 session_id → 撈該 session 全時段原始點 → 平滑 → 寫回 Influx
  新 measurement **`track_smoothed`**：
  fields `lat_s, lon_s, speed_mps, sigma_m`；tags `device_id, session_id, algo="rts_cv"`。
  寫入前先刪同 session 舊資料（重跑冪等）。
- CLI：`scripts/smooth_session.py --session sess-YYYYMMDD-HHMMSS [--dry-run --plot]`。
  `--plot` 疊畫 raw GPS vs smoothed 在 `tks_qiaotou_track.png` 上輸出 PNG（驗收用）。
- 自動化（phase 2，先不做）：session 結束時由 `session_lifecycle` 觸發。先手動跑穩再說。

### 整合

- `_query_track_points()` fallback 鏈最前面插入 `track_smoothed`
  （`lat_s/lon_s`，speed_field=`speed_mps`），**用 env flag
  `TRACK_USE_SMOOTHED=1` 控制**，預設關。畫走線與 GPS 分圈都自動受益。
- GPS 分圈的 gate 內插用平滑後 50Hz 等效軌跡後，過線時間解析度同步提升——
  不用改 splitter，來源換掉就好。
- 面板上該圈的 `source` 欄會顯示 `track_smoothed`，看得出用的是哪條鏈。

### 測試 `tests/test_rts_smoother.py`

1. 合成賽道圈（已知真值）+ 高斯噪聲 σ=2.5m → smoothed RMSE ≤ raw RMSE 的 1/2。
2. 挖掉 8s GPS（模擬失鎖）→ 缺口內平滑軌跡對真值 RMSE < 5m，且標了 gap。
3. 注入 5 個 30m 跳點 → 全被 gating 剔除，不影響 RMSE。
4. hall 尺度回歸：合成 hall_hz = v / 0.85m + 噪聲 → 解出 m_per_rev 誤差 < 2%。
5. 變動取樣間隔（10–50Hz 混合）不炸、不失真。

---

## 建議實作順序

1. Part B smoother 純演算法 + 測試（無 IO，最快能驗）
2. Part B IO + CLI + `--plot`，拿現有 session 目視驗收
3. Part A 校準純函式 + 測試
4. Part A API / SQLite / 面板 vs-decoder 欄位
5. fallback 鏈整合（flag 打開，跑一週沒問題再改預設）

## 驗收

- [ ] 兩組 pytest 全綠
- [ ] `smooth_session.py --plot`：平滑走線貼賽道、失鎖段合理橋接、無鋸齒
- [ ] 校準後 GPS 分圈 vs decoder 圈速 diff 穩定 ±0.1s 內（gate 正確的前提下）
- [ ] `TRACK_USE_SMOOTHED=1` 時面板走線與分圈正常，關掉行為完全不變

## 約束

- numpy only，不新增 filterpy/sklearn 依賴。
- 純演算法模組不做任何 Influx/HTTP IO，全部可離線單測。
- 註解風格照 repo 現況（繁中、寫為什麼）。
- 不動 ESP32 / `dead_reckoning` / `attitude_ekf` 即時路徑——這是離線後處理。
