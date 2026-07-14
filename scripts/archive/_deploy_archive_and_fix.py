"""Archive today's heat + deploy leaderboard/car-map fixes to chuck."""
from __future__ import annotations

import io
import os
import tarfile
import time
from pathlib import Path

import paramiko

HOST = os.environ.get("KPP_PI_HOST", "100.102.122.104")
USER = os.environ.get("KPP_PI_USER", "evan")
PASSWORD = os.environ.get("KPP_PI_PASS", "")
ROOT = Path(__file__).resolve().parents[1]

FILES = [
    "services/decoder_ingest/lap_tracker.py",
    "services/decoder_ingest/config.py",
    "services/decoder_ingest/session_manager.py",
    "services/decoder_ingest/dashboard.py",
    "services/webapp/history.py",
    ".env.example",
    "scripts/_remote_archive_session.py",
]


def main() -> None:
    if not PASSWORD:
        raise SystemExit("set KPP_PI_PASS")

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

    def run(cmd: str, check: bool = True) -> str:
        print(f"$ {cmd[:200]}")
        _, stdout, stderr = c.exec_command(cmd, get_pty=True)
        out = (stdout.read() + stderr.read()).decode(errors="replace")
        code = stdout.channel.recv_exit_status()
        safe = out.encode("ascii", "replace").decode("ascii")
        if safe.strip():
            print(safe.rstrip()[-10000:])
        if check and code != 0:
            raise RuntimeError(f"failed ({code}): {cmd}")
        return out

    # Upload archive script first and run while snapshot still has live data
    sftp = c.open_sftp()
    sftp.put(
        str(ROOT / "scripts" / "_remote_archive_session.py"),
        "/tmp/_remote_archive_session.py",
    )
    sftp.close()
    run("cd ~/kpp && .venv/bin/python /tmp/_remote_archive_session.py")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in FILES:
            tar.add(ROOT / rel, arcname=rel)
    data = buf.getvalue()
    sftp = c.open_sftp()
    with sftp.file("/tmp/kpp-leaderboard-fix.tar.gz", "wb") as f:
        f.write(data)
    sftp.close()
    print(f"uploaded {len(data)} bytes")

    run("tar -xzf /tmp/kpp-leaderboard-fix.tar.gz -C ~/kpp")
    run(
        "grep -q '^SESSION_RESET_TOKEN=' ~/kpp/.env "
        "|| echo 'SESSION_RESET_TOKEN=kpp-reset-local' >> ~/kpp/.env"
    )
    run("grep -E '^(CAR_NUMBER_MAP|SESSION_RESET_TOKEN)=' ~/kpp/.env")
    run("systemctl --user restart kpp-dashboard.service")
    time.sleep(5)
    run("systemctl --user is-active kpp-dashboard.service")
    run(
        "curl -s -o /dev/null -w 'login:%{http_code}\\n' http://127.0.0.1:5000/login"
    )

    verify = r"""
cd ~/kpp && .venv/bin/python - <<'PY'
from dotenv import load_dotenv
import os
from influxdb_client import InfluxDBClient
load_dotenv("/home/evan/kpp/.env")
c = InfluxDBClient(url=os.getenv("INFLUX_URL"), token=os.getenv("INFLUX_TOKEN"), org=os.getenv("INFLUX_ORG"))
q = c.query_api()
b = os.getenv("INFLUX_BUCKET", "decoder")
flux = f'''
from(bucket: "{b}")
  |> range(start: -1d)
  |> filter(fn: (r) => r._measurement == "session_archive" and r.session_id == "sess-20260712-040531")
  |> filter(fn: (r) => r._field == "best_lap_time")
'''
rows = []
for t in q.query(flux):
    for r in t.records:
        rows.append((r.values.get("car_number"), r.get_value(), r.values.get("transponder_id")))
rows.sort(key=lambda x: x[1] if x[1] else 999)
print("archived sess-20260712-040531:")
for i, (car, best, tid) in enumerate(rows, 1):
    print(f"  {i}. #{car} {best:.3f}s {tid}")
print("n", len(rows))
PY
"""
    run(verify)


if __name__ == "__main__":
    main()
