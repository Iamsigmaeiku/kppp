"""One-off: ensure TELEMETRY_UDP_* in Pi ~/kpp/.env and restart dashboard."""
import paramiko
import os
import sys

host = os.environ.get("KPP_PI_HOST", "100.102.122.104")
user = os.environ.get("KPP_PI_USER", "evan")
password = os.environ.get("KPP_PI_PASS", "00000000")

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(host, username=user, password=password, allow_agent=False, look_for_keys=False)


def run(cmd: str) -> None:
    print(f"$ {cmd[:120]}")
    _, o, e = c.exec_command(cmd)
    out = (o.read() + e.read()).decode("utf-8", "replace")
    if out.strip():
        print(out.rstrip()[-3000:])


patch = r"""
ENV=~/kpp/.env
touch "$ENV"
grep -q '^TELEMETRY_UDP_HOST=' "$ENV" || echo 'TELEMETRY_UDP_HOST=0.0.0.0' >> "$ENV"
grep -q '^TELEMETRY_UDP_PORT=' "$ENV" || echo 'TELEMETRY_UDP_PORT=9500' >> "$ENV"
grep -q '^TELEMETRY_UDP_DEVICE_ID=' "$ENV" || echo 'TELEMETRY_UDP_DEVICE_ID=esp32-kart-01' >> "$ENV"
"""
run("bash -lc " + repr(patch))
run("grep '^TELEMETRY_UDP' ~/kpp/.env || true")
run("sudo ufw allow 9500/udp 2>/dev/null || true")
run("systemctl --user restart kpp-dashboard.service")
import time

time.sleep(3)
run("systemctl --user is-active kpp-dashboard.service")
run("ss -ulnp 2>/dev/null | grep 9500 || netstat -ulnp 2>/dev/null | grep 9500 || echo 'check UDP bind in logs'")
c.close()
print("done")
