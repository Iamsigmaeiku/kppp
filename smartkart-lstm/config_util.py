"""Shared config / path helpers for smartkart-lstm."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import yaml

PKG_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PKG_ROOT.parent

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env", override=False)
except ImportError:
    pass  # infer-only hosts may skip python-dotenv



def load_config(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or (PKG_ROOT / "model" / "config.yaml")
    with cfg_path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def influx_settings(cfg: dict[str, Any] | None = None) -> dict[str, str]:
    cfg = cfg or load_config()
    url = os.getenv("INFLUX_URL", "").strip()
    token = os.getenv("INFLUX_TOKEN", "").strip()
    org = os.getenv("INFLUX_ORG", "kpp").strip()
    bucket = os.getenv("INFLUX_BUCKET", "decoder").strip()
    if not url or not token:
        raise SystemExit("INFLUX_URL / INFLUX_TOKEN required in env")
    influx_cfg = cfg.get("influx") or {}
    if influx_cfg.get("use_tailscale_rewrite", True) and "192.168." in url:
        tp = urlparse(influx_cfg.get("tailscale_url", "http://100.102.122.104:8086"))
        p = urlparse(url)
        url = urlunparse((p.scheme, f"{tp.hostname}:{p.port or 8086}", p.path, "", "", ""))
    return {"url": url, "token": token, "org": org, "bucket": bucket}


def ensure_dirs(cfg: dict[str, Any] | None = None) -> None:
    cfg = cfg or load_config()
    (PKG_ROOT / cfg["train"]["checkpoint_dir"]).mkdir(parents=True, exist_ok=True)
    (PKG_ROOT / cfg["train"]["outputs_dir"]).mkdir(parents=True, exist_ok=True)
