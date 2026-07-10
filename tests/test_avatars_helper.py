"""avatar_url_for()：本機上傳優先，沒有時 fallback 回 Google 大頭貼。"""

from __future__ import annotations

from services.webapp.avatars import avatar_url_for
from services.webapp.models import User


def test_avatar_url_prefers_uploaded_avatar():
    user = User(
        id=1,
        google_sub="x",
        email="x@example.com",
        avatar_path="avatars/1.jpg",
        google_picture_url="https://example.com/pic.jpg",
    )
    assert avatar_url_for(user) == "/uploads/avatars/1.jpg"


def test_avatar_url_falls_back_to_google_picture():
    user = User(
        id=1,
        google_sub="x",
        email="x@example.com",
        avatar_path=None,
        google_picture_url="https://example.com/pic.jpg",
    )
    assert avatar_url_for(user) == "https://example.com/pic.jpg"


def test_avatar_url_none_when_neither_set():
    user = User(id=1, google_sub="x", email="x@example.com")
    assert avatar_url_for(user) is None
