import os
import tarfile
import tempfile
from pathlib import Path

import paramiko

HOST = os.environ.get("KPP_PI_HOST", "100.102.122.104")
USER = os.environ.get("KPP_PI_USER", "evan")
PASSWORD = os.environ.get("KPP_PI_PASS", "")
if not PASSWORD:
    raise SystemExit("set KPP_PI_PASS env var")
ROOT = Path(__file__).resolve().parents[1]

INCLUDE = [
    "docker-compose.yml",
    "infra",
]


def connect():
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
    return c


def run(c, cmd, check=True):
    print(f"$ {cmd}")
    _, stdout, stderr = c.exec_command(cmd, get_pty=True)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    code = stdout.channel.recv_exit_status()
    # Windows console (cp950) chokes on docker spinner braille
    safe = lambda s: s.encode("ascii", "replace").decode("ascii")
    if out.strip():
        print(safe(out.rstrip()))
    if err.strip():
        print(safe(err.rstrip()))
    if check and code != 0:
        raise RuntimeError(f"cmd failed ({code}): {cmd}")
    return code, out


def main():
    c = connect()
    run(c, "hostname; hostname -I; docker --version")
    run(c, "mkdir -p ~/kpp")

    # pack needed files
    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    tmp.close()
    tar_path = Path(tmp.name)
    with tarfile.open(tar_path, "w:gz") as tar:
        for rel in INCLUDE:
            p = ROOT / rel
            tar.add(p, arcname=rel)

    sftp = c.open_sftp()
    remote_tar = "/tmp/kpp-infra.tar.gz"
    print(f"upload {tar_path} -> {remote_tar}")
    sftp.put(str(tar_path), remote_tar)
    sftp.close()
    tar_path.unlink(missing_ok=True)

    run(c, "tar -xzf /tmp/kpp-infra.tar.gz -C ~/kpp")
    run(c, "cd ~/kpp && docker compose pull")
    run(c, "cd ~/kpp && docker compose up -d")
    run(c, "cd ~/kpp && docker compose ps")
    run(
        c,
        "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8086/health || true",
        check=False,
    )
    run(c, "ss -lnt | grep -E ':8086|:3000' || netstat -lnt | grep -E ':8086|:3000' || true", check=False)
    c.close()
    print("DONE")


if __name__ == "__main__":
    main()
