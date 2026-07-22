import paramiko
import time

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(
    "100.102.122.104",
    username="evan",
    password="00000000",
    allow_agent=False,
    look_for_keys=False,
)

_, o, _ = c.exec_command("grep -m1 '^Udp:' /proc/net/snmp")
before = o.read().decode().strip()
time.sleep(5)
_, o, _ = c.exec_command("grep -m1 '^Udp:' /proc/net/snmp")
after = o.read().decode().strip()
print("UDP snmp before:", before)
print("UDP snmp after: ", after)

flux = r'''
from(bucket: "decoder")
  |> range(start: -3m)
  |> filter(fn: (r) => r._measurement == "kart_telemetry" and r.device_id == "esp32-kart-01")
  |> count()
'''
cmd = (
    "cd ~/kpp && export $(grep -E '^INFLUX_' .env | xargs) && "
    'curl -s -XPOST "$INFLUX_URL/api/v2/query?org=$INFLUX_ORG" '
    '-H "Authorization: Token $INFLUX_TOKEN" -H "Accept: application/csv" '
    f"--data-binary {flux!r} | tail -8"
)
_, o, e = c.exec_command(cmd, get_pty=True)
print("influx query:\n", (o.read() + e.read()).decode()[-1200:])

c.close()
