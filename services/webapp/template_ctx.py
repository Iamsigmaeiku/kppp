"""Jinja / route helpers for telemetry nav visibility."""

from __future__ import annotations

from .models import User
from .telemetry_access import can_view_telemetry


def template_globals(user: User | None = None, **extra) -> dict:
    ctx = {"show_telemetry": can_view_telemetry(user), "user": user}
    ctx.update(extra)
    return ctx
