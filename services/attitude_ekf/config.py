"""Influx / poll settings for Attitude EKF (matches project INFLUX_* env)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Repo root .env (services/attitude_ekf -> ../..)
_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")

INFLUX_URL = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "")
INFLUX_ORG = os.getenv("INFLUX_ORG", "kpp")
BUCKET = os.getenv("INFLUX_BUCKET", "decoder")
POLL_INTERVAL_SEC = float(os.getenv("EKF_POLL_INTERVAL_SEC", "0.001"))
MEASUREMENT_IMU = "kart_telemetry"
MEASUREMENT_ATTITUDE = "attitude"
