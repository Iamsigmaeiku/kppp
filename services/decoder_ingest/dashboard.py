"""FastAPI WebSocket 面板：即時推播 lap 狀態。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, WebSocket

app = FastAPI(title="TKS Dashboard")
connected_clients: set[WebSocket] = set()

_lap_tracker = None
_on_reset = None
_session_manager = None
_influx_writer = None


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


@app.post("/api/session/reset")
async def reset_session() -> dict:
    if _lap_tracker is None:
        raise HTTPException(status_code=503, detail="lap tracker not initialized")

    new_session_id = None
    if _session_manager is not None and _influx_writer is not None:
        # 先把目前場次歸檔進 InfluxDB 再清空，任何一次手動 reset 都不會
        # 遺失資料（見 session_manager.py）。
        new_session_id = await _session_manager.archive_and_reset(
            _lap_tracker, _influx_writer, trigger="manual"
        )
    else:
        _lap_tracker.reset_session()

    if _on_reset is not None:
        _on_reset()
    reset_at = datetime.now(timezone.utc).isoformat()
    await broadcast_session_reset(reset_at=reset_at)
    return {"status": "ok", "reset_at": reset_at, "session_id": new_session_id}


@app.websocket("/ws/laps")
async def ws_laps(websocket: WebSocket) -> None:
    await websocket.accept()
    connected_clients.add(websocket)
    if _lap_tracker is not None:
        await websocket.send_json(_lap_tracker.decoder_status_message())
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
