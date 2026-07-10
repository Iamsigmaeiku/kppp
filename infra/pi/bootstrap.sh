#!/usr/bin/env bash
# 在樹莓派上執行：把 InfluxDB + Grafana 拉起來（不要跑在 Windows）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

if ! command -v docker >/dev/null 2>&1; then
  echo "安裝 Docker…"
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER" || true
  echo "若剛裝 Docker，請 logout/login 後再跑一次此腳本"
fi

echo "啟動 InfluxDB + Grafana…"
docker compose pull
docker compose up -d

echo
docker compose ps
echo
echo "Influx : http://$(hostname -I | awk '{print $1}'):8086"
echo "Grafana: http://$(hostname -I | awk '{print $1}'):3000/grafana/"
echo "  admin / kpp-grafana-admin"
echo
echo "Windows / ESP 端 .env 請設："
echo "  INFLUX_URL=http://$(hostname -I | awk '{print $1}'):8086"
echo "  INFLUX_TOKEN=kpp-dev-influx-token-change-me"
echo "  INFLUX_ORG=kpp"
echo "  INFLUX_BUCKET=decoder"
echo "  GRAFANA_EMBED_URL=http://$(hostname -I | awk '{print $1}'):3000/grafana/d/kart-telemetry/karting?orgId=1&kiosk&theme=dark&refresh=2s"
