# 樹莓派部署（InfluxDB + Grafana + Dual-Pi HA）

**不要**在 Windows 跑 Docker Desktop。遙測 DB / Grafana 一律上 Pi。

## 雙 Pi 拓樸（現行）

| 角色 | Tailscale | 帳號 | LAN | keepalived |
|------|-----------|------|-----|------------|
| RPi5 MASTER | `kpp` (`100.70.246.88`) | `kpp` | `192.168.88.62/24` | priority 150 |
| RPi4 BACKUP | `chuck` (`100.102.122.104`) | `evan` | `192.168.0.105/24` | priority 100 |
| VIP | — | — | `192.168.88.100/24` | VRRP id 51 |

**重要：** keepalived VRRP 需要兩台同 L2。目前 kpp / chuck 在不同網段，
VIP failover **在兩邊合到同一 LAN 之前無效**。合網後 VIP 才會真正飄移。

infra/pi/README.md

```bash
sudo systemctl enable --now keepalived
```

前端 / Grafana / API 讀取走 VIP（合網後）；ESP32 ingest 建議雙寫兩台實體 IP。

## NTP

RPi5（`kpp`）所在網路擋對外 UDP/123，改指 gateway：

```
# infra/pi/timesyncd-gateway.conf → /etc/systemd/timesyncd.conf.d/10-gateway.conf
[Time]
NTP=192.168.88.1
FallbackNTP=time.cloudflare.com pool.ntp.org
```

`chuck` 可打到 debian pool，保持預設即可（不要裝 192.168.88.1 drop-in）。

驗證：`timedatectl` → `System clock synchronized: yes`

## 一鍵 bootstrap（單機 Influx+Grafana）

在 **Pi** 上：

```bash
git clone <repo> ~/kpp
cd ~/kpp
bash infra/pi/bootstrap.sh
```

起來後：

| 服務 | URL |
|------|-----|
| InfluxDB | `http://<PI_IP>:8086` |
| Grafana | `http://<PI_IP>:3000/grafana/`（admin / `kpp-grafana-admin`） |
| FastAPI | `http://<PI_IP>:8000`（`kpp-dashboard.service`） |
| health | `GET /health`、`GET /version`（免登入） |

## Dual-Pi 佈署（從筆電）

```powershell
# 首次把 kpp 佈齊 + 兩台裝 keepalived
python scripts/provision_dual_pi.py

# 之後零停機 rolling deploy
# （Git Bash / WSL）
./deploy.sh
```

`deploy.sh` 流程：STANDBY 更新 → 健康檢查 → `echo 1 > /opt/smartkart/priority_override`
（keepalived `delta = file * weight(-100)` → priority -100）觸發 VIP 離開舊 MASTER →
更新舊 MASTER → 兩邊 `echo 0` 歸零（RPi5 preempt 回 MASTER）。

健康檢查腳本：`/opt/smartkart/health_check.sh`（FastAPI :8000 + Influx :8086 皆 200）。

## 暫時單 Pi 模式（RPi5）

VRRP 尚未合網期間，網站只跑在 `kpp`：

- Pi5 `kpp-dashboard.service`：`:8000`
- Cloudflare tunnel 固定連 `127.0.0.1:5000`，由
  `kpp-port5000-proxy.socket` 轉到 `:8000`
- Pi4 的 `kpp-dashboard.service` 與 `cloudflared.service` 保持停止
- Pi5 暫時透過 Tailscale `100.102.122.104:8086` 讀取 Pi4 現有 InfluxDB

**現場 W610 在 `192.168.0.0/24` 時（2026-07 起）：** 即時 ingest / 對外
`chuck.dctggest.filegear-sg.me` 改跑 **Pi4（chuck）**——`kpp-dashboard` +
`cloudflared` 在 chuck；**Pi5 的 `cloudflared` 必須停掉**，否則同一 tunnel
token 會搶連線、外網 502。Pi5 只保留 Influx/Grafana 備援或 88 網段用途。

## keepalived 檔案

- [`keepalived/keepalived.rpi5.conf`](keepalived/keepalived.rpi5.conf)
- [`keepalived/keepalived.rpi4.conf`](keepalived/keepalived.rpi4.conf)
- [`health_check.sh`](health_check.sh)
- [`kpp-dashboard.service`](kpp-dashboard.service)
- [`kpp-port5000-proxy.socket`](kpp-port5000-proxy.socket)
- [`kpp-port5000-proxy.service`](kpp-port5000-proxy.service)
- [`timesyncd-gateway.conf`](timesyncd-gateway.conf)

## Windows / ESP `.env` 指到 Pi

```
INFLUX_URL=http://192.168.88.62:8086
# 或合網後雙寫兩台實體 IP
INFLUX_TOKEN=kpp-dev-influx-token-change-me
INFLUX_ORG=kpp
INFLUX_BUCKET=decoder
DASHBOARD_PORT=8000
GRAFANA_EMBED_URL=http://192.168.88.100:3000/grafana/d/kart-telemetry/kart-telemetry-f1?orgId=1&kiosk&theme=dark&refresh=2s
```

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
sudo ufw allow 8000/tcp
# VRRP (IP proto 112) — 同 L2 時需要
sudo ufw allow in on eth0 proto vrrp
```
