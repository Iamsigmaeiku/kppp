# 基礎設施（InfluxDB + Grafana）

```bash
docker compose up -d
```

| 服務 | URL | 帳密 |
|------|-----|------|
| InfluxDB | http://localhost:8086 | org=`kpp` / token 見 compose / bucket=`decoder` |
| Grafana | http://localhost:3000/grafana/ | admin / `kpp-grafana-admin` |

對齊根目錄 `.env`（**Influx/Grafana 在樹莓派**，見 [`pi/README.md`](pi/README.md)）：

```
INFLUX_URL=http://192.168.0.105:8086
INFLUX_TOKEN=kpp-dev-influx-token-change-me
INFLUX_ORG=kpp
INFLUX_BUCKET=decoder
TELEMETRY_INGEST_TOKEN=kpp-telemetry-ingest-token-change-me
GRAFANA_EMBED_URL=http://192.168.0.105:3000/grafana/d/kart-telemetry/karting?orgId=1&kiosk&theme=dark&refresh=2s
```

在 Pi 上：`bash infra/pi/bootstrap.sh`  
不要在 Windows 跑 `docker compose`。
