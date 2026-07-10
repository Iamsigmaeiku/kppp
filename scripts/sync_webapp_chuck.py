"""同步 web 層到 chuck 並重啟 kpp-dashboard.service。"""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

import paramiko

HOST = os.environ.get("KPP_PI_HOST", "100.102.122.104")
USER = os.environ.get("KPP_PI_USER", "evan")
PASSWORD = os.environ.get("KPP_PI_PASS", "")
ROOT = Path(__file__).resolve().parents[1]


def main():
    if not PASSWORD:
        raise SystemExit("set KPP_PI_PASS")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PASSWORD, timeout=20, allow_agent=False, look_for_keys=False)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(ROOT / "services" / "webapp", arcname="services/webapp")
    data = buf.getvalue()
    sftp = c.open_sftp()
    with sftp.file("/tmp/kpp-webapp.tar.gz", "wb") as f:
        f.write(data)
    sftp.close()

    cmds = [
        "tar -xzf /tmp/kpp-webapp.tar.gz -C ~/kpp",
        "systemctl --user restart kpp-dashboard.service",
        "sleep 3",
        "systemctl --user is-active kpp-dashboard.service",
        "curl -s -o /dev/null -w 'root:%{http_code} loc:%{redirect_url}\\n' http://127.0.0.1:5000/",
        "curl -s -o /dev/null -w 'login:%{http_code}\\n' http://127.0.0.1:5000/login",
        "curl -s http://127.0.0.1:5000/login | grep -E '請先登入|leaderboard|遙測' | head -10",
    ]
    for cmd in cmds:
        print("$", cmd[:100])
        _, stdout, stderr = c.exec_command(cmd)
        out = (stdout.read() + stderr.read()).decode(errors="replace")
        print(out.encode("ascii", "replace").decode()[-2000:])
    c.close()


if __name__ == "__main__":
    main()
