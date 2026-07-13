"""Influx / poll settings for GPS-aided dead reckoning (matches project INFLUX_* env)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Repo root .env (services/dead_reckoning -> ../..)
_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")

INFLUX_URL = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "")
INFLUX_ORG = os.getenv("INFLUX_ORG", "kpp")
BUCKET = os.getenv("INFLUX_BUCKET", "decoder")
POLL_INTERVAL_SEC = float(os.getenv("DR_POLL_INTERVAL_SEC", "0.05"))
MEASUREMENT_IMU = "kart_telemetry"
MEASUREMENT_POSITION = "position_est"

# GPS course-over-ground 只有在車速夠快時才可信（低速時 NMEA course 雜訊很大）
GPS_COURSE_MIN_SPEED_MPS = float(os.getenv("DR_GPS_COURSE_MIN_SPEED_MPS", "1.0"))
