# KPP 專案重構指令（餵給執行 AI 用）

角色設定：你是資深後端/系統架構工程師，負責在**不改變任何對外行為**的前提下，把這個卡丁車遙測系統（ESP32 → decoder_ingest → InfluxDB/Grafana → webapp）的程式碼從「能動但亂」整理成「架構乾淨、可維護」。這不是重寫，是重構（refactor），黃金準則是 **behavior parity**：使用者、Pi 上的部署腳本、Grafana 面板、ESP32 韌體看到的所有輸入輸出，重構前後必須完全一致。

## 鐵律（不可違反）

1. **禁止大爆炸重寫**。一次只動一個模組/一個關注點，每次改動都要能獨立跑測試、獨立 commit、獨立 revert。
2. **先建安全網，再動刀**。任何檔案在被重構前，必須先確認有測試覆蓋（`tests/` 目錄）或至少有可重現的手動驗證步驟；沒有測試的模組，先補「characterization test」（把現有行為原封不動釘住的測試）再重構，不要邊重構邊補測試。
3. **不准改的東西**（除非使用者明確要求）：
   - ESP32 韌體（`ESP32/src/*`）與其 TCP/序列封包格式 — decoder_ingest 的 `packet_parser.py` 依賴這個 wire format，改了會炸整條線。
   - `services/webapp/models.py` 與 Alembic migrations（`services/webapp/migrations/`）的 DB schema — 要動就必須生對應 migration，不能直接改欄位了事。
   - WebSocket / API 的 response JSON 結構（`dashboard.py`, `telemetry.py`, `history.py` 對外的欄位名稱與型別）。
   - `.env` / `docker-compose.yml` 裡的環境變數名稱（外部 Pi 部署腳本、Grafana provisioning 依賴這些名稱）。
   - 允許改的是「內部結構」：檔案怎麼切、函式怎麼命名、模組怎麼分層、重複程式碼怎麼合併 — 只要對外行為不變。
4. 每個 phase 結束都要跑：`python -m pytest tests/ -q`，全綠才能進下一個 phase。這專案目前測試不完整（見下），跑不動的部分要用手動驗證清單頂替。
5. 每次結構性搬動（搬檔案、拆函式、改 import）都獨立成一個 commit，訊息用英文或中文都可以，但要說清楚「搬了什麼、為什麼」，不要用「大更新」「renew」「ch」這種訊息（這是目前 git log 的真實問題，不要重蹈覆轍）。
6. 過程中在專案根目錄維護一份 `CLEANUP_LOG.md`，每個 phase 完成後追加一段：改了什麼檔案、為什麼、怎麼驗證過行為沒變。這是給人類複查用的，不是選配。

## 專案現況速覽（已核實，執行 AI 不用重新探索）

- 技術棧：Python 3.13 + FastAPI + SQLAlchemy(async, sqlite) + Alembic + InfluxDB2 + Jinja2，前端純 Alpine.js，Grafana 用 iframe 嵌入。ESP32 韌體用 PlatformIO/C++。
- 四個服務：`services/decoder_ingest`（TCP 收 ESP32 封包、解碼、算圈速、寫 Influx）、`services/webapp`（登入、歷史、AI 教練報告、即時面板，內部掛載 decoder_ingest 的 FastAPI app）、`services/attitude_ekf`、`services/dead_reckoning`（獨立小服務）。
- 部署方式：裸機 Windows 主機跑 webapp，Pi（代號 "chuck"）跑 InfluxDB+Grafana（docker-compose），目前用一堆 `scripts/_deploy_*.py`（內建 paramiko，SSH 把個別檔案 push 到 Pi）手動同步程式碼 — 這是技術債，見下方 Phase 1。
- git log 訊息品質很差（"大更新" "renew" "ch" "hah"），說明過去是邊做邊改沒有章法地累積出來的，這正是要清理的「屎山」本體。

## Phase 0：建安全網（不改任何程式碼）

