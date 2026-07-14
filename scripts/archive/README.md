# scripts/archive/

這裡存放已被 `scripts/deploy_to_pi.py` 取代的舊部署腳本。

這些腳本是開發期間的一次性 hotfix 腳本，各自硬 code 一份檔案清單和 Pi 連線設定，
彼此高度重複。重構後統一由 `deploy_to_pi.py` 取代，請勿再新增 `_deploy_*.py`。

## 被取代的腳本清單

| 舊腳本 | 原始用途 | 對應新用法 |
|--------|---------|-----------|
| `_deploy_archive_and_fix.py` | archive session + 部署 leaderboard/car-map 修正 | `python scripts/deploy_to_pi.py --mode full` |
| `_deploy_avatar_ai_fix.py` | 部署 avatar + AI coach 修正 | `python scripts/deploy_to_pi.py --mode webapp` |
| `_deploy_bugfix_day_uid.py` | 部署 day-rollover UID 修正 | `python scripts/deploy_to_pi.py --mode webapp` |
| `_deploy_gps_imu_chuck.py` | 部署 GPS+IMU 遙測 + dead reckoning | `python scripts/deploy_to_pi.py --mode full` |
| `_deploy_phase2_pi.py` | Phase 2 auth/bindings 部署 | `python scripts/deploy_to_pi.py --mode full` |
| `_deploy_session_archive_ui.py` | 部署 session archive UI | `python scripts/deploy_to_pi.py --mode webapp` |
| `_deploy_telemetry_ui.py` | 部署即時面板 UI | `python scripts/deploy_to_pi.py --mode webapp` |
| `_fix_grafana_embed_env.py` | 修正 Grafana embed URL .env | `python scripts/deploy_to_pi.py --mode full` |
| `_remote_archive_session.py` | 在 Pi 上直接歸檔 snapshot（遠端執行） | 參考腳本本體邏輯（已整合進 session_manager.archive_and_reset()） |
| `_remote_inspect_session.py` | 在 Pi 上檢查 session snapshot | 直接 SSH 到 Pi 手動查看 |
| `_remote_probe_ai.py` | 在 Pi 上探查 AI 模型可用性 | 直接 SSH 到 Pi 執行 |

## 新的部署方式

```bash
# 必填環境變數
export KPP_PI_HOST=100.102.122.104
export KPP_PI_USER=evan
# 有 SSH key 不需要 KPP_PI_PASS

# 全量部署（services/ + infra/ + docker-compose）
python scripts/deploy_to_pi.py --mode full

# 只更新 webapp（快速修 UI bug）
python scripts/deploy_to_pi.py --mode webapp

# 只更新 dead reckoning 服務
python scripts/deploy_to_pi.py --mode dr

# 只跑 smoke check（不上傳）
python scripts/deploy_to_pi.py --check

# 上傳但不重啟服務（比賽中謹慎更新用）
python scripts/deploy_to_pi.py --no-restart
```
