"""Sync Phase 2 to chuck: dashboard + attitude_ekf, restart Grafana, start EKF."""
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
    if not PASSWORD:
        raise SystemExit("set KPP_PI_PASS")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        HOST,
        username=USER,
        password=PASSWORD,
        timeout=20,
        allow_agent=False,
        look_for_keys=False,
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


def main() -> None:
    c = connect()
    run(c, "mkdir -p ~/kpp")
    data = pack_buf()
    sftp = c.open_sftp()
    with sftp.file("/tmp/kpp-sync.tar.gz", "wb") as f:
        f.write(data)
    sftp.close()
    print(f"uploaded {len(data)} bytes")

    run(c, "tar -xzf /tmp/kpp-sync.tar.gz -C ~/kpp")
    run(c, "cd ~/kpp && .venv/bin/pip install -q -r requirements.txt", check=False)

    # Restart Grafana so provisioned dashboard JSON reloads
    run(
        c,
        "cd ~/kpp && docker compose restart grafana && sleep 4 && "
        "docker ps --format 'table {{.Names}}\t{{.Status}}' | grep -E 'NAME|kpp-'",
        check=False,
    )

    # Confirm new panels present on disk
    run(
        c,
        "python3 -c \"import json; p=json.load(open('/home/evan/kpp/infra/grafana/dashboards/kart-telemetry.json')); "
        "print('panels', len(p['panels']), [x['type'] for x in p['panels'] if x['id']>=9])\"",
    )

    # Restart Attitude EKF
    run(c, "pkill -f 'services.attitude_ekf.main' || true", check=False)
    time.sleep(1)
    run(
        c,
        "cd ~/kpp && nohup .venv/bin/python -m services.attitude_ekf.main "
        "> /tmp/kpp-attitude-ekf.log 2>&1 & echo ekf_pid:$!; sleep 2; "
        "pgrep -af attitude_ekf || echo NO_EKF; "
        "tail -30 /tmp/kpp-attitude-ekf.log",
        check=False,
    )

    # Quick Influx check for attitude (may be empty if no IMU yet)
    run(
        c,
        "curl -s -o /dev/null -w 'grafana:%{http_code}\\n' "
        "'http://127.0.0.1:3000/grafana/d/kart-telemetry/karting?orgId=1'; "
        "curl -s -o /dev/null -w 'dash5000:%{http_code}\\n' "
        "'http://127.0.0.1:5000/grafana/d/kart-telemetry/karting?orgId=1&kiosk'",
        check=False,
    )

    c.close()
    print("DONE")


if __name__ == "__main__":
    main()
