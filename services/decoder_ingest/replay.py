"""重播 raw_capture.log：把歷史 hex 紀錄餵給 PacketParser 再走完整處理管線。

從 main.py 抽出，職責：
  - _parse_replay_line()：解析 `{iso} | {hex} | {ascii}` 格式的單行
  - replay_file()：逐行解析並呼叫 handle_feed_result
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from .dashboard import broadcast_decoder_status
from .ingest_loop import handle_feed_result
from .lap_tracker import LapTracker
from .packet_parser import PacketParser, UnknownPacket
from .session_manager import SessionManager

logger = logging.getLogger(__name__)

REPLAY_DECODER_ID = "replay"


def _parse_replay_line(line: str) -> tuple[datetime, bytes] | None:
    """解析 `{iso} | {hex} | {ascii}` 格式。"""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    parts = [part.strip() for part in stripped.split("|")]
    if len(parts) < 2:
        return None

    try:
        ts = datetime.fromisoformat(parts[0])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        raw = bytes.fromhex(parts[1])
    except ValueError:
        return None

    return ts, raw


async def replay_file(
    logfile: Path,
    parser: PacketParser,
    *,
    unknown_queue: "asyncio.Queue[UnknownPacket | None]",
    writer,  # InfluxWriter — avoid circular import
    dry_run: bool,
    lap_tracker: LapTracker | None = None,
    with_dashboard: bool = False,
    calibration_path: Path | None = None,
    session_manager: SessionManager | None = None,
) -> None:
    """解析 raw_capture.log 每行 hex，餵給 parser.feed_frame()。"""
    if not logfile.exists():
        raise FileNotFoundError(f"replay log not found: {logfile}")

    if lap_tracker is not None:
        # replay 模式沒有真正的 TCP decoder connect/disconnect 事件，
        # 但邏輯上這段期間就是「decoder 連線中」在餵資料，否則計時器會維持
        # 建構時的預設「未連線」狀態，導致 current_lap_elapsed 永遠回傳
        # None（本圈計時整場顯示「—」）。用一個固定的合成 decoder_id 代表
        # 這段 replay 期間的連線。
        lap_tracker.set_decoder_connected(REPLAY_DECODER_ID, True)
        if with_dashboard:
            await broadcast_decoder_status(lap_tracker.decoder_status_message())

    lines = await asyncio.to_thread(logfile.read_text, encoding="utf-8")
    for lineno, line in enumerate(lines.splitlines(), start=1):
        parsed = _parse_replay_line(line)
        if parsed is None:
            if line.strip():
                logger.warning("replay skip line %d: %r", lineno, line)
            continue

        received_at, frame = parsed
        result = parser.feed_frame(frame, received_at=received_at)
        await handle_feed_result(
            result,
            decoder_id=REPLAY_DECODER_ID,
            unknown_queue=unknown_queue,
            writer=writer,
            dry_run=dry_run,
            lap_tracker=lap_tracker,
            with_dashboard=with_dashboard,
            calibration_path=calibration_path,
            session_manager=session_manager,
        )

    logger.info("replay finished: %s", logfile)

    if lap_tracker is not None:
        # replay 結束後模擬斷線，讓計時器行為跟真實斷線一致（凍結在最後
        # elapsed 值），而不是悄悄維持「連線中」但再也不會有新資料。
        lap_tracker.set_decoder_connected(REPLAY_DECODER_ID, False)
        if with_dashboard:
            await broadcast_decoder_status(lap_tracker.decoder_status_message())
