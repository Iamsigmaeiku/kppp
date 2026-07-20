"""把本機程式碼同步到 Pi（chuck）並重啟服務。

取代原本 scripts/_deploy_*.py 系列的 11 支一次性腳本。

使用方式：
    python scripts/deploy_to_pi.py [--mode MODE] [--no-restart] [--check]

必填環境變數（從 .env 讀，或直接設在 shell）：
    KPP_PI_HOST   Pi 的 IP 或 tailscale 位址（例如 100.102.122.104）
    KPP_PI_USER   SSH 使用者（例如 evan）

選填環境變數：
    KPP_PI_PASS   SSH 密碼（若有設 SSH key 則不需要）

--mode 選項：
    full     （預設）同步整個 services/ + infra/ + requirements.txt + docker-compose.yml
    webapp   只同步 services/webapp/ 相關檔案（快速修 UI bug 用）
    dr       同步 services/dead_reckoning/（dead reckoning 服務更新用）

部署流程（full 模式）：
    1. 打包指定路徑成 tar.gz
    2. SFTP 上傳到 Pi 的 /tmp/
    3. 解壓到 ~/kpp/
    4. 執行 pip install -r requirements.txt（--no-restart 時跳過）
    5. 執行 alembic upgrade head（webapp 有 migration 時自動升）
    6. 重啟 kpp-dashboard.service（--no-restart 時跳過）
    7. 執行 smoke check（curl 確認服務有回應）
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# SSH 連線（支援 SSH key 或密碼，不留可運作的預設密碼）
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(
            f"ERROR: 環境變數 {name} 未設定。\n"
            f"請在 .env 或 shell 設定後再執行。"
        )
    return value


def connect():
    try:
        import paramiko
    except ImportError:
        sys.exit("ERROR: 需要 paramiko。請執行：pip install paramiko")

    host = _require_env("KPP_PI_HOST")
    user = _require_env("KPP_PI_USER")
    password = os.environ.get("KPP_PI_PASS", "").strip()

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs: dict = dict(
        hostname=host,
        username=user,
        timeout=20,
    )
    if password:
        connect_kwargs["password"] = password
        connect_kwargs["allow_agent"] = False
        connect_kwargs["look_for_keys"] = False
    else:
        connect_kwargs["allow_agent"] = True
        connect_kwargs["look_for_keys"] = True

    try:
        c.connect(**connect_kwargs)
    except paramiko.AuthenticationException:
        if password:
            sys.exit(f"ERROR: SSH 密碼錯誤，連線 {user}@{host} 失敗。")
        sys.exit(
            f"ERROR: SSH key 驗證失敗，連線 {user}@{host} 失敗。\n"
            "請設定 KPP_PI_PASS 或確認 SSH key 已加入 Pi 的 authorized_keys。"
        )

    print(f"[deploy] 已連線到 {user}@{host}")
    return c


# ---------------------------------------------------------------------------
# 遠端指令執行
# ---------------------------------------------------------------------------

def run(c, cmd: str, *, check: bool = True) -> tuple[int, str]:
    print(f"$ {cmd[:200]}")
    _, stdout, stderr = c.exec_command(cmd, get_pty=True)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    code = stdout.channel.recv_exit_status()
    combined = (out + err).encode("ascii", "replace").decode("ascii")
    if combined.strip():
        print(combined.rstrip()[-6000:])
    if check and code != 0:
        raise RuntimeError(f"指令失敗（exit {code}）：{cmd[:200]}")
    return code, out


# ---------------------------------------------------------------------------
# 打包
# ---------------------------------------------------------------------------

FULL_DIRS = ["services", "infra"]
FULL_FILES = ["requirements.txt", "docker-compose.yml", "alembic.ini"]

WEBAPP_PATHS = [
    "services/webapp",
    "services/decoder_ingest",
    "requirements.txt",
    "alembic.ini",
]

DR_PATHS = [
    "services/dead_reckoning",
    "requirements.txt",
]


def pack_buf(paths: list[str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in paths:
            p = ROOT / rel
            if p.exists():
                tar.add(p, arcname=rel)
            else:
                print(f"[warn] 找不到路徑，略過：{rel}")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 部署流程
# ---------------------------------------------------------------------------

def deploy(c, *, mode: str, no_restart: bool, check_only: bool) -> None:
    run(c, "mkdir -p ~/kpp")

    if check_only:
        _smoke_check(c)
        return

    # 打包與上傳
    if mode == "full":
        paths = FULL_DIRS + FULL_FILES
        tmp_name = "kpp-full.tar.gz"
    elif mode == "webapp":
        paths = WEBAPP_PATHS
        tmp_name = "kpp-webapp.tar.gz"
    elif mode == "dr":
        paths = DR_PATHS
        tmp_name = "kpp-dr.tar.gz"
    else:
        sys.exit(f"ERROR: 未知的 mode={mode!r}，有效值：full, webapp, dr")

    data = pack_buf(paths)
    sftp = c.open_sftp()
    remote_tmp = f"/tmp/{tmp_name}"
    with sftp.file(remote_tmp, "wb") as f:
        f.write(data)
    sftp.close()
    print(f"[deploy] 上傳 {len(data):,} bytes → {remote_tmp}")

    run(c, f"tar -xzf {remote_tmp} -C ~/kpp")

    if not no_restart:
        run(c, "cd ~/kpp && .venv/bin/pip install -q -r requirements.txt", check=False)
        run(c, "cd ~/kpp && .venv/bin/alembic upgrade head")

        if mode in ("full", "webapp"):
            # 重啟 dashboard（decoder_ingest --with-dashboard）
            run(
                c,
                "systemctl --user restart kpp-dashboard.service",
                check=False,
            )
            time.sleep(4)
            run(c, "systemctl --user is-active kpp-dashboard.service", check=False)

        if mode in ("full", "dr"):
            # 重啟 dead reckoning
            run(c, "pkill -f 'services.dead_reckoning.main' || true", check=False)
            time.sleep(1)
            run(
                c,
                "cd ~/kpp && nohup .venv/bin/python -m services.dead_reckoning.main "
                "> /tmp/kpp-dead-reckoning.log 2>&1 & echo dr_pid:$!; sleep 2; "
                "pgrep -af dead_reckoning || echo NO_DR",
                check=False,
            )

        if mode == "full":
            # Grafana provisioning 重載
            run(
                c,
                "cd ~/kpp && docker compose restart grafana && sleep 4 && "
                "docker ps --format 'table {{.Names}}\\t{{.Status}}' | grep -E 'NAME|kpp-|grafana'",
                check=False,
            )

    _smoke_check(c)


def _smoke_check(c) -> None:
    print("\n[deploy] === Smoke check ===")
    run(
        c,
        "curl -s -o /dev/null -w 'health:%{http_code}\\n' http://127.0.0.1:8000/health; "
        "curl -s -o /dev/null -w 'dashboard_login:%{http_code}\\n' http://127.0.0.1:8000/login; "
        "curl -s -o /dev/null -w 'grafana_health:%{http_code}\\n' 'http://127.0.0.1:3000/grafana/api/health'; "
        "systemctl --user is-active kpp-dashboard.service || echo 'dashboard: NOT active'",
        check=False,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="把本機程式碼同步到 Pi（chuck）並重啟服務",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--mode",
        choices=["full", "webapp", "dr"],
        default="full",
        help="同步範圍（預設：full）",
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="只上傳檔案，不重啟服務（適合在比賽現場更新但不想中斷服務時使用）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="不上傳，只跑 smoke check 確認服務健康",
    )
    args = parser.parse_args()

    c = connect()
    try:
        deploy(c, mode=args.mode, no_restart=args.no_restart, check_only=args.check)
    finally:
        c.close()
    print("\n[deploy] DONE")


if __name__ == "__main__":
    main()
