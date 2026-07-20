"""遙測頁面 / Grafana 反代白名單。"""

from __future__ import annotations

from .models import User

TELEMETRY_ALLOWED_EMAILS = frozenset(
    {
        "evandaihongyu@gmail.com",
        "c113154241@nkust.edu.tw",
    }
)


def can_view_telemetry(user: User | None) -> bool:
    if user is None or not user.email:
        return False
    return user.email.strip().lower() in TELEMETRY_ALLOWED_EMAILS
