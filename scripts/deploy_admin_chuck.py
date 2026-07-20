#!/usr/bin/env python3
"""Sync webapp to chuck, migrate, set ADMIN_EMAILS, restart services."""
from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

import paramiko

HOST = "100.102.122.104"
USER = "evan"
PASSWORD = os.environ.get("KPP_PI_PASS", "00000000")
ROOT = Path(__file__).resolve().parents[1]

ADMIN_EMAILS = "hjz0312@gmail.com,wangyushun060@gmail.com"


def run(c: paramiko.SSHClient, cmd: str) -> str:
    _, o, e = c.exec_command(cmd, get_pty=True, timeout=180)
    out = o.read().decode(errors="replace") + e.read().decode(errors="replace")
    print(out)
    return out


def main() -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for d in ("services", "infra"):
            tar.add(ROOT / d, arcname=d)
        for f in ("requirements.txt", "alembic.ini"):
            p = ROOT / f
            if p.exists():
                tar.add(p, arcname=f)
    data = buf.getvalue()

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
    try:
        sftp = c.open_sftp()
        with sftp.file("/tmp/kpp-sync.tar.gz", "wb") as f:
            f.write(data)
        sftp.close()
        run(c, "mkdir -p ~/kpp && tar -xzf /tmp/kpp-sync.tar.gz -C ~/kpp")
        run(
            c,
            f"grep -v '^ADMIN_EMAILS=' ~/kpp/.env > /tmp/kpp.env && "
            f"printf 'ADMIN_EMAILS={ADMIN_EMAILS}\\n' >> /tmp/kpp.env && mv /tmp/kpp.env ~/kpp/.env",
        )
        run(c, "cd ~/kpp && .venv/bin/pip install -q -r requirements.txt")
        run(
            c,
            "cd ~/kpp && .venv/bin/alembic -c alembic.ini upgrade head",
        )
        run(c, "systemctl --user restart kpp-dashboard.service")
        run(c, "sudo systemctl restart cloudflared.service")
        run(
            c,
            "sleep 3; systemctl --user is-active kpp-dashboard.service; "
            "systemctl is-active cloudflared.service; "
            "curl -s -o /dev/null -w '5000_login:%{http_code}\\n' http://127.0.0.1:5000/login; "
            "python3 - <<'PY'\n"
            "import sqlite3\n"
            "c=sqlite3.connect('/home/evan/kpp/services/webapp/kpp.sqlite3')\n"
            "print('users', c.execute('select email,is_admin from users').fetchall())\n"
            "print('has_col', [x[1] for x in c.execute('pragma table_info(users)') if x[1]=='is_admin'])\n"
            "PY",
        )
    finally:
        c.close()


if __name__ == "__main__":
    main()
