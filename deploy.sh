#!/usr/bin/env bash
# SmartKart 雙 Pi 零停機 rolling deploy（在部署機 / 筆電上跑）
#
# 前置：
#   - RPi5 (kpp)  = keepalived MASTER, priority 150
#   - RPi4 (chuck)= keepalived BACKUP, priority 100
#   - VIP         = 192.168.88.100 （兩台必須同 L2；目前若不同網段則 VIP failover 無效）
#   - FastAPI     = systemd --user kpp-dashboard.service :8000
#
# Usage: ./deploy.sh
set -euo pipefail

RPI5="kpp@kpp"          # RPi5 via Tailscale
RPI4="evan@chuck"       # RPi4 via Tailscale
VIP="192.168.88.100"
REPO_PATH="\$HOME/kpp"
DASHBOARD_PORT=8000
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)

ssh_pi() {
    local host=$1
    shift
    ssh "${SSH_OPTS[@]}" "$host" "$@"
}

get_current_master() {
    if ssh_pi "$RPI5" "ip addr show eth0 2>/dev/null | grep -q '${VIP}'"; then
        echo "rpi5"
    elif ssh_pi "$RPI4" "ip addr show eth0 2>/dev/null | grep -q '${VIP}'"; then
        echo "rpi4"
    else
        # VIP 尚未掛上（常見於兩台不同網段）：預設以 RPi5 為 MASTER 角色
        echo "rpi5-novip"
    fi
}

deploy_to() {
    local host=$1
    echo ">>> 部署到 $host ..."
    ssh_pi "$host" "bash -s" <<EOF
set -euo pipefail
cd \$HOME/kpp
if [ -d .git ]; then
  git pull --ff-only || git pull
else
  echo "WARNING: \$HOME/kpp 不是 git checkout，跳過 git pull"
fi
if [ -x .venv/bin/pip ]; then
  .venv/bin/pip install -q -r requirements.txt
fi
# Grafana/Influx 只重啟若 compose 有變更；平常 web 更新不碰
systemctl --user restart kpp-dashboard.service
EOF
}

wait_healthy() {
    local host=$1
    echo ">>> 等待 $host 健康..."
    for i in $(seq 1 30); do
        if ssh_pi "$host" "curl -sf --max-time 2 http://127.0.0.1:${DASHBOARD_PORT}/health" >/dev/null 2>&1; then
            echo ">>> $host 健康檢查通過"
            return 0
        fi
        sleep 2
    done
    echo "!!! $host 健康檢查逾時,中止部署,不切換VIP"
    exit 1
}

force_failover_away_from() {
    local host=$1
    echo ">>> 降低 $host priority (track_file=1 * weight=-100),觸發VRRP failover..."
    # keepalived: delta = file_value * weight；寫 1 才會 -100，寫負數會變加分
    ssh_pi "$host" "echo 1 > /opt/smartkart/priority_override"
    sleep 3
}

restore_priority() {
    local host=$1
    ssh_pi "$host" "echo 0 > /opt/smartkart/priority_override" || true
}

show_version() {
    local label=$1 url=$2
    echo -n ">>> $label version: "
    curl -sf --max-time 3 "$url" || echo "(unreachable)"
    echo
}

MASTER=$(get_current_master)
case "$MASTER" in
    rpi5|rpi5-novip)
        STANDBY_HOST=$RPI4; STANDBY_NAME="chuck(rpi4)"
        MASTER_HOST=$RPI5; MASTER_NAME="kpp(rpi5)"
        ;;
    rpi4)
        STANDBY_HOST=$RPI5; STANDBY_NAME="kpp(rpi5)"
        MASTER_HOST=$RPI4; MASTER_NAME="chuck(rpi4)"
        ;;
esac

echo "=== 目前 MASTER: $MASTER_NAME ($MASTER), 先部署 STANDBY: $STANDBY_NAME ==="
deploy_to "$STANDBY_HOST"
wait_healthy "$STANDBY_HOST"

if [ "$MASTER" != "rpi5-novip" ]; then
    echo "=== STANDBY確認健康,切換VIP離開 $MASTER_NAME ==="
    force_failover_away_from "$MASTER_HOST"
    sleep 2
    show_version "VIP" "http://${VIP}:${DASHBOARD_PORT}/version"
else
    echo "=== 警告: VIP 未掛上（兩台可能不同 L2 網段），跳過 VRRP failover，改順序重啟 ==="
fi

echo "=== 部署舊MASTER: $MASTER_NAME ==="
deploy_to "$MASTER_HOST"
wait_healthy "$MASTER_HOST"

echo "=== 兩台皆已更新,恢復正常 priority（RPi5 preempt 回 MASTER）==="
restore_priority "$RPI5"
restore_priority "$RPI4"

echo "=== 部署完成 ==="
show_version "kpp local" "http://kpp:${DASHBOARD_PORT}/version" 2>/dev/null || \
  ssh_pi "$RPI5" "curl -sf http://127.0.0.1:${DASHBOARD_PORT}/version" || true
ssh_pi "$RPI4" "curl -sf http://127.0.0.1:${DASHBOARD_PORT}/version" || true
if [ "$MASTER" != "rpi5-novip" ]; then
    show_version "VIP" "http://${VIP}:${DASHBOARD_PORT}/version"
fi
