"""Sync telemetry.html + kart-telemetry.json to chuck; restart dashboard + grafana."""
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

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(
            ROOT / "services" / "webapp" / "templates" / "telemetry.html",
            arcname="services/webapp/templates/telemetry.html",
        )
        tar.add(
            ROOT / "infra" / "grafana" / "dashboards" / "kart-telemetry.json",
            arcname="infra/grafana/dashboards/kart-telemetry.json",
        )
    data = buf.getvalue()
    sftp = c.open_sftp()
    with sftp.file("/tmp/kpp-telemetry-ui.tar.gz", "wb") as f:
        f.write(data)
    sftp.close()
    print(f"uploaded {len(data)} bytes")

    def run(cmd: str, check: bool = True) -> str:
        print(f"$ {cmd[:180]}")
        _, stdout, stderr = c.exec_command(cmd, get_pty=True)
        out = (stdout.read() + stderr.read()).decode(errors="replace")
        code = stdout.channel.recv_exit_status()
        safe = out.encode("ascii", "replace").decode("ascii")
        if safe.strip():
            print(safe.rstrip()[-4000:])
        if check and code != 0:
            raise RuntimeError(f"failed ({code}): {cmd}")
        return out

    run("tar -xzf /tmp/kpp-telemetry-ui.tar.gz -C ~/kpp")
    run("systemctl --user restart kpp-dashboard.service")
    run(
        "cd ~/kpp && docker compose restart grafana",
        check=False,
    )
    time.sleep(6)
    run("systemctl --user is-active kpp-dashboard.service")
    run(
        "python3 -c \""
        "import json; "
        "p=json.load(open('/home/evan/kpp/infra/grafana/dashboards/kart-telemetry.json')); "
        "ids=sorted(x['id'] for x in p['panels']); "
        "print('panels', len(p['panels']), 'ids', ids); "
        "print([(x['id'], x.get('title',''), x['gridPos']) for x in p['panels'] if x['id'] in (12,13,14)])"
        "\""
    )
    run(
        "grep -n '100vh - 220px\\|min-height: 640px\\|margin-bottom: 0.5rem' "
        "~/kpp/services/webapp/templates/telemetry.html"
    )
    run(
        "curl -s -o /dev/null -w 'telemetry:%{http_code}\\n' "
        "http://127.0.0.1:5000/telemetry; "
        "curl -s -o /dev/null -w 'login:%{http_code}\\n' "
        "http://127.0.0.1:5000/login; "
        "curl -s -o /dev/null -w 'grafana:%{http_code}\\n' "
        "'http://127.0.0.1:3000/grafana/api/health'",
        check=False,
    )
    c.close()
    print("DONE")


if __name__ == "__main__":
    main()