1. 建一個新分支 `refactor/cleanup`，之後所有工作都在這個分支做，禁止直接改 `main`。
2. 在使用者的機器上（有 `.venv`，這裡不是）跑 `python -m pytest tests/ -q`，記錄目前有幾個 pass / fail / skip，存進 `CLEANUP_LOG.md` 當 baseline。已知測試檔案：`tests/test_ai_coach_helpers.py`、`test_auto_archive_frozen.py`、`test_avatars_helper.py`、`test_influx_reader_sessions.py`、`test_lap_history_uid_drift.py`、`test_lap_tracker.py`、`test_packet_parser_tick.py`、`test_session_manager.py`、`test_session_numbering.py`、`test_session_snapshot.py`、`test_transponder_normalize.py`、`test_webapp_auth_and_bindings.py`、`test_webapp_models.py`。
3. 針對測試覆蓋不到、但要重構的高風險模組（尤其 `decoder_ingest/main.py`、`decoder_ingest/config.py`、`webapp/ai_coach.py`），補 characterization test：對外部輸入丟固定資料，把現在的輸出/side effect 存成 golden output，重構後跑同一組輸入比對輸出完全一致再算過關。
4. 列出手動驗證清單（沒辦法自動化的部分）：webapp 登入流程、`/telemetry` WebSocket 即時面板連線、Grafana iframe 嵌入、decoder_ingest 收到 ESP32 封包後能正確寫入 Influx 並被 lap_tracker 判圈。每個 phase 結束前手動跑一次這份清單。

## Phase 1：清垃圾（低風險，優先做，成就感也最高）

以下是**目前已經被 git 追蹤**的明確垃圾/技術債案例，逐項處理：

- 根目錄 `test.py`（24行，讀 secrets/socket 的臨時腳本）、`gettrack.py`（32行，requests+math 臨時腳本）— 確認沒有其他程式碼 import 它們後，移進 `scripts/adhoc/` 或直接刪除（先問使用者要留存證據還是直接刪）。
- `tks_qiaotou_track.png1`（副檔名打錯的重複檔，`tks_qiaotou_track.png` 才是正確版本，`tools/track_mapping/data/` 底下還有一份一樣的）— 確認 `tools/track_mapping/mark_features.py`、`coord_transform.py` 實際引用哪一份，統一路徑後刪多餘拷貝。
- `services/decoder_ingest/session_snapshot.json` — 這是**執行期狀態檔**（每次跑服務都會變），卻被 commit 進 git，git status 顯示它一直在變動。加進 `.gitignore`，跑 `git rm --cached services/decoder_ingest/session_snapshot.json`，並確認 `session_snapshot.py` 在檔案不存在時會自己生成預設值（不會炸）。
- `scripts/_ai_probe_out.txt` — debug 輸出檔被誤 commit，刪除並加進 `.gitignore`（比對 `_remote_probe_ai.py` 是不是產生這個檔案的腳本，順手看要不要把輸出導到 `.gitignore` 掉的目錄）。
- `scripts/_deploy_*.py`（7支：`_deploy_archive_and_fix.py`、`_deploy_avatar_ai_fix.py`、`_deploy_bugfix_day_uid.py`、`_deploy_gps_imu_chuck.py`、`_deploy_phase2_pi.py`、`_deploy_session_archive_ui.py`、`_deploy_telemetry_ui.py`）與 `scripts/_remote_*.py`（`_remote_archive_session.py`、`_remote_inspect_session.py`、`_remote_probe_ai.py`）、`scripts/_fix_grafana_embed_env.py` — 這些都是「一次性」用 paramiko SSH 把特定檔案清單 push 到 Pi 的腳本，彼此高度重複（各自硬 code 一份 `FILES = [...]` 清單、硬 code host/user）。處理方式：
  1. 讀完全部 10 支，抽出共用邏輯（SSH 連線、打包上傳、重啟遠端服務）合併成一個 `scripts/deploy_to_pi.py`，支援用參數指定要同步哪些路徑（或直接同步整個 `services/` + `docker-compose.yml`），而不是每次新增功能就複製一支新腳本。
  2. `HOST`/`USER`/`PASSWORD` 目前是硬 code 預設值（`100.102.122.104` 等），全部改成必須從 `.env`/環境變數讀，沒有就報錯，不要留可運作的預設密碼路徑。
  3. 舊的 7+3+1 支腳本移進 `scripts/archive/`（保留歷史可查）或直接刪除，README 註明「已被 `deploy_to_pi.py` 取代」。
- 檢查 `services/webapp/kpp.sqlite3` 是否曾經被 commit 過（目前在 `.gitignore` 但先確認 `git log --all --oneline -- services/webapp/kpp.sqlite3` 有沒有歷史紀錄），若有，跟使用者確認要不要用 `git filter-repo` 清掉歷史（這個動作有風險，一定要問過使用者再做，不要自作主張改 git 歷史）。
- `ESP32/.pio/` 是 PlatformIO 建置產物（裡面整份 ArduinoJson/TinyGPSPlus 原始碼都被複製進去了），已在 `.gitignore` 但先確認沒有被誤 commit。

Phase 1 完成後跑一次 `git status`，確認沒有新增未預期的差異，且 Phase 0 的手動驗證清單全過。

