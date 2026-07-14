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

## Phase 1：清垃圾（進行中）

_（此 phase 完成後補填）_

---

## Phase 2：結構重構

_（此 phase 完成後補填）_

---

## Phase 3：收尾

_（此 phase 完成後補填）_
