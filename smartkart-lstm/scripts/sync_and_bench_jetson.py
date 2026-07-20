#!/usr/bin/env python3
"""Sync smartkart-lstm infer bundle to Jetson and run benchmark."""
from __future__ import annotations

import io
import tarfile
from pathlib import Path

import paramiko

HOST = "100.80.172.49"
USER = "evan"
PASS = "00000000"
PKG = Path(__file__).resolve().parents[1]


def connect() -> paramiko.SSHClient:
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
    return c


def run(c: paramiko.SSHClient, cmd: str, timeout: int = 300) -> int:
    print(f"$ {cmd}")
    _, stdout, stderr = c.exec_command(cmd, timeout=timeout, get_pty=True)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    print(out, end="")
    if err:
        print(err, end="")
    return stdout.channel.recv_exit_status()


def main() -> int:
    files = [
        "infer_jetson.py",
        "config_util.py",
        "model/__init__.py",
        "model/gru_laptime.py",
        "model/checkpoint_io.py",
        "model/config.yaml",
        "checkpoints/latest.pt",
        "requirements.txt",
        "README.md",
    ]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in files:
            path = PKG / rel
            if not path.exists():
                raise SystemExit(f"missing {path}")
            tar.add(path, arcname=f"smartkart-lstm/{rel}")
    payload = buf.getvalue()

    c = connect()
    try:
        run(c, "mkdir -p ~/smartkart-lstm/checkpoints ~/smartkart-lstm/outputs")
        sftp = c.open_sftp()
        remote = "/home/evan/smartkart-lstm/_bundle.tgz"
        with sftp.file(remote, "wb") as f:
            f.write(payload)
        sftp.close()
        print(f"uploaded bundle ({len(payload)} bytes)")
        run(c, f"tar -xzf {remote} -C /home/evan && rm -f {remote}")
        run(
            c,
            "bash -lc 'source ~/smartkart-lstm/.venv/bin/activate; "
            "pip install -q \"numpy<2\" PyYAML; "
            "export LD_LIBRARY_PATH=/usr/local/cuda-12.6/targets/aarch64-linux/lib:/usr/local/cuda/lib64:$LD_LIBRARY_PATH; "
            "cd ~/smartkart-lstm; python infer_jetson.py --device cuda --n 200 --warmup 20'",
            timeout=300,
        )
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    raise SystemExit(main())
