# 樹莓派部署（InfluxDB + Grafana）

**不要**在 Windows 跑 Docker Desktop。遙測 DB / Grafana 一律上 Pi。

## 網段候選（本機 ARP，MAC `e4:5f:01` = RPi）

- `192.168.0.105`
- `192.168.0.115`

先 `ssh pi@192.168.0.105`（或你的帳號）確認哪台是對的。

## 一鍵

在 **Pi** 上：

```bash
# 把整個 kpp repo 弄上 Pi（擇一）
git clone <你的 repo URL> ~/kpp
# 或從 Windows：
#   scp -r C:\Users\evand\Desktop\kpp pi@192.168.0.105:~/kpp

cd ~/kpp
bash infra/pi/bootstrap.sh
```

起來後：

| 服務 | URL |
|------|-----|
| InfluxDB | `http://<PI_IP>:8086` |
| Grafana | `http://<PI_IP>:3000/grafana/`（admin / `kpp-grafana-admin`） |

## Windows 只改 `.env`（網站/decoder 仍可在 PC，但寫入指到 Pi）

```
INFLUX_URL=http://192.168.0.105:8086
INFLUX_TOKEN=kpp-dev-influx-token-change-me
INFLUX_ORG=kpp
INFLUX_BUCKET=decoder
GRAFANA_EMBED_URL=http://192.168.0.105:3000/grafana/d/kart-telemetry/karting?orgId=1&kiosk&theme=dark&refresh=2s
```

重啟：

```powershell
.venv\Scripts\python -m services.decoder_ingest.main --with-dashboard
```

Attitude EKF（IMU → `attitude` measurement，另開一個 terminal）：

```powershell
.venv\Scripts\python -m services.attitude_ekf.main
```

賽道彎道標記（本機互動，需 GUI）：

```powershell
cd tools\track_mapping
..\..\.venv\Scripts\python mark_features.py
```

ESP 繼續 POST 到跑 FastAPI 的那台（PC 或之後也搬 Pi）。ingest 會寫進 Pi 的 Influx。

Grafana Phase 2 panels（traction circle / Grip% / yaw / geomap）在 `infra/grafana/dashboards/kart-telemetry.json`；Pi 上更新 repo 後重啟 Grafana container 即可。

## Decoder 圈時校正（務必確認）

`.env` 必須：

```
DECODER_TICK_HZ=256000
```

Wire 上 `$[tid12][ticks8][强度4]\\r\\n` 的 ticks 是 **ASCII hex**（`int(s, 16)`），
圈時 = `(tick2 - tick1) % 2^32 / 256000`。

**不要**把 Wireshark 可印字元當十進位去反推 Hz（會得到錯誤的 ~14250）。
空字串 `DECODER_TICK_HZ=` 會關掉 tick、改用 wall-clock——僅限 debug，正式環境不要。

## 防火牆（Pi）

```bash
sudo ufw allow 8086/tcp
sudo ufw allow 3000/tcp
```
