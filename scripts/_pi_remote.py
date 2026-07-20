#!/usr/bin/env python3
"""One-shot remote helper for dual-Pi setup.

Usage:
  python scripts/_pi_remote.py <host> <cmd...>
  python scripts/_pi_remote.py --put <host> <local> <remote>
  python scripts/_pi_remote.py --get <host> <remote> <local>

Hosts:
  kpp   -> user kpp  (RPi5)
  chuck -> user evan (RPi4)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

PASS = os.environ.get("KPP_PI_PASS", "00000000")
USERS = {
    "kpp": "kpp",
    "chuck": "evan",
    "100.70.246.88": "kpp",
    "100.102.122.104": "evan",
}


def _user(host: str) -> str:
    return USERS.get(host, os.environ.get("KPP_PI_USER", "kpp"))


def connect(host: str) -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        host,
        username=_user(host),
        password=PASS,
        timeout=20,
        allow_agent=False,
        look_for_keys=False,
    )
    return c


def run(host: str, cmd: str, timeout: int = 300) -> int:
    c = connect(host)
    try:
        _, stdout, stderr = c.exec_command(cmd, timeout=timeout, get_pty=True)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        code = stdout.channel.recv_exit_status()
        encoding = sys.stdout.encoding or "utf-8"
        sys.stdout.write(out.encode(encoding, errors="replace").decode(encoding))
        if err:
            err_encoding = sys.stderr.encoding or "utf-8"
            sys.stderr.write(
                err.encode(err_encoding, errors="replace").decode(err_encoding)
            )
        return code
    finally:
        c.close()


def put(host: str, local: str, remote: str) -> int:
    c = connect(host)
    try:
        sftp = c.open_sftp()
        # ensure parent dir exists
        parent = str(Path(remote).as_posix().rsplit("/", 1)[0])
        try:
            sftp.stat(parent)
        except OSError:
            # mkdir -p via shell
            _, stdout, _ = c.exec_command(f"mkdir -p {parent}")
            stdout.channel.recv_exit_status()
        sftp.put(local, remote)
        sftp.close()
        print(f"put {local} -> {host}:{remote}")
        return 0
    finally:
        c.close()


def get(host: str, remote: str, local: str) -> int:
    c = connect(host)
    try:
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        sftp = c.open_sftp()
        sftp.get(remote, local)
        sftp.close()
        print(f"get {host}:{remote} -> {local}")
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    if sys.argv[1] == "--put":
        if len(sys.argv) != 5:
            print("usage: --put <host> <local> <remote>", file=sys.stderr)
            sys.exit(2)
        sys.exit(put(sys.argv[2], sys.argv[3], sys.argv[4]))
    if sys.argv[1] == "--get":
        if len(sys.argv) != 5:
            print("usage: --get <host> <remote> <local>", file=sys.stderr)
            sys.exit(2)
        sys.exit(get(sys.argv[2], sys.argv[3], sys.argv[4]))
    host = sys.argv[1]
    cmd = " ".join(sys.argv[2:])
    sys.exit(run(host, cmd))
