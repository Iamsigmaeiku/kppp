"""Fix corrupted GRAFANA_EMBED_URL on chuck (.env sed & bug) and restart dashboard."""
from __future__ import annotations

import os
import time

import paramiko

HOST = os.environ.get("KPP_PI_HOST", "100.102.122.104")
USER = os.environ.get("KPP_PI_USER", "evan")
PASSWORD = os.environ.get("KPP_PI_PASS", "")

# Grafana 11 TV kiosk: hide sidebar + top nav chrome in iframe embeds
EMBED = (
    "/grafana/d/kart-telemetry/karting"
    "?orgId=1&kiosk=tv&theme=dark&refresh=2s"
)


def main() -> None:
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

    def run(cmd: str) -> str:
        print(f"$ {cmd[:160]}")
        _, stdout, stderr = c.exec_command(cmd, get_pty=True)
        out = (stdout.read() + stderr.read()).decode(errors="replace")
        print(out.encode("ascii", "replace").decode()[-3000:].rstrip())
        return out

    run("grep -n GRAFANA_EMBED ~/kpp/.env || true")
    # Drop every GRAFANA_EMBED_URL line then append a clean one (avoid sed & expansion)
    run("grep -v '^GRAFANA_EMBED_URL=' ~/kpp/.env > /tmp/kpp.env.fixed")
    run("mv /tmp/kpp.env.fixed ~/kpp/.env")
    run(f"printf '%s\\n' 'GRAFANA_EMBED_URL={EMBED}' >> ~/kpp/.env")
    run("grep '^GRAFANA_EMBED_URL=' ~/kpp/.env")
    run("systemctl --user restart kpp-dashboard.service")
    time.sleep(4)
    run("systemctl --user is-active kpp-dashboard.service")
    run(
        "curl -s -o /dev/null -w 'login:%{http_code} telemetry:%{http_code}\\n' "
        "http://127.0.0.1:5000/login http://127.0.0.1:5000/telemetry"
    )
    c.close()
    print("DONE")
    print("embed ->", EMBED)


if __name__ == "__main__":
    main()
