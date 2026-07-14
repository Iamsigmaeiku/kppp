"""Deploy day-rollover / session 1-10 compact / UID last-byte normalize to chuck."""
from __future__ import annotations

import io
import os
import re
import tarfile
from pathlib import Path

import paramiko

HOST = os.environ.get("KPP_PI_HOST", "100.102.122.104")
USER = os.environ.get("KPP_PI_USER", "evan")
PASSWORD = os.environ.get("KPP_PI_PASS", "")
ROOT = Path(__file__).resolve().parents[1]

FILES = [
    "services/webapp/session_control.py",
    "services/webapp/session_numbering.py",
    "services/webapp/history.py",
    "services/webapp/models.py",
    "services/decoder_ingest/main.py",
    "services/decoder_ingest/session_manager.py",
    "services/decoder_ingest/lap_tracker.py",
    "services/decoder_ingest/config.py",
]

NEW_CAR_MAP = (
    "14021124C877:11,140215359577:12,140210B98377:13,"
    "140210E3C477:14,148210E3C477:14,140201B81B77:15,"
    "140210D7E877:16,140211084277:17,140211241C77:18,"
    "140215494F77:19,140210998E77:20"
)


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
    with sftp.file("/tmp/kpp-bugfix-day-uid.tar.gz", "wb") as f:
        f.write(data)
    sftp.close()
    print(f"uploaded {len(data)} bytes")

    run(c, "tar -xzf /tmp/kpp-bugfix-day-uid.tar.gz -C ~/kpp")

    # Patch remote CAR_NUMBER_MAP to canonical …77 form (+ car 12)
    raw = run(c, "cat ~/kpp/.env", check=False)
    if re.search(r"^CAR_NUMBER_MAP=.*$", raw, flags=re.M):
        run(
            c,
            "python3 - <<'PY'\n"
            "from pathlib import Path\n"
            "import re\n"
            "p = Path.home() / 'kpp' / '.env'\n"
            "raw = p.read_text(encoding='utf-8')\n"
            f"new_map = {NEW_CAR_MAP!r}\n"
            "raw2 = re.sub(r'^CAR_NUMBER_MAP=.*$', f'CAR_NUMBER_MAP={new_map}', raw, flags=re.M)\n"
            "p.write_text(raw2, encoding='utf-8')\n"
            "print('updated CAR_NUMBER_MAP')\n"
            "PY"
        )
    else:
        run(
            c,
            f"echo 'CAR_NUMBER_MAP={NEW_CAR_MAP}' >> ~/kpp/.env && echo appended CAR_NUMBER_MAP",
        )

    # Restart ingest/web stack（跟既有 deploy 腳本同一支 user unit）
    run(c, "systemctl --user restart kpp-dashboard.service", check=False)
    run(c, "sleep 3; systemctl --user is-active kpp-dashboard.service", check=False)
    run(
        c,
        "cd ~/kpp && .venv/bin/python -c "
        "\"from services.webapp.session_numbering import compute_display_labels; "
        "from types import SimpleNamespace; "
        "from datetime import datetime, timezone; "
        "s=[SimpleNamespace(session_id='a', started_at=datetime(2026,7,13,8,tzinfo=timezone.utc))]; "
        "lab=compute_display_labels(s, tz_name='Asia/Taipei'); "
        "assert lab['a']['session_number']==1; print('import-ok')\"",
    )
    run(c, "grep -E '^(CAR_NUMBER_MAP)=' ~/kpp/.env | head -c 220; echo")
    print("done")
    c.close()


if __name__ == "__main__":
    main()
