"""FastAPI WebSocket 面板：即時推播 lap 狀態。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request, WebSocket

app = FastAPI(title="TKS Dashboard")
connected_clients: set[WebSocket] = set()

_lap_tracker = None
_on_reset = None
_session_manager = None
_influx_writer = None
_on_session_started = None


def set_lap_tracker(tracker) -> None:
    global _lap_tracker
    _lap_tracker = tracker


def get_lap_tracker():
    """供 services/webapp（car_bindings 等）讀取目前的 LapTracker，例如
    查詢某個 transponder 目前對應的車號。回傳 None 代表 dashboard 尚未
    啟動（--with-dashboard 未開）。
    """
    return _lap_tracker


def set_session_manager(session_manager, influx_writer) -> None:
    global _session_manager, _influx_writer
    _session_manager = session_manager
    _influx_writer = influx_writer


def get_session_manager():
    """供 services/webapp（car_bindings 等）讀取目前場次的權威 session_id
    來源。回傳 None 代表 dashboard 尚未啟動。
    """
    return _session_manager


def set_reset_hook(callback) -> None:
    """註冊一個 reset 後要立即執行的同步 callback（例如立刻寫一次
    session snapshot），避免 reset 後、下次週期性 snapshot 寫入前的空窗期
    崩潰導致復原到 reset 前的舊資料。callback 不接受參數、不回傳值。
    """
    global _on_reset
    _on_reset = callback


def get_reset_hook():
    return _on_reset


def set_session_started_hook(callback) -> None:
    """註冊一個「新場次開始」的 async callback（簽名
    `callback(session_id: str, started_at: datetime) -> Awaitable[None]`），
    在服務啟動的第一個場次、以及之後每次 archive_and_reset() 換發新
    session_id 時都會被呼叫一次。供 services/webapp 掛上場次每日編號邏輯
    （見 session_numbering.py）——decoder_ingest 自己不需要知道編號怎麼算，
    只負責在「新場次真的開始了」的當下通知一聲。
    """
    global _on_session_started
    _on_session_started = callback


def get_session_started_hook():
    return _on_session_started


@app.post("/api/session/reset")
async def reset_session(request: Request) -> dict:
    """場次結束：歸檔後清空。需 Header `X-Session-Reset-Token` 對上
    環境變數 `SESSION_RESET_TOKEN`（未設定 token 則維持禁用）。
    """
    import os

    expected = (os.getenv("SESSION_RESET_TOKEN") or "").strip()
    provided = (request.headers.get("X-Session-Reset-Token") or "").strip()
    if not expected or provided != expected:
        raise HTTPException(
            status_code=403, detail="session reset disabled for public users"
        )
    if _session_manager is None or _influx_writer is None or _lap_tracker is None:
        raise HTTPException(status_code=503, detail="session manager not ready")

    archived = _session_manager.current_session_id
    archived_started = _session_manager.session_started_at
    new_session_id = await _session_manager.archive_and_reset(
        _lap_tracker, _influx_writer, trigger="manual"
    )
    # 手動收場同樣補編號，避免列表出現沒有「第 N 節」的裸 session_id
    if _on_session_started is not None:
        try:
            from services.webapp import session_numbering

            await session_numbering.ensure_session_numbered(
                archived, archived_started
            )
        except Exception:
            pass
    if _on_reset is not None:
        _on_reset()
    reset_at = datetime.now(timezone.utc).isoformat()
    await broadcast_session_reset(reset_at=reset_at)
    await broadcast_session_info(session_id=new_session_id)
    if _on_session_started is not None:
        await _on_session_started(
            new_session_id, _session_manager.session_started_at
        )
    return {
        "ok": True,
        "archived_session_id": archived,
        "new_session_id": new_session_id,
        "reset_at": reset_at,
    }


@app.websocket("/ws/laps")
async def ws_laps(websocket: WebSocket) -> None:
    await websocket.accept()
    connected_clients.add(websocket)
    if _lap_tracker is not None:
        await websocket.send_json(_lap_tracker.decoder_status_message())
        if _session_manager is not None:
            # 讓前端知道目前是哪個 session_id，才能查詢「每一圈的圈時」
            # 展開明細（見 GET /api/sessions/{session_id}/laps/{transponder_id}）。
            await websocket.send_json(
                {"type": "session_info", "session_id": _session_manager.current_session_id}
            )
        for state in _lap_tracker.all_states():
            try:
                await websocket.send_json(state)
            except Exception:
                break
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        pass
    finally:
        connected_clients.discard(websocket)


async def broadcast_message(data: dict) -> None:
    dead: list[WebSocket] = []
    for ws in connected_clients:
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        connected_clients.discard(ws)


async def broadcast_lap_update(data: dict) -> None:
    await broadcast_message(data)


async def broadcast_capture(
    *,
    timestamp: str,
    hex_data: str,
    ascii_data: str,
) -> None:
    await broadcast_message(
        {
            "type": "capture",
            "timestamp": timestamp,
            "hex": hex_data,
            "ascii": ascii_data,
        }
    )


async def broadcast_decoder_status(status: dict) -> None:
    """轉發 LapTracker.decoder_status_message() 產生的完整狀態物件
    （含 connected/connected_count/total_count/decoders 明細）。
    """
    await broadcast_message(status)


async def broadcast_session_reset(*, reset_at: str) -> None:
    await broadcast_message(
        {
            "type": "session_reset",
            "reset_at": reset_at,
        }
    )


async def broadcast_session_info(session_id: str) -> None:
    """通知所有已連線客戶端目前的 session_id，讓前端能查詢
    GET /api/sessions/{session_id}/laps/{transponder_id}（每一圈的圈時
    展開明細）。在新場次開始時（服務啟動、或 archive_and_reset 換發新
    session_id 後）廣播一次。
    """
    await broadcast_message({"type": "session_info", "session_id": session_id})
