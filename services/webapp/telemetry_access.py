"""遙測頁面 / Grafana 反代權限。"""

from __future__ import annotations

from .models import User


def can_view_telemetry(user: User | None) -> bool:
    if user is None:
        return False
    return bool(user.is_admin)
