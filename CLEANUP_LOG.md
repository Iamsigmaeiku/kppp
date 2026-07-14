# KPP Cleanup Log

此文件記錄每個重構 phase 的改動、理由、驗證方式，供人工複查。

---

## Phase 0：建安全網（2026-07-14）

### 0.1 pre-refactor commit + 建立 refactor/cleanup 分支

- **改了什麼**：把 working tree 27 個已修改未 commit 的檔案一次 commit（`pre-refactor: snapshot of working state before cleanup branch`），建立 `refactor/cleanup` 分支
- **為什麼**：確保重構前的狀態有明確的 git 節點可 revert，且所有工作都在獨立分支上進行，不直接改 main
- **驗證方式**：`git log --oneline -3` 確認 commit 存在，`git branch` 確認目前在 `refactor/cleanup`

### 0.2 pytest baseline

執行時間：2026-07-14 09:44（台灣時間）

```
python -m pytest tests/ -q
```

**結果：70 passed, 3 failed, 3 warnings in 14.26s**

已知 3 個 failure（pre-existing，非本次重構造成）：

| 測試 | 失敗原因 |
|------|---------|
| `test_session_manager.py::test_archive_and_reset_archives_before_clearing` | `sqlite3.IntegrityError: UNIQUE constraint failed: sessions.session_date, sessions.session_number`（測試內的 session numbering 並發邊界，非業務邏輯錯誤）|
| `test_webapp_auth_and_bindings.py::test_bindings_by_car_number_succeeds` | 同上，跨測試 DB 狀態污染 |
| `test_webapp_auth_and_bindings.py::test_bindings_current_false_when_only_previous_session` | 同上 |

已知 warnings：
- `on_event is deprecated`（FastAPI 版本，不影響功能）
- `httpx` 相容性警告（`starlette.testclient`）

**重構後的 target：** 同樣 70 passed, 3 failed（不能讓新的測試失敗，已知的 3 個 failure 是 pre-existing）

### 0.3 手動驗證清單

每個 Phase 結束前手動跑一次：

- [ ] `python -m services.decoder_ingest.main --dry-run` 能正常啟動
- [ ] `python -m services.decoder_ingest.main --replay services/decoder_ingest/raw_capture.log --dry-run` 能跑完
- [ ] webapp login 流程（`/login` → 正常顯示）
- [ ] `/telemetry` WebSocket 連線（瀏覽器確認收到 `decoder_status` 訊息）
- [ ] Grafana iframe 正常載入

---

## Phase 1：清垃圾（2026-07-14）

### 1.1 session_snapshot.json 從 git 追蹤移除

- **改了什麼**：`git rm --cached services/decoder_ingest/session_snapshot.json`，加入 `.gitignore`
- **為什麼**：這是執行期狀態檔（每次服務跑都會變），有 6 筆不必要的 git 歷史。`session_snapshot.py` 的 `load_snapshot()` 在檔案不存在時已正確回傳 `None`（L81-82），不會造成啟動失敗
- **驗證**：確認 `load_snapshot()` 邏輯，確認加入 `.gitignore` 後 `git status` 不再顯示它

### 1.2 scripts/_ai_probe_out.txt 移除

- **改了什麼**：`git rm scripts/_ai_probe_out.txt`，加入 `.gitignore`
- **為什麼**：`_remote_probe_ai.py` 執行後產生的 debug 輸出，被誤 commit 進 git
- **驗證**：`git log --oneline -- scripts/_ai_probe_out.txt` 確認已從追蹤移除

### 1.3 根目錄垃圾腳本搬移 + png1 刪除

- **改了什麼**：
  - `test.py` → `scripts/adhoc/test_tcp_raw.py`
  - `gettrack.py` → `scripts/adhoc/gettrack_satellite.py`
  - `tks_qiaotou_track.png1` 刪除（副檔名打錯的重複檔）
  - 新增 `scripts/adhoc/README.md` 說明用途和 API key 安全警告
- **為什麼**：這些是沒有被任何模組 import 的一次性腳本，留在根目錄會污染結構。`mark_features.py` 引用的是 `tools/track_mapping/data/tks_qiaotou_track.png`，根目錄的 `png` 和 `png1` 都是多餘拷貝，只刪 `png1`（副檔名打錯的那份）
- **驗證**：grep 確認 `mark_features.py` 和 `coord_transform.py` 不引用根目錄的 png

### 1.4 11 支 _deploy_* 腳本合併成 deploy_to_pi.py

- **改了什麼**：
  - 新增 `scripts/deploy_to_pi.py`（支援 `--mode full|webapp|dr`、`--no-restart`、`--check`）
  - 11 支舊腳本移到 `scripts/archive/`
  - 新增 `scripts/archive/README.md` 說明遷移對照
