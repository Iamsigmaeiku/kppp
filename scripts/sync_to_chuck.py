"""把本機新碼同步到 chuck（樹莓派）並重啟 dashboard（:5000）。"""

from __future__ import annotations

import io
import os
import tarfile
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
    print(f"$ {cmd}")
    _, stdout, stderr = c.exec_command(cmd, get_pty=True)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    code = stdout.channel.recv_exit_status()
    safe = lambda s: s.encode("ascii", "replace").decode("ascii")
    if out.strip():
        print(safe(out.rstrip())[-4000:])
    if err.strip():
        print(safe(err.rstrip())[-1000:])
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


ENV_SNIPPET = """
# --- telemetry / grafana (managed) ---
TELEMETRY_INGEST_TOKEN=kpp-telemetry-ingest-token-change-me
GRAFANA_EMBED_URL=/grafana/d/kart-telemetry/kart-telemetry-f1?orgId=1&kiosk=tv&theme=dark&refresh=2s
GRAFANA_UPSTREAM=http://127.0.0.1:3000
INFLUX_URL=http://127.0.0.1:8086
INFLUX_TOKEN=kpp-dev-influx-token-change-me
INFLUX_ORG=kpp
INFLUX_BUCKET=decoder
DASHBOARD_PORT=5000
"""


def main():
    c = connect()
    run(c, "mkdir -p ~/kpp")
    data = pack_buf()
    sftp = c.open_sftp()
    with sftp.file("/tmp/kpp-sync.tar.gz", "wb") as f:
        f.write(data)
    sftp.close()
    print(f"uploaded {len(data)} bytes")

    run(c, "tar -xzf /tmp/kpp-sync.tar.gz -C ~/kpp")

    # merge env keys (idempotent-ish): append snippet if missing telemetry token
    run(
        c,
        "grep -q TELEMETRY_INGEST_TOKEN ~/kpp/.env 2>/dev/null || "
        "printf '%s\\n' '" + ENV_SNIPPET.replace("'", "'\\''") + "' >> ~/kpp/.env",
    )
    # force key lines we care about
    for key, val in [
        ("TELEMETRY_INGEST_TOKEN", "kpp-telemetry-ingest-token-change-me"),
        (
            "GRAFANA_EMBED_URL",
            # kiosk=tv: Grafana 11 TV mode — hide sidebar + top nav in iframe
            "/grafana/d/kart-telemetry/kart-telemetry-f1?orgId=1&kiosk=tv&theme=dark&refresh=2s",
        ),
        ("GRAFANA_UPSTREAM", "http://127.0.0.1:3000"),
        ("INFLUX_URL", "http://127.0.0.1:8086"),
        ("INFLUX_TOKEN", "kpp-dev-influx-token-change-me"),
        ("INFLUX_ORG", "kpp"),
        ("INFLUX_BUCKET", "decoder"),
        ("DASHBOARD_PORT", "5000"),
    ]:
        # Do NOT use sed with values containing '&' — sed treats & as matched text.
        run(c, f"grep -v '^{key}=' ~/kpp/.env > /tmp/kpp.env.fix && mv /tmp/kpp.env.fix ~/kpp/.env")
        run(c, f"printf '%s\\n' '{key}={val}' >> ~/kpp/.env")

    run(c, "cd ~/kpp && .venv/bin/pip install -q -r requirements.txt", check=False)

    # restart dashboard on :5000
    run(
        c,
        "pkill -f 'services.decoder_ingest.main --with-dashboard' || true; "
        "sleep 1; "
        "cd ~/kpp && nohup .venv/bin/python -m services.decoder_ingest.main --with-dashboard "
        "> /tmp/kpp-dashboard.log 2>&1 & echo started; sleep 3; "
        "curl -s -o /dev/null -w 'telemetry:%{http_code}\\n' http://127.0.0.1:5000/telemetry; "
        "curl -s -o /dev/null -w 'grafana:%{http_code}\\n' "
        "'http://127.0.0.1:5000/grafana/d/kart-telemetry/kart-telemetry-f1?orgId=1&kiosk&theme=dark'; "
        "curl -s -o /dev/null -w 'ingest_opt:%{http_code}\\n' -X OPTIONS http://127.0.0.1:5000/api/telemetry/ingest; "
        "tail -40 /tmp/kpp-dashboard.log",
        check=False,
    )
    c.close()
    print("DONE")


if __name__ == "__main__":
    main()
