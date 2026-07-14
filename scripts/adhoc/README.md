# scripts/adhoc/

臨時用腳本的存放區。這些腳本不是系統的一部分，也沒有被任何模組 import，
純粹是開發期間的調試/探索工具。

## 腳本說明

| 檔案 | 原始路徑 | 用途 |
|------|---------|------|
| `test_tcp_raw.py` | 根目錄 `test.py` | 直連 decoder TCP socket，把收到的資料印成 hex，用來手動驗證 decoder 硬體的封包格式 |
| `gettrack_satellite.py` | 根目錄 `gettrack.py` | 呼叫 Google Maps Static API 抓橋頭 TKS 賽道衛星圖；**API key 已硬 code（務必確認已 revoke 或替換）** |

> [!WARNING]
> `gettrack_satellite.py` 含有一把 Google Maps API key（`AIzaSyDn...`），已記錄在 git 歷史中。
> 如果這把 key 還有效，請立即至 Google Cloud Console → APIs & Services → Credentials 撤銷它。
> 正確做法：把 key 移到 `.env`，腳本改從環境變數讀取。
