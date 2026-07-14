"""Deploy session-number / archive UI + AI live fallback to chuck."""
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

FILES = [
    "services/webapp/session_control.py",
    "services/webapp/session_numbering.py",
    "services/webapp/ai_coach.py",
    "services/webapp/pages.py",
    "services/webapp/history.py",
    "services/webapp/app.py",
    "services/webapp/templates/dashboard.html",
    "services/webapp/templates/profile.html",
    "services/webapp/static/theme.css",
    "services/decoder_ingest/dashboard.py",
    "services/decoder_ingest/main.py",
]


def connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = dict(
        hostname=HOST,
        username=USER,
        timeout=25,
        allow_agent=True,
        look_for_keys=True,
    )
    if PASSWORD:
        kwargs["password"] = PASSWORD
        kwargs["allow_agent"] = False
        kwargs["look_for_keys"] = False
    c.connect(**kwargs)
    return c


def run(c, cmd: str, check: bool = True) -> str:
    print(f"$ {cmd[:200]}")
    _, stdout, stderr = c.exec_command(cmd, get_pty=True)
    out = (stdout.read() + stderr.read()).decode(errors="replace")
    code = stdout.channel.recv_exit_status()
    text = out.encode("ascii", "replace").decode("ascii")
    if text.strip():
        print(text.rstrip()[-5000:])
    if check and code != 0:
        raise RuntimeError(f"failed ({code}): {cmd}")
    return out


def main() -> None:
    for rel in FILES:
        if not (ROOT / rel).exists():
            raise SystemExit(f"missing {rel}")

    c = connect()
    print("connected", HOST)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in FILES:
            tar.add(ROOT / rel, arcname=rel)
    data = buf.getvalue()

    sftp = c.open_sftp()
    with sftp.file("/tmp/kpp-session-archive-ui.tar.gz", "wb") as f:
        f.write(data)
    sftp.close()
    print(f"uploaded {len(data)} bytes")

    run(c, "tar -xzf /tmp/kpp-session-archive-ui.tar.gz -C ~/kpp")
    run(c, "systemctl --user restart kpp-dashboard.service")
    time.sleep(4)
    run(c, "systemctl --user is-active kpp-dashboard.service")
    run(
        c,
        "curl -s -o /dev/null -w 'login:%{http_code}\\n' http://127.0.0.1:5000/login",
    )
    run(
        c,
        "cd ~/kpp && .venv/bin/python -c "
        "\"from services.webapp import session_control; "
        "from services.decoder_ingest.dashboard import get_influx_writer; "
        "print('import-ok', session_control.router.prefix)\"",
    )
    run(
        c,
        "grep -n 'sessionBadge\\|archiveBtn\\|歸檔本節' "
        "~/kpp/services/webapp/templates/dashboard.html | head",
    )
    c.close()
    print("DONE")


if __name__ == "__main__":
    main()
