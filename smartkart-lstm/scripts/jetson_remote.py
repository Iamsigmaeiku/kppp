#!/usr/bin/env python3
"""Probe / run commands on Jetson over Tailscale SSH."""
from __future__ import annotations

import sys
import time

import paramiko

HOST = "100.80.172.49"
USER = "evan"
PASS = "00000000"


def run(cmd: str, timeout: int = 600) -> int:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        HOST,
        username=USER,
        password=PASS,
        timeout=20,
        allow_agent=False,
        look_for_keys=False,
        banner_timeout=30,
    )
    try:
        transport = c.get_transport()
        assert transport is not None
        chan = transport.open_session()
        chan.get_pty()
        chan.exec_command(cmd)
        chan.settimeout(1.0)
        deadline = time.time() + timeout
        buf = b""
        while True:
            if chan.recv_ready():
                chunk = chan.recv(4096)
                if not chunk:
                    break
                sys.stdout.write(chunk.decode(errors="replace"))
                sys.stdout.flush()
                buf += chunk
            if chan.recv_stderr_ready():
                chunk = chan.recv_stderr(4096)
                sys.stderr.write(chunk.decode(errors="replace"))
                sys.stderr.flush()
            if chan.exit_status_ready():
                # drain
                while chan.recv_ready():
                    chunk = chan.recv(4096)
                    sys.stdout.write(chunk.decode(errors="replace"))
                while chan.recv_stderr_ready():
                    chunk = chan.recv_stderr(4096)
                    sys.stderr.write(chunk.decode(errors="replace"))
                return chan.recv_exit_status()
            if time.time() > deadline:
                chan.close()
                raise TimeoutError(f"command timed out after {timeout}s")
            time.sleep(0.2)
    finally:
        c.close()


def put(local: str, remote: str) -> None:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        HOST,
        username=USER,
        password=PASS,
        timeout=20,
        allow_agent=False,
        look_for_keys=False,
    )
    try:
        sftp = c.open_sftp()
        # mkdir -p
        parts = remote.strip("/").split("/")
        cur = ""
        for p in parts[:-1]:
            cur += "/" + p
            try:
                sftp.stat(cur)
            except OSError:
                sftp.mkdir(cur)
        sftp.put(local, remote)
        print(f"put {local} -> {remote}")
    finally:
        c.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--put":
        put(sys.argv[2], sys.argv[3])
        raise SystemExit(0)
    cmd = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else (
        "python3 -V; python3 -m pip -V; "
        "python3 -c 'import torch; print(torch.__version__, torch.cuda.is_available())' || true; "
        "cat /etc/nv_tegra_release"
    )
    raise SystemExit(run(cmd))