## Phase 2：結構重構（每個項目獨立分支/commit，做完一個驗證一個）

按風險由低到高排序，**不要跳著做**：

1. **`services/decoder_ingest/config.py`（386行）**：檔案開頭註解寫「不含業務邏輯」，但長度明顯超過一個純設定檔該有的量，先讀一遍確認是否混入了驗證邏輯/業務規則。若有，把「讀環境變數 + dataclass 定義」跟「驗證/轉換邏輯」拆成 `config.py` + `config_validation.py`，或至少用清楚的區塊/函式分組。
2. **`scripts/_deploy_*` 系列合併**（同 Phase 1 最後一項，若還沒做就在這裡做）。
3. **`services/webapp/ai_coach.py`（463行）與 `services/decoder_ingest/lap_tracker.py`（501行）**：這兩個是目前次大的檔案。先畫出檔案內部有哪幾個職責（例如 lap_tracker 可能同時管：圈速判定狀態機、UID/transponder 正規化、session 邊界判斷），畫完職責清單先給使用者確認再拆，避免拆錯邊界。拆分後每個新模組要有清楚的單一職責，並且原本的 public API（其他檔案 import 的函式/類別名稱）維持不變或用 `__init__.py` re-export，避免動到所有呼叫端。
4. **`services/decoder_ingest/main.py`（946行，全專案最大檔案，14個函式全部平鋪在同一層）**：這是主要目標。現有函式已經有清楚的職責邊界（`raw_capture_worker`、`snapshot_loop`、`handle_feed_result`、`lap_timer_broadcast_loop`、`replay_file`、`tcp_ingest_loop`、`run_service`、`main` 等），照職責拆成子模組，例如：
   - `bootstrap.py`：`build_arg_parser`、`setup_logging`、`main`、`install_signal_handlers`
   - `ingest_loop.py`：`tcp_ingest_loop`、`_run_single_decoder`、`raw_capture_worker`
   - `session_lifecycle.py`：`_on_new_session_started`、`_roll_session_if_new_local_day`、`_discard_stale_snapshot_session`、`snapshot_loop`
   - `replay.py`：`_parse_replay_line`、`replay_file`
   - `broadcast.py`：`lap_timer_broadcast_loop`
   - `main.py` 縮成組裝入口，只剩 import + `run_service` 串接這些模組。
   拆之前先確認這些函式之間有沒有共享的可變狀態（closure 變數、全域變數），有的話要在拆分時明確定義成一個 context/state 物件傳遞，不要留下隱性耦合。
5. **`services/webapp/` 底下的路由模組**（`pages.py`、`session_control.py`、`history.py`、`telemetry.py`、`car_bindings.py`、`avatars.py`、`auth.py` 等）：這部分本身已經是照 router 分檔案的合理結構，重點檢查有沒有：跨檔案重複的資料庫查詢邏輯（該合併成 `db`/`repository` 層的共用函式）、重複的權限檢查邏輯（該合併進 `auth_gate.py`/`deps.py`）。不需要大動，做增量整理即可。

每完成一項，跑 `pytest`、跑 Phase 0 的手動驗證清單、把行為對照結果寫進 `CLEANUP_LOG.md`，再進下一項。

## Phase 3：收尾

1. 更新 `README`（目前沒有根目錄 README，只有各服務自己的；視情況新增一份根目錄 `README.md` 說明整體架構圖：ESP32 → decoder_ingest → InfluxDB/webapp → Grafana，取代原本只能靠讀程式碼才拼得出來的架構認知）。
2. 檢查所有 `import` 沒有殘留指向被搬移/刪除檔案的路徑。
3. 最終跑一次完整測試 + 完整手動驗證清單，跟 Phase 0 記錄的 baseline 逐項比對，一致才能合併回 `main`。
4. 合併前把 `CLEANUP_LOG.md` 整理成一份簡短的 PR/commit description，列出「拆了哪些檔案、刪了哪些垃圾、行為驗證方式」。

## 給執行 AI 的提醒

- 每次動手前先講清楚「這一步要改什麼、為什麼、怎麼驗證」，不要一次丟出一大包 diff 讓人審不動。
- 不確定某段程式碼是不是還在用（尤其 `scripts/` 底下那些 `_` 開頭的檔案），先用 grep 全專案搜尋有沒有其他地方 import/呼叫它，不要用「看起來沒用」當理由砍掉。
- 遇到 Phase 2 第 3、4 項這種需要拆邊界的決策，先提出拆分方案讓使用者確認，不要自己決定完就直接大改。