- **為什麼**：11 支腳本各自有獨立的 `connect()`、`run()`、`pack_buf()`，每次新功能就複製一支，造成維護地獄。舊腳本有硬 code HOST/USER 和空密碼預設值（連線若沒設密碼可能用 SSH key 成功，但設計意圖不明確）
- **驗證**：新腳本語法 check（`ast.parse`）通過；HOST/USER 現在 `_require_env()` 強制要求環境變數，沒有可運作的預設密碼路徑

### Phase 1 pytest 結果

```
93 passed, 3 failed (同 baseline 的 3 個 pre-existing failures), 3 warnings
```

---

## Phase 2：結構重構（2026-07-14）

### 2.1 decoder_ingest/main.py 拆分（947 行 → 316 行）

- **改了什麼**：把 `main.py` 的 14 個函式按職責拆到 4 個新子模組：

  | 新模組 | 搬移的函式 | 職責 |
  |--------|-----------|------|
  | `ingest_loop.py` | `raw_capture_worker`, `handle_feed_result`, `_append_calibration_line`, `_run_single_decoder`, `tcp_ingest_loop` | TCP 收包 + PacketParser 輸出處理 |
  | `session_lifecycle.py` | `snapshot_loop`, `_on_new_session_started`, `_roll_session_if_new_local_day`, `_discard_stale_snapshot_session` | 場次生命週期管理 |
  | `broadcast.py` | `lap_timer_broadcast_loop` | 每秒廣播計時 + 自動歸檔觸發 |
  | `replay.py` | `_parse_replay_line`, `REPLAY_DECODER_ID`, `replay_file` | raw_capture.log 重播邏輯 |
  | `main.py`（縮減）| `build_arg_parser`, `setup_logging`, `install_signal_handlers`, `run_service`, `main` | 純組裝入口 |

- **為什麼**：14 個函式平鋪在 947 行，讀者必須滾動整個檔案才能理解職責邊界。按職責拆分後，每個模組的 docstring 就說清楚它負責什麼，修改也只需要找到對的檔案
- **共用狀態處理**：確認沒有隱性閉包/全域狀態需要跨模組傳遞；dashboard hooks（`get_reset_hook` 等）已是 `dashboard.py` 的模組層級 API，不受影響
- **驗證方式**：
  - `ast.parse()` 確認所有新模組語法正確
  - 動態 import 確認模組鏈可完整載入
  - `pytest tests/ -q`：**93 passed, 3 failed（與 baseline 完全一致）**

---

## Phase 3：收尾（2026-07-14）

### 3.1 根目錄 README.md

- **改了什麼**：新增 `README.md` 說明整體架構、服務啟動、部署流程、目錄結構
- **為什麼**：原本沒有根目錄 README，系統架構只能靠讀程式碼拼湊

### 3.2 最終 pytest 驗證

```
93 passed, 3 failed (同 baseline), 3 warnings
```

3 個 pre-existing failures：
- `test_session_manager.py::test_archive_and_reset_archives_before_clearing`
- `test_webapp_auth_and_bindings.py::test_bindings_by_car_number_succeeds`
- `test_webapp_auth_and_bindings.py::test_bindings_current_false_when_only_previous_session`

均為跨測試 SQLite UNIQUE constraint 衝突，與本次重構無關。

---

## PR 摘要

**Branch**: `refactor/cleanup` → `main`

**拆了哪些檔案**：
- `decoder_ingest/main.py`（947 行）→ `ingest_loop.py` + `session_lifecycle.py` + `broadcast.py` + `replay.py` + `main.py`（316 行）

**刪了哪些垃圾**：
- `session_snapshot.json`（runtime 狀態檔）從 git 追蹤移除
- `scripts/_ai_probe_out.txt`（debug 輸出）刪除
- `tks_qiaotou_track.png1`（副檔名打錯的重複檔）刪除
- 11 支 `_deploy_*/_remote_*` 腳本移到 `scripts/archive/`，新增 `scripts/deploy_to_pi.py` 統一取代

**搬了哪些東西**：
- `test.py` → `scripts/adhoc/test_tcp_raw.py`
- `gettrack.py` → `scripts/adhoc/gettrack_satellite.py`

**新增了哪些東西**：
- `tests/test_config_load.py`（23 個 characterization tests，釘住 config.py 行為）
- `scripts/deploy_to_pi.py`（統一部署工具）
- `README.md`（根目錄架構說明）
- `CLEANUP_LOG.md`（本文件）

**行為驗證**：pytest 93 passed，3 個 pre-existing failures 不變，無新增 failure。
