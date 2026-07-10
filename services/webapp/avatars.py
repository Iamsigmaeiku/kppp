"""頭像：預設用 Google 登入帶回的大頭貼，允許上傳自訂圖片覆蓋。上傳的檔案
存在本機磁碟（AVATAR_UPLOAD_DIR），透過 StaticFiles 掛在 /uploads/avatars/
（見 app.py）。
"""

from __future__ import annotations

import io
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from .deps import get_db, require_user
from .models import User

router = APIRouter(prefix="/api/profile")

AVATAR_STATIC_PREFIX = "/uploads/avatars"
MAX_AVATAR_DIMENSION = 512
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}


def avatar_url_for(user: User) -> str | None:
    """本機上傳過的頭像優先；沒有的話 fallback 回 Google 登入帶回的大頭貼。"""
    if user.avatar_path:
        return f"{AVATAR_STATIC_PREFIX}/{Path(user.avatar_path).name}"
    return user.google_picture_url


@router.post("/avatar")
async def upload_avatar(
    request: Request,
    file: UploadFile,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="僅接受 JPEG/PNG/WebP 圖片")

    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="檔案過大（上限 5MB）")

    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="無法解析圖片檔案") from exc

    image = image.convert("RGB")
    image.thumbnail((MAX_AVATAR_DIMENSION, MAX_AVATAR_DIMENSION))

    upload_dir: Path = request.app.state.web_config.avatar_upload_dir
    upload_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{user.id}.jpg"
    image.save(upload_dir / filename, format="JPEG", quality=85)

    # 依真正登入的使用者 id 重新從 DB 撈一份可寫入的實例，避免用
    # get_current_user() 撈出的舊 session 物件跨 session 寫入。
    fresh_user = await db.get(User, user.id)
    if fresh_user is None:
        raise HTTPException(status_code=404, detail="user not found")
    fresh_user.avatar_path = str(Path("avatars") / filename)
    await db.commit()

    return {"avatar_url": avatar_url_for(fresh_user)}
