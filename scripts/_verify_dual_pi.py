#!/usr/bin/env python3
"""Final dual-Pi checklist verification."""
from __future__ import annotations

import urllib.request

import paramiko

PASS = "00000000"


def ssh(host: str, user: str, cmd: str) -> str:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        host,
        username=user,
        password=PASS,
        timeout=15,
        allow_agent=False,
        look_for_keys=False,
    )
    try:
        _, o, _ = c.exec_command(cmd, get_pty=True)
        return o.read().decode(errors="replace")
    finally:
        c.close()


def http(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return f"{r.status} {r.read().decode()}"
    except Exception as ex:
        return f"FAIL {ex}"


print("=== kpp ===")
print(
    ssh(
        "kpp",
        "kpp",
        "timedatectl | grep synchronized; "
        "curl -sf http://127.0.0.1:8000/health; echo; "
        "curl -sf http://127.0.0.1:8000/version; echo; "
        "curl -sf http://192.168.88.100:8000/health; echo; "
        "curl -sf http://192.168.88.100:8000/version; echo; "
        "systemctl is-active keepalived; "
        "systemctl --user is-active kpp-dashboard.service; "
        'docker ps --format "{{.Names}} {{.Status}}"',
    )
)
print("=== chuck ===")
print(
    ssh(
        "chuck",
        "evan",
        "timedatectl | grep synchronized; "
        "curl -sf http://127.0.0.1:8000/health; echo; "
        "curl -sf http://127.0.0.1:8000/version; echo; "
        "systemctl --user is-active kpp-dashboard.service; "
        "systemctl is-active keepalived || echo keepalived:disabled; "
        'docker ps --format "{{.Names}} {{.Status}}"',
    )
)
print("=== tailscale from PC ===")
for url in (
    "http://kpp:8000/health",
    "http://kpp:8000/version",
    "http://chuck:8000/health",
    "http://chuck:8000/version",
):
    print(url, http(url))
