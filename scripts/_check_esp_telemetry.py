"""Check if ESP32 UDP telemetry is reaching chuck."""
import json
import os
import paramiko

host = os.environ.get("KPP_PI_HOST", "100.102.122.104")
user = os.environ.get("KPP_PI_USER", "evan")
password = os.environ.get("KPP_PI_PASS", "00000000")

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(host, username=user, password=password, allow_agent=False, look_for_keys=False)


def run(cmd: str) -> str:
    print(f"\n$ {cmd[:100]}")
    _, o, e = c.exec_command(cmd, get_pty=True)
    out = (o.read() + e.read()).decode("utf-8", "replace")
    print(out.rstrip()[-3500:])
    return out


run("grep TELEMETRY_UDP ~/kpp/.env 2>/dev/null || echo NO_UDP_ENV")
run("ss -ulnp 2>/dev/null | grep 9500 || echo NO_UDP_BIND_9500")
run("systemctl --user is-active kpp-dashboard.service")
run(
    "journalctl --user -u kpp-dashboard.service -n 80 --no-pager 2>/dev/null "
    "| grep -iE 'udp|telemetry|9500|ingest' | tail -15 || true"
)
# Try to read app state via python on pi if possible
run(
    "cd ~/kpp && .venv/bin/python -c \""
    "import urllib.request; "
    "r=urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3); "
    "print('health', r.status)\" 2>&1"
)
# Packet capture 3s on UDP 9500 if tcpdump available
run(
    "timeout 8 sudo tcpdump -ni any udp port 9500 -c 3 2>&1 | tail -10 || "
    "echo tcpdump_failed"
)
run("ip -4 -br addr show eth0 wlan0 2>/dev/null || ip -4 addr | grep inet")

c.close()
