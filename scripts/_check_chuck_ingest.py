"""Check chuck frame-ingest + recent logs."""
import os
import paramiko

host = os.environ.get("KPP_PI_HOST", "100.102.122.104")
user = os.environ.get("KPP_PI_USER", "evan")
password = os.environ.get("KPP_PI_PASS", "00000000").strip()

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(
    hostname=host,
    username=user,
    password=password,
    allow_agent=False,
    look_for_keys=False,
    timeout=20,
)

cmds = [
    "grep TELEMETRY ~/kpp/.env 2>/dev/null; grep INFLUX ~/kpp/.env 2>/dev/null | head -5",
    'curl -s -w "\\nHTTP:%{http_code}\\n" -X POST http://127.0.0.1:5000/api/telemetry/frame-ingest '
    '-H "Authorization: Bearer kpp-telemetry-ingest-token-change-me" '
    '-H "Content-Type: application/octet-stream" --data-binary ""',
    "ps aux | grep -E 'uvicorn|webapp|decoder' | grep -v grep",
    "tail -50 ~/kpp/dashboard.log 2>/dev/null || tail -50 /tmp/kpp-dashboard.log 2>/dev/null || echo NO_LOG",
]

for cmd in cmds:
    print("\n===", cmd[:100])
    _, o, e = c.exec_command(cmd)
    out = (o.read() + e.read()).decode("utf-8", "replace")
    print(out[-3000:])

c.close()
