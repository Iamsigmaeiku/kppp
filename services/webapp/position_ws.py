"""即時定位 WebSocket：/ws/positions。

ingest 寫入 telemetry 快取後呼叫 broadcast_position；前端 live-map 訂閱。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, Request, WebSocket
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from services.decoder_ingest.dashboard import get_session_manager

from .avatars import avatar_url_for
from .models import CarBinding, public_display_name

logger = logging.getLogger(__name__)

router = APIRouter()

_position_clients: set[WebSocket] = set()
_last_broadcast_mono: dict[str, float] = {}
_THROTTLE_SEC = 0.12  # ~8 Hz per device

_driver_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_DRIVER_CACHE_TTL = 20.0


def parse_device_car_map(raw: str) -> dict[str, str]:
    """Parse TELEMETRY_DEVICE_CAR_MAP=esp32-kart-01:7,esp32-kart-02:12."""
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        device_id, car_id = part.split(":", 1)
        device_id = device_id.strip()
        car_id = car_id.strip()
        if device_id and car_id:
            out[device_id] = car_id
    return out


def resolve_car_id(
    device_id: str,
    car_id: str | None,
    device_car_map: dict[str, str],
) -> str | None:
    if car_id and str(car_id).strip():
        return str(car_id).strip()
    mapped = device_car_map.get(device_id)
    return mapped.strip() if mapped else None


def extract_position(sample: dict[str, Any] | None) -> dict[str, Any] | None:
    """Prefer DR over raw GPS; heading / speed with sensible fallbacks."""
    if not isinstance(sample, dict):
        return None

    lat = sample.get("lat_dr")
    lon = sample.get("lon_dr")
    source = "dr"
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        lat = sample.get("gps_lat")
        lon = sample.get("gps_lon")
        source = "gps"
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None

    heading = sample.get("dr_heading_deg")
    if not isinstance(heading, (int, float)):
        heading = sample.get("gps_course_deg")

    speed = sample.get("dr_speed_mps")
    if not isinstance(speed, (int, float)):
        speed = sample.get("gps_speed_mps")

    return {
        "lat": float(lat),
        "lon": float(lon),
        "heading_deg": float(heading) if isinstance(heading, (int, float)) else None,
        "speed_mps": float(speed) if isinstance(speed, (int, float)) else None,
        "source": source,
    }


async def _lookup_driver(app: Any, car_id: str | None) -> dict[str, Any] | None:
    if not car_id:
        return None

    now = time.monotonic()
    cached = _driver_cache.get(car_id)
    if cached and (now - cached[0]) < _DRIVER_CACHE_TTL:
        return cached[1]

    sm = get_session_manager()
    session_id = sm.current_session_id if sm is not None else None
    if not session_id:
        _driver_cache[car_id] = (now, None)
        return None

    session_factory = getattr(app.state, "session_factory", None)
    if session_factory is None:
        _driver_cache[car_id] = (now, None)
        return None

    info: dict[str, Any] | None = None
    try:
        async with session_factory() as db:
            result = await db.execute(
                select(CarBinding)
                .where(
                    CarBinding.session_id == session_id,
                    CarBinding.car_number == car_id,
                )
                .options(selectinload(CarBinding.user))
                .limit(1)
            )
            binding = result.scalar_one_or_none()
            if binding is None:
                result2 = await db.execute(
                    select(CarBinding)
                    .where(CarBinding.session_id == session_id)
                    .options(selectinload(CarBinding.user))
                )
                want = car_id.lstrip("0") or car_id
                for b in result2.scalars().all():
                    cn = (b.car_number or "").strip()
                    if cn == car_id or (cn.lstrip("0") or cn) == want:
                        binding = b
                        break
            if binding is not None and binding.user is not None:
                user = binding.user
                info = {
                    "display_name": public_display_name(user),
                    "avatar_url": avatar_url_for(user),
                    "car_number": binding.car_number,
                    "transponder_id": binding.transponder_id,
                }
    except Exception:
        logger.exception("position driver lookup failed car_id=%s", car_id)
        info = None

    _driver_cache[car_id] = (now, info)
    return info


async def build_position_payload(
    app: Any,
    *,
    device_id: str,
    car_id: str | None,
    received_at: str,
    sample: dict[str, Any],
) -> dict[str, Any] | None:
    pos = extract_position(sample)
    if pos is None:
        return None

    web_config = app.state.web_config
    device_map = getattr(web_config, "telemetry_device_car_map", {}) or {}
    resolved_car = resolve_car_id(device_id, car_id, device_map)
    driver = await _lookup_driver(app, resolved_car)

    payload: dict[str, Any] = {
        "type": "position",
        "device_id": device_id,
        "car_id": resolved_car,
        "lat": pos["lat"],
        "lon": pos["lon"],
        "heading_deg": pos["heading_deg"],
        "speed_mps": pos["speed_mps"],
        "source": pos["source"],
        "received_at": received_at,
        "display_name": None,
        "avatar_url": None,
    }
    if driver:
        payload["display_name"] = driver.get("display_name")
        payload["avatar_url"] = driver.get("avatar_url")
        if not payload["car_id"] and driver.get("car_number"):
            payload["car_id"] = driver["car_number"]
    return payload


def should_throttle(device_id: str) -> bool:
    now = time.monotonic()
    last = _last_broadcast_mono.get(device_id, 0.0)
    if now - last < _THROTTLE_SEC:
        return True
    _last_broadcast_mono[device_id] = now
    return False


async def broadcast_position(payload: dict[str, Any]) -> None:
    if not _position_clients:
        return
    dead: list[WebSocket] = []
    for ws in list(_position_clients):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _position_clients.discard(ws)


async def notify_position_from_ingest(
    request: Request,
    *,
    device_id: str,
    car_id: str | None,
    received_at: str,
    sample: dict[str, Any],
) -> None:
    if should_throttle(device_id):
        return
    payload = await build_position_payload(
        request.app,
        device_id=device_id,
        car_id=car_id,
        received_at=received_at,
        sample=sample,
    )
    if payload is None:
        return
    await broadcast_position(payload)


async def snapshot_payloads(app: Any) -> list[dict[str, Any]]:
    by_device = getattr(app.state, "telemetry_by_device", None)
    if not isinstance(by_device, dict):
        return []
    out: list[dict[str, Any]] = []
    for device_id, entry in by_device.items():
        if not isinstance(entry, dict):
            continue
        payload = await build_position_payload(
            app,
            device_id=str(entry.get("device_id") or device_id),
            car_id=entry.get("car_id"),
            received_at=str(entry.get("received_at") or ""),
            sample=entry.get("sample") or {},
        )
        if payload is not None:
            out.append(payload)
    return out


@router.websocket("/ws/positions")
async def ws_positions(websocket: WebSocket) -> None:
    await websocket.accept()
    _position_clients.add(websocket)
    try:
        snap = await snapshot_payloads(websocket.app)
        await websocket.send_json({"type": "snapshot", "positions": snap})
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=90.0)
            except asyncio.TimeoutError:
                continue
    except Exception:
        pass
    finally:
        _position_clients.discard(websocket)
