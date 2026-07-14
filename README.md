# KPP 卡丁車遙測系統

橋頭 TKS 賽道即時圈速遙測與 AI 教練報告系統。

## 系統架構

```
ESP32（車上）
  │  TCP（MYLAPS AmbRC wire format）
  ▼
decoder_ingest（Windows 主機）
  │  解析封包 → 計算圈速 → 寫入 InfluxDB
  │  WebSocket 廣播即時圈速（:8000 / :5000）
  ▼
InfluxDB（Pi / chuck）          SQLite（本機）
  │                               │
  ▼                               ▼
Grafana（Pi，iframe 嵌入）    webapp FastAPI（Python）
                                  │  登入、歷史、AI 教練報告
                                  ▼
                              瀏覽器（使用者）
```

## 服務說明

| 服務 | 路徑 | 說明 |
|------|------|------|
| `decoder_ingest` | `services/decoder_ingest/` | TCP 收 ESP32 封包、解碼、計算圈速、寫 InfluxDB；內建 FastAPI WebSocket 即時面板 |
| `webapp` | `services/webapp/` | 使用者登入（Google OAuth）、歷史場次、AI 教練報告；內部掛載 decoder_ingest 的 FastAPI app |
| `attitude_ekf` | `services/attitude_ekf/` | 姿態估計（EKF） |
| `dead_reckoning` | `services/dead_reckoning/` | GPS+IMU 航位推算 |

## 開發環境啟動

```bash
# 建立虛擬環境（第一次）
python -m venv .venv
.venv\Scripts\activate      # Windows
pip install -r requirements.txt

# 複製並編輯設定
cp .env.example .env

# 啟動 webapp（含 decoder_ingest --with-dashboard）
python -m services.decoder_ingest.main --with-dashboard

# 只跑 decoder（不帶 web UI）
python -m services.decoder_ingest.main --dry-run

# 重播歷史 log 驗證解碼邏輯
python -m services.decoder_ingest.main --replay services/decoder_ingest/raw_capture.log --dry-run

# 跑測試
python -m pytest tests/ -q
```

## Pi（chuck）部署

```bash
# 必填環境變數
export KPP_PI_HOST=100.102.122.104
export KPP_PI_USER=evan
# 有 SSH key 則不需要 KPP_PI_PASS

# 全量部署（services/ + infra/ + docker-compose）
python scripts/deploy_to_pi.py --mode full

# 只更新 webapp（快速修 UI）
python scripts/deploy_to_pi.py --mode webapp

# 只跑 smoke check（確認服務健康，不上傳）
python scripts/deploy_to_pi.py --check
```

Pi 上執行的服務：
- InfluxDB（docker-compose）
- Grafana（docker-compose，provisioning 設定在 `infra/grafana/`）
- `kpp-dashboard.service`（systemd user service，執行 `decoder_ingest.main --with-dashboard`）

## 主要環境變數（`.env`）

| 變數 | 說明 |
|------|------|
| `DECODER_HOST` | MYLAPS decoder IP |
| `DECODER_PORT` | TCP port（預設 8899）|
| `INFLUX_URL` | InfluxDB URL |
| `INFLUX_TOKEN` | InfluxDB API token |
| `INFLUX_ORG` | InfluxDB org |
| `INFLUX_BUCKET` | InfluxDB bucket |
| `CAR_NUMBER_MAP` | `transponder:車號` 逗號分隔對照表 |
| `GRAFANA_EMBED_URL` | Grafana iframe URL |
| `SECRET_KEY` | webapp session 加密 key |

完整清單見 `.env.example`。

## 目錄結構

```
kpp/
├── services/
│   ├── decoder_ingest/
│   │   ├── main.py              # 組裝入口（CLI + run_service）
│   │   ├── ingest_loop.py       # TCP 收包 + PacketParser 輸出處理
│   │   ├── session_lifecycle.py # 場次生命週期（跨日換日、snapshot）
│   │   ├── broadcast.py         # 每秒廣播計時 + 自動歸檔觸發
│   │   ├── replay.py            # raw_capture.log 重播邏輯
│   │   ├── config.py            # 環境變數解析
│   │   ├── lap_tracker.py       # per-transponder 圈速狀態機
│   │   ├── packet_parser.py     # MYLAPS wire format 解碼
│   │   ├── influx_writer.py     # InfluxDB 寫入（batch + fallback）
│   │   ├── influx_reader.py     # InfluxDB 讀取（歷史查詢）
│   │   ├── session_manager.py   # session_id 管理與歸檔
│   │   ├── session_snapshot.py  # 本地 JSON snapshot（崩潰復原用）
│   │   ├── dashboard.py         # FastAPI WebSocket 面板 API
│   │   └── tcp_client.py        # 自動重連 TCP client
│   ├── webapp/
│   │   ├── app.py               # FastAPI app 組裝
│   │   ├── auth.py              # Google OAuth
│   │   ├── ai_coach.py          # AI 教練報告（background job + API）
│   │   ├── history.py           # 歷史場次 API
│   │   ├── telemetry.py         # 即時遙測 WebSocket
│   │   ├── session_control.py   # 手動重置/歸檔 API
│   │   ├── models.py            # SQLAlchemy models
│   │   └── migrations/          # Alembic migrations
│   ├── attitude_ekf/
│   └── dead_reckoning/
├── scripts/
│   ├── deploy_to_pi.py          # 部署到 Pi 的統一工具
│   ├── archive/                 # 已被 deploy_to_pi.py 取代的舊腳本
│   └── adhoc/                   # 一次性調試腳本
├── tools/
│   └── track_mapping/           # 賽道座標轉換工具
├── infra/
│   └── grafana/                 # Grafana provisioning 設定
├── tests/                       # pytest 測試套件
├── ESP32/                       # PlatformIO 韌體
├── docker-compose.yml           # Pi 上的 InfluxDB + Grafana
├── requirements.txt
├── alembic.ini
├── .env.example
└── CLEANUP_LOG.md               # 重構歷程記錄
```
