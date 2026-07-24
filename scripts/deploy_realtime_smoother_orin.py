"""把 realtime RTS smoother 同步到 Jetson Orin（Tailscale）並啟動 daemon。

HOST 預設 jetson-orin-super = 100.80.172.49（見 smartkart-lstm/scripts/jetson_remote.py）
Influx 指向 chuck：100.102.122.104:8086
"""

from __future__ import annotations

import io
import os
import sys
import tarfile
import time
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
HOST = os.environ.get("KPP_ORIN_HOST", "100.80.172.49")
USER = os.environ.get("KPP_ORIN_USER", "evan")
PASS = os.environ.get("KPP_ORIN_PASS", os.environ.get("KPP_PI_PASS", "00000000"))
CHUCK_INFLUX = os.environ.get("KPP_CHUCK_INFLUX", "http://100.102.122.104:8086")
CHUCK_DASH = os.environ.get("KPP_CHUCK_DASH", "http://100.102.122.104:8000")
REMOTE_ROOT = "/home/evan/kpp"

PACK = [
    "services/postprocess",
    "services/webapp/track_coords.py",
    "services/decoder_ingest/config.py",  # dotenv path helper not required but ok
]


def connect() -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        HOST,
        username=USER,
        password=PASS,
        timeout=25,
        allow_agent=False,
        look_for_keys=False,
        banner_timeout=30,
    )
    return c


def run(c: paramiko.SSHClient, cmd: str, check: bool = True) -> tuple[int, str]:
    print(f"$ {cmd[:220]}")
    _, stdout, stderr = c.exec_command(cmd, get_pty=True, timeout=300)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    code = stdout.channel.recv_exit_status()
    text = (out + err).encode("ascii", "replace").decode("ascii")
    if text.strip():
        print(text.rstrip()[-5000:])
    if check and code != 0:
        raise RuntimeError(f"exit {code}: {cmd[:200]}")
    return code, out


def pack() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in PACK:
            p = ROOT / rel
            if p.is_dir():
                tar.add(p, arcname=rel)
            elif p.is_file():
                tar.add(p, arcname=rel)
            else:
                print(f"[warn] missing {rel}")
        # empty package markers
        for init in [
            "services/__init__.py",
            "services/webapp/__init__.py",
            "services/decoder_ingest/__init__.py",
        ]:
            ip = ROOT / init
            if ip.exists():
                tar.add(ip, arcname=init)
    return buf.getvalue()


SERVICE = f"""[Unit]
Description=KPP realtime fixed-lag RTS smoother
After=network-online.target

[Service]
Type=simple
WorkingDirectory={REMOTE_ROOT}
Environment=PYTHONPATH={REMOTE_ROOT}
Environment=INFLUX_URL={CHUCK_INFLUX}
Environment=INFLUX_TOKEN=kpp-dev-influx-token-change-me
Environment=INFLUX_ORG=kpp
Environment=INFLUX_BUCKET=decoder
Environment=KPP_DASHBOARD_URL={CHUCK_DASH}
Environment=RTS_LAG_SEC=3
Environment=RTS_POLL_SEC=0.5
ExecStart={REMOTE_ROOT}/.venv/bin/python -m services.postprocess.realtime_smoother -v
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"""


def main() -> int:
    print(f"[orin] connect {USER}@{HOST} ...")
    try:
        c = connect()
    except Exception as exc:
        print(
            f"ERROR: 連不上 Orin ({HOST}): {exc}\n"
            "Tailscale 顯示 jetson-orin-super offline 時請先開機/上線再跑本腳本。",
            file=sys.stderr,
        )
        return 2

    try:
        run(c, f"mkdir -p {REMOTE_ROOT}/services/webapp {REMOTE_ROOT}/services/postprocess")
        data = pack()
        sftp = c.open_sftp()
        remote_tar = "/tmp/kpp-rts-orin.tgz"
        with sftp.file(remote_tar, "wb") as f:
            f.write(data)
        sftp.close()
        print(f"[orin] uploaded {len(data):,} bytes")
        run(c, f"tar -xzf {remote_tar} -C {REMOTE_ROOT}")

        # venv + deps
        run(
            c,
            f"test -d {REMOTE_ROOT}/.venv || python3 -m venv {REMOTE_ROOT}/.venv; "
            f"{REMOTE_ROOT}/.venv/bin/pip install -q 'numpy<2' influxdb-client python-dotenv",
            check=False,
        )

        # write unit via sftp（避免 shell quoting）
        unit_path = "/home/evan/.config/systemd/user/kpp-rts-smoother.service"
        run(c, "mkdir -p ~/.config/systemd/user")
        sftp = c.open_sftp()
        with sftp.file(unit_path, "w") as f:
            f.write(SERVICE)
        sftp.close()
        run(c, "systemctl --user daemon-reload", check=False)
        run(c, "systemctl --user enable --now kpp-rts-smoother.service", check=False)
        time.sleep(2)
        run(
            c,
            "systemctl --user is-active kpp-rts-smoother.service; "
            "journalctl --user -u kpp-rts-smoother.service -n 30 --no-pager",
            check=False,
        )
        print("[orin] DONE")
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    raise SystemExit(main())
