"""Sync GPS+IMU stack to chuck: webapp telemetry UI, Grafana geomap, dead_reckoning."""
from __future__ import annotations

import io
import os
import tarfile
import time
from pathlib import Path

import paramiko

HOST = os.environ.get("KPP_PI_HOST", "100.102.122.104")
USER = os.environ.get("KPP_PI_USER", "evan")
PASSWORD = os.environ.get("KPP_PI_PASS", "")
ROOT = Path(__file__).resolve().parents[1]

INCLUDE_DIRS = ["services", "infra"]
INCLUDE_FILES = ["requirements.txt", "docker-compose.yml", "alembic.ini"]


def connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = dict(
        hostname=HOST,
        username=USER,
        timeout=20,
        allow_agent=True,
        look_for_keys=True,
    )
    if PASSWORD:
        connect_kwargs["password"] = PASSWORD
        connect_kwargs["allow_agent"] = False
        connect_kwargs["look_for_keys"] = False
    try:
        c.connect(**connect_kwargs)
    except paramiko.AuthenticationException:
        if PASSWORD:
            raise
        raise SystemExit(
            "SSH auth failed — set KPP_PI_PASS or ensure your SSH key is authorized on chuck"
        )
    return c


def run(c, cmd, check=True):
    print(f"$ {cmd[:200]}")
    _, stdout, stderr = c.exec_command(cmd, get_pty=True)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    code = stdout.channel.recv_exit_status()
    text = (out + err).encode("ascii", "replace").decode("ascii")
    if text.strip():
        print(text.rstrip()[-5000:])
    if check and code != 0:
        raise RuntimeError(f"failed ({code}): {cmd}")
    return code, out


def pack_buf() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for d in INCLUDE_DIRS:
            tar.add(ROOT / d, arcname=d)
        for f in INCLUDE_FILES:
            p = ROOT / f
            if p.exists():
                tar.add(p, arcname=f)
    return buf.getvalue()


def ensure_dr_env(c) -> None:
    """Append DR_* keys only if missing; do not overwrite existing Influx/Grafana config."""
    run(
        c,
        "test -f ~/kpp/.env || touch ~/kpp/.env; "
        "grep -q '^DR_POLL_INTERVAL_SEC=' ~/kpp/.env || "
        "printf '%s\\n' 'DR_POLL_INTERVAL_SEC=0.05' >> ~/kpp/.env; "
        "grep -q '^DR_GPS_COURSE_MIN_SPEED_MPS=' ~/kpp/.env || "
        "printf '%s\\n' 'DR_GPS_COURSE_MIN_SPEED_MPS=1.0' >> ~/kpp/.env; "
        "grep -E '^DR_' ~/kpp/.env",
    )


def main() -> None:
    c = connect()
    run(c, "mkdir -p ~/kpp")
    data = pack_buf()
    sftp = c.open_sftp()
    with sftp.file("/tmp/kpp-gps-imu.tar.gz", "wb") as f:
        f.write(data)
    sftp.close()
    print(f"uploaded {len(data)} bytes")

    run(c, "tar -xzf /tmp/kpp-gps-imu.tar.gz -C ~/kpp")
    run(c, "cd ~/kpp && .venv/bin/pip install -q -r requirements.txt", check=False)
    # webapp models 可能超前於 Pi 上的 SQLite；漏跑會讓 Google callback upsert 炸掉
    run(c, "cd ~/kpp && .venv/bin/alembic upgrade head")

    ensure_dr_env(c)

    # Restart Grafana so provisioned dashboard JSON reloads (geomap / position_est)
    run(
        c,
        "cd ~/kpp && docker compose restart grafana && sleep 4 && "
        "docker ps --format 'table {{.Names}}\\t{{.Status}}' | grep -E 'NAME|kpp-|grafana'",
        check=False,
    )

    # Restart dashboard so telemetry.html + telemetry.py GPS fields load
    run(c, "systemctl --user restart kpp-dashboard.service", check=False)
    time.sleep(3)
    run(c, "systemctl --user is-active kpp-dashboard.service", check=False)

    # Start / restart dead reckoning (parallel to attitude_ekf)
    run(c, "pkill -f 'services.dead_reckoning.main' || true", check=False)
    time.sleep(1)
    run(
        c,
        "cd ~/kpp && nohup .venv/bin/python -m services.dead_reckoning.main "
        "> /tmp/kpp-dead-reckoning.log 2>&1 & echo dr_pid:$!; sleep 2; "
        "pgrep -af dead_reckoning || echo NO_DR; "
        "tail -40 /tmp/kpp-dead-reckoning.log",
        check=False,
    )

    # Smoke checks
    run(
        c,
        "python3 -c \""
        "import json; "
        "p=json.load(open('/home/evan/kpp/infra/grafana/dashboards/kart-telemetry.json')); "
        "panel=next(x for x in p['panels'] if x['id']==12); "
        "print('panel12', panel.get('title','')); "
        "print('layers', len(panel.get('options',{}).get('layers',[]))); "
        "print('targets', len(panel.get('targets',[])))"
        "\"",
    )
    run(
        c,
        "grep -n 'hasGpsFix\\|GPS 尚未定位\\|gps_lat' "
        "~/kpp/services/webapp/templates/telemetry.html | head -20",
    )
    run(
        c,
        "grep -n 'gps_fix\\|gps_lat' ~/kpp/services/webapp/telemetry.py | head -20",
    )
    run(
        c,
        "curl -s -o /dev/null -w 'telemetry:%{http_code}\\n' http://127.0.0.1:5000/telemetry; "
        "curl -s -o /dev/null -w 'grafana:%{http_code}\\n' "
        "'http://127.0.0.1:3000/grafana/api/health'; "
        "curl -s -o /dev/null -w 'dash_grafana:%{http_code}\\n' "
        "'http://127.0.0.1:5000/grafana/d/kart-telemetry/karting?orgId=1&kiosk'; "
        "systemctl --user is-active kpp-dashboard.service; "
        "pgrep -af dead_reckoning || echo NO_DR",
        check=False,
    )

    c.close()
    print("DONE")


if __name__ == "__main__":
    main()
