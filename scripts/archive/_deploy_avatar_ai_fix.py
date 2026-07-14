"""Deploy avatar + AI coach fixes; optionally probe ExpTech."""
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
    "services/webapp/history.py",
    "services/webapp/ai_coach.py",
    "services/webapp/templates/profile.html",
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
        out = (stdout.read() + stderr.read()).decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        # Avoid Windows console encoding crash
        safe = out.encode("utf-8", "replace").decode("utf-8")
        if safe.strip():
            print(safe.rstrip()[-12000:])
        if check and code != 0:
            raise RuntimeError(f"failed ({code}): {cmd}")
        return out

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in FILES:
            tar.add(ROOT / rel, arcname=rel)
        tar.add(
            ROOT / "scripts" / "_remote_probe_ai.py",
            arcname="scripts/_remote_probe_ai.py",
        )
    data = buf.getvalue()
    sftp = c.open_sftp()
    with sftp.file("/tmp/kpp-avatar-ai.tar.gz", "wb") as f:
        f.write(data)
    sftp.close()
    print(f"uploaded {len(data)} bytes")

    run("tar -xzf /tmp/kpp-avatar-ai.tar.gz -C ~/kpp")
    # Prefer a working chat model if 35b returns empty
    run(
        "grep -q '^AI_AUTO_CHAT_MODEL=' ~/kpp/.env "
        "&& sed -i 's/^AI_AUTO_CHAT_MODEL=.*/AI_AUTO_CHAT_MODEL=ornith-1.0-9b/' ~/kpp/.env "
        "|| echo 'AI_AUTO_CHAT_MODEL=ornith-1.0-9b' >> ~/kpp/.env"
    )
    run("grep -E '^AI_' ~/kpp/.env | sed 's/KEY=.*/KEY=***/'")
    run("systemctl --user restart kpp-dashboard.service")
    time.sleep(5)
    run("systemctl --user is-active kpp-dashboard.service")

    # Quick offline unit: avatar lookup + parse helpers import
    run(
        "cd ~/kpp && .venv/bin/python - <<'PY'\n"
        "from services.webapp.ai_coach import _extract_message_content, _parse_report_json\n"
        "assert _extract_message_content({'choices':[{'message':{'content':'  {\"a\":1}  '}}]}).startswith('{')\n"
        "r=_parse_report_json('```json\\n{\"summary\":\"ok\",\"confidence_score\":70}\\n```')\n"
        "assert r.summary=='ok'\n"
        "print('helpers ok')\n"
        "PY"
    )

    # Probe models to file
    run(
        "cd ~/kpp && .venv/bin/python scripts/_remote_probe_ai.py "
        "> /tmp/ai_probe.out 2>&1; echo EXIT:$?; tail -c 4000 /tmp/ai_probe.out",
        check=False,
    )

    # End-to-end generate with fallback path
    run(
        "cd ~/kpp && .venv/bin/python - <<'PY'\n"
        "import asyncio, json, os\n"
        "from dotenv import load_dotenv\n"
        "load_dotenv('/home/evan/kpp/.env')\n"
        "from services.decoder_ingest.config import load_influx_config\n"
        "from services.decoder_ingest.influx_reader import InfluxReader\n"
        "from services.webapp.ai_coach import _build_user_prompt, _call_exptech, _parse_report_json\n"
        "from services.webapp.config import load_web_config\n"
        "cfg=load_web_config()\n"
        "async def main():\n"
        "  reader=InfluxReader(load_influx_config())\n"
        "  laps=await reader.get_lap_history('sess-20260712-040531','140201B81B77')\n"
        "  prompt=_build_user_prompt(car_number='15', driver_name='Iamsigma', best_lap_time=51.96, laps=laps[:8])\n"
        "  content=await _call_exptech(cfg.ai_coach, prompt)\n"
        "  print('content_len', len(content))\n"
        "  print('content_head', content[:300].replace('\\n',' '))\n"
        "  report=_parse_report_json(content)\n"
        "  print('SUMMARY', report.summary[:160])\n"
        "  print('strengths', report.strengths[:3])\n"
        "  await reader.close()\n"
        "asyncio.run(main())\n"
        "PY",
        check=False,
    )


if __name__ == "__main__":
    main()
