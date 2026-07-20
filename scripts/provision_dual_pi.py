#!/usr/bin/env python3
"""Provision kpp (RPi5) + install keepalived/NTP bits on both Pis.

Usage:
  python scripts/provision_dual_pi.py
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
PASS = os.environ.get("KPP_PI_PASS", "00000000")

HOSTS = {
    "kpp": {"user": "kpp", "role": "rpi5"},
    "chuck": {"user": "evan", "role": "rpi4"},
}


def connect(host: str) -> paramiko.SSHClient:
    info = HOSTS[host]
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        host,
        username=info["user"],
        password=PASS,
        timeout=20,
        allow_agent=False,
        look_for_keys=False,
    )
    print(f"[ok] connected {info['user']}@{host}")
    return c


def run(c: paramiko.SSHClient, cmd: str, *, check: bool = True, timeout: int = 600) -> str:
    print(f"$ {cmd[:180]}")
    _, stdout, stderr = c.exec_command(cmd, get_pty=True, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    code = stdout.channel.recv_exit_status()
    text = (out + err).strip()
    if text:
        # avoid mojibake spam
        printable = text.encode("ascii", "replace").decode("ascii")
        print(printable[-5000:])
    if check and code != 0:
        raise RuntimeError(f"exit {code}: {cmd[:180]}")
    return out


def sudo(c: paramiko.SSHClient, cmd: str, *, check: bool = True) -> str:
    # Avoid nested quote hell — write a temp script, then sudo bash it.
    remote = f"/tmp/kpp-sudo-{int(time.time() * 1000)}.sh"
    sftp = c.open_sftp()
    with sftp.file(remote, "w") as f:
        f.write("#!/bin/bash\nset -euo pipefail\n" + cmd + "\n")
    sftp.chmod(remote, 0o755)
    sftp.close()
    return run(c, f"echo {PASS} | sudo -S bash {remote}", check=check)


def put_bytes(c: paramiko.SSHClient, data: bytes, remote: str) -> None:
    sftp = c.open_sftp()
    parent = remote.rsplit("/", 1)[0]
    run(c, f"mkdir -p {parent}", check=False)
    with sftp.file(remote, "wb") as f:
        f.write(data)
    sftp.close()
    print(f"[put] {len(data):,} bytes -> {remote}")


def put_file(c: paramiko.SSHClient, local: Path, remote: str) -> None:
    put_bytes(c, local.read_bytes(), remote)


def pack_repo() -> bytes:
    paths = [
        "services",
        "infra",
        "requirements.txt",
        "docker-compose.yml",
        "alembic.ini",
        "deploy.sh",
    ]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in paths:
            p = ROOT / rel
            if p.exists():
                tar.add(p, arcname=rel)
    return buf.getvalue()


def local_commit() -> str:
    import subprocess

    try:
        return (
            subprocess.check_output(
                ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"]
            )
            .decode()
            .strip()
        )
    except Exception:
        return "unknown"


def provision_kpp(c: paramiko.SSHClient) -> None:
    commit = local_commit()
    print(f"=== provision kpp (commit={commit}) ===")

    # sync code
    data = pack_repo()
    put_bytes(c, data, "/tmp/kpp-full.tar.gz")
    run(c, "mkdir -p ~/kpp && tar -xzf /tmp/kpp-full.tar.gz -C ~/kpp")

    # make it a git checkout if possible (for /version + deploy.sh git pull)
    run(
        c,
        "cd ~/kpp && if [ ! -d .git ]; then "
        "git init -b main && "
        "git remote add origin https://github.com/Iamsigmaeiku/kppp.git 2>/dev/null || true; "
        "git add -A && git -c user.email=kpp@local -c user.name=kpp "
        f"commit -m 'sync {commit}' --allow-empty >/dev/null 2>&1 || true; "
        "fi",
        check=False,
    )

    # fix .env for local stack
    run(
        c,
        "cd ~/kpp && "
        "grep -q '^INFLUX_URL=' .env && sed -i 's|^INFLUX_URL=.*|INFLUX_URL=http://127.0.0.1:8086|' .env || "
        "echo 'INFLUX_URL=http://127.0.0.1:8086' >> .env; "
        "grep -q '^DASHBOARD_PORT=' .env && sed -i 's|^DASHBOARD_PORT=.*|DASHBOARD_PORT=8000|' .env || "
        "echo 'DASHBOARD_PORT=8000' >> .env; "
        f"grep -q '^KPP_COMMIT=' .env && sed -i 's|^KPP_COMMIT=.*|KPP_COMMIT={commit}|' .env || "
        f"echo 'KPP_COMMIT={commit}' >> .env; "
        "grep INFLUX_URL .env; grep DASHBOARD_PORT .env; grep KPP_COMMIT .env",
    )

    # venv
    run(
        c,
        "cd ~/kpp && "
        "if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi && "
        ".venv/bin/pip install -q -U pip && "
        ".venv/bin/pip install -q -r requirements.txt",
        timeout=900,
    )

    # docker stack
    run(c, "cd ~/kpp && docker compose pull && docker compose up -d", timeout=900)
    time.sleep(5)
    run(c, "docker ps --format 'table {{.Names}}\\t{{.Status}}'")

    # systemd user service
    put_file(
        c,
        ROOT / "infra/pi/kpp-dashboard.service",
        "/tmp/kpp-dashboard.service",
    )
    run(
        c,
        "mkdir -p ~/.config/systemd/user && "
        "cp /tmp/kpp-dashboard.service ~/.config/systemd/user/kpp-dashboard.service && "
        "systemctl --user daemon-reload && "
        "loginctl enable-linger $(whoami) 2>/dev/null || true && "
        "systemctl --user enable --now kpp-dashboard.service",
    )
    time.sleep(5)
    run(c, "systemctl --user is-active kpp-dashboard.service", check=False)
    run(
        c,
        "curl -sf -o /dev/null -w 'health:%{http_code}\\n' http://127.0.0.1:8000/health; "
        "curl -sf http://127.0.0.1:8000/version; echo; "
        "curl -sf -o /dev/null -w 'influx:%{http_code}\\n' http://127.0.0.1:8086/health",
        check=False,
    )


def install_keepalived(c: paramiko.SSHClient, role: str) -> None:
    print(f"=== install keepalived ({role}) ===")
    conf_name = "keepalived.rpi5.conf" if role == "rpi5" else "keepalived.rpi4.conf"

    put_file(c, ROOT / "infra/pi/health_check.sh", "/tmp/health_check.sh")
    put_file(
        c,
        ROOT / f"infra/pi/keepalived/{conf_name}",
        "/tmp/keepalived.conf",
    )
    # NTP gateway drop-in only on kpp (192.168.88.1). chuck already syncs via
    # debian pool and cannot reach 192.168.88.1 — installing it would break NTP.
    ntp_bits = ""
    if role == "rpi5":
        put_file(
            c,
            ROOT / "infra/pi/timesyncd-gateway.conf",
            "/tmp/timesyncd-gateway.conf",
        )
        ntp_bits = (
            "mkdir -p /etc/systemd/timesyncd.conf.d && "
            "cp /tmp/timesyncd-gateway.conf /etc/systemd/timesyncd.conf.d/10-gateway.conf && "
            "timedatectl set-ntp true && "
            "systemctl restart systemd-timesyncd && "
        )

    # Deploy user needs write on priority_override (no sudo during rolling deploy).
    deploy_user = HOSTS["kpp" if role == "rpi5" else "chuck"]["user"]
    sudo(
        c,
        "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq keepalived curl && "
        "mkdir -p /opt/smartkart /etc/keepalived && "
        "cp /tmp/health_check.sh /opt/smartkart/health_check.sh && "
        "chmod +x /opt/smartkart/health_check.sh && "
        "cp /tmp/keepalived.conf /etc/keepalived/keepalived.conf && "
        "echo 0 > /opt/smartkart/priority_override && "
        f"chown {deploy_user}:{deploy_user} /opt/smartkart/priority_override && "
        "chmod 664 /opt/smartkart/priority_override && "
        + ntp_bits
        + "systemctl enable keepalived && "
        "systemctl restart keepalived && "
        "systemctl is-active keepalived && "
        "ip addr show eth0 | grep -E 'inet |192.168' || true",
    )


def update_chuck_port(c: paramiko.SSHClient) -> None:
    print("=== unify chuck DASHBOARD_PORT=8000 + sync health endpoints ===")
    data = pack_repo()
    put_bytes(c, data, "/tmp/kpp-full.tar.gz")
    run(c, "tar -xzf /tmp/kpp-full.tar.gz -C ~/kpp")
    commit = local_commit()
    run(
        c,
        "cd ~/kpp && "
        "sed -i 's|^DASHBOARD_PORT=.*|DASHBOARD_PORT=8000|' .env && "
        f"grep -q '^KPP_COMMIT=' .env && sed -i 's|^KPP_COMMIT=.*|KPP_COMMIT={commit}|' .env || "
        f"echo 'KPP_COMMIT={commit}' >> .env && "
        ".venv/bin/pip install -q -r requirements.txt && "
        "systemctl --user daemon-reload && "
        "systemctl --user restart kpp-dashboard.service",
        timeout=300,
    )
    time.sleep(5)
    run(
        c,
        "systemctl --user is-active kpp-dashboard.service; "
        "ss -ltn | grep -E ':8000|:5000|:8086' || true; "
        "curl -sf http://127.0.0.1:8000/health; echo; "
        "curl -sf http://127.0.0.1:8000/version; echo",
        check=False,
    )


def main() -> None:
    # 1) kpp full provision
    ck = connect("kpp")
    try:
        provision_kpp(ck)
        install_keepalived(ck, "rpi5")
    finally:
        ck.close()

    # 2) chuck: sync code, port 8000, keepalived
    cc = connect("chuck")
    try:
        update_chuck_port(cc)
        install_keepalived(cc, "rpi4")
    finally:
        cc.close()

    print("\n=== DONE ===")
    print("NOTE: kpp is on 192.168.88.x, chuck is on 192.168.0.x —")
    print("VIP 192.168.88.100 only works once both share the same L2.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
