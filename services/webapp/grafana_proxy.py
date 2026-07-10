"""把本機 Grafana（預設 :3000）反代到同站 /grafana/*，
讓 https://…/telemetry 的 iframe 可同網域嵌入。
"""

from __future__ import annotations

import logging
import os

import httpx
from fastapi import APIRouter, Request, Response

logger = logging.getLogger(__name__)

router = APIRouter()

_UPSTREAM = os.getenv("GRAFANA_UPSTREAM", "http://127.0.0.1:3000").rstrip("/")
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


async def _proxy_http(request: Request, path: str) -> Response:
    url = f"{_UPSTREAM}/grafana/{path}" if path else f"{_UPSTREAM}/grafana/"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }
    body = await request.body()

    async with httpx.AsyncClient(follow_redirects=False, timeout=60.0) as client:
        upstream = await client.request(
            request.method,
            url,
            headers=headers,
            content=body,
        )

    out_headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=out_headers,
        media_type=upstream.headers.get("content-type"),
    )


@router.api_route(
    "/grafana",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
@router.api_route(
    "/grafana/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def grafana_http_proxy(request: Request, path: str = "") -> Response:
    try:
        return await _proxy_http(request, path)
    except httpx.HTTPError as exc:
        logger.exception("grafana proxy failed")
        return Response(content=f"grafana upstream error: {exc}", status_code=502)
