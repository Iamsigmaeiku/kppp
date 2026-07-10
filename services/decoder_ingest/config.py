"""從環境變數載入 decoder / reconnect / InfluxDB 設定，不含業務邏輯。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# 專案根目錄的 .env（若存在）在 import 當下就載入，
# 已存在的環境變數優先，不會被 .env 覆蓋。
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)


class ConfigError(ValueError):
    """設定載入或驗證失敗。"""


@dataclass(frozen=True, slots=True)
class DecoderEndpoint:
    """單一 decoder TCP 連線設定。多台 decoder 時，每台各自對應一個
    DecoderEndpoint，各自有獨立的 PacketParser（TCP stream framing 狀態
    不可共用），但共用同一個 LapTracker/InfluxWriter。
    """

    host: str
    port: int
    decoder_id: str
    reconnect_initial_sec: float
    reconnect_max_sec: float


@dataclass(frozen=True, slots=True)
class InfluxConfig:
    url: str
    token: str
    org: str
    bucket: str
    decoder_id: str
    batch_size: int
    flush_interval_sec: float
    fallback_path: Path


@dataclass(frozen=True, slots=True)
class LapConfig:
    transponder_prefix_len: int
    noise_threshold_sec: float
    timer_timeout_sec: float
    max_lap_time_sec: float
    car_number_map: dict[str, str]
    decoder_tick_hz: float | None
    decoder_tick_byte_offset: int
    decoder_tick_byte_len: int


@dataclass(frozen=True, slots=True)
class DashboardConfig:
    host: str
    port: int


@dataclass(frozen=True, slots=True)
class AppConfig:
    decoders: list[DecoderEndpoint]
    influx: InfluxConfig
    lap: LapConfig
    dashboard: DashboardConfig
    raw_capture_path: Path
    snapshot_path: Path
    snapshot_interval_sec: float
    log_level: str
    auto_archive_idle_sec: float
    passing_calibration_path: Path | None


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"環境變數 {name} 為必填")
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"環境變數 {name} 必須為數字，收到: {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"環境變數 {name} 必須為整數，收到: {raw!r}") from exc


def _env_path(name: str, default: str) -> Path:
    raw = os.getenv(name, default).strip()
    return Path(raw)


def _env_optional_float(name: str) -> float | None:
    """未設定或空字串回傳 None（代表功能關閉），與 _env_float 的「有預設值」
    語意不同：這裡的 None 本身就是有意義的預設狀態，不是缺值錯誤。
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"環境變數 {name} 必須為數字，收到: {raw!r}") from exc


def _env_optional_path(name: str) -> Path | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    return Path(raw)


DEFAULT_CAR_NUMBER_MAP = (
    "140210B98368:13,140210E3C468:14,14021124C868:11,140201B81B68:15"
)


def _env_car_number_map(name: str = "CAR_NUMBER_MAP") -> dict[str, str]:
    """解析 `transponder:車號` 逗號分隔對照，例如 `14821144A15E:17,ABCD1234:02`。"""
    raw = os.getenv(name, "").strip() or DEFAULT_CAR_NUMBER_MAP

    mapping: dict[str, str] = {}
    for entry in raw.split(","):
        item = entry.strip()
        if not item:
            continue
        if ":" not in item:
            raise ConfigError(
                f"環境變數 {name} 格式錯誤，需為 transponder:車號，收到: {item!r}"
            )
        transponder_id, car_number = item.split(":", 1)
        transponder_id = transponder_id.strip().upper()
        car_number = car_number.strip()
        if not transponder_id or not car_number:
            raise ConfigError(
                f"環境變數 {name} 格式錯誤，transponder 與車號不可為空，收到: {item!r}"
            )
        mapping[transponder_id] = car_number
    return mapping


def _env_decoders(
    *,
    reconnect_initial_sec: float,
    reconnect_max_sec: float,
    name: str = "DECODERS",
) -> list[DecoderEndpoint] | None:
    """解析多 decoder 設定，格式 `host1:port1:id1,host2:port2:id2`，
    未設定時回傳 None（呼叫端 fallback 為單一 DECODER_HOST/PORT/ID 舊格式）。
    同一個 decoder_id 不可重複，否則每 decoder 連線狀態追蹤與 Influx tagging
    會互相覆蓋、失去意義。
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return None

    endpoints: list[DecoderEndpoint] = []
    seen_ids: set[str] = set()
    for entry in raw.split(","):
        item = entry.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) != 3:
            raise ConfigError(
                f"環境變數 {name} 格式錯誤，需為 host:port:decoder_id，收到: {item!r}"
            )
        host, port_str, decoder_id = (p.strip() for p in parts)
        if not host or not decoder_id:
            raise ConfigError(
                f"環境變數 {name} 格式錯誤，host 與 decoder_id 不可為空，收到: {item!r}"
            )
        try:
            port = int(port_str)
        except ValueError as exc:
            raise ConfigError(
                f"環境變數 {name} 的 port 必須為整數，收到: {item!r}"
            ) from exc
        if decoder_id in seen_ids:
            raise ConfigError(
                f"環境變數 {name} 的 decoder_id 重複: {decoder_id!r}"
            )
        seen_ids.add(decoder_id)
        endpoints.append(
            DecoderEndpoint(
                host=host,
                port=port,
                decoder_id=decoder_id,
                reconnect_initial_sec=reconnect_initial_sec,
                reconnect_max_sec=reconnect_max_sec,
            )
        )

    if not endpoints:
        raise ConfigError(f"環境變數 {name} 不可為空字串")
    return endpoints


def load_influx_config() -> InfluxConfig:
    """只讀 InfluxDB 相關設定，不需要 DECODER_HOST 等 decoder 設定——供
    services/webapp（influx_reader 的讀取路徑）取得連線資訊使用，不需要
    走完整的 load_config()（那是為 decoder ingest 服務設計的）。
    """
    return InfluxConfig(
        url=_require_env("INFLUX_URL"),
        token=_require_env("INFLUX_TOKEN"),
        org=_require_env("INFLUX_ORG"),
        bucket=_require_env("INFLUX_BUCKET"),
        decoder_id=os.getenv("DECODER_ID", "decoder-01").strip() or "decoder-01",
        batch_size=_env_int("BATCH_SIZE", 100),
        flush_interval_sec=_env_float("FLUSH_INTERVAL_SEC", 5.0),
        fallback_path=_env_path(
            "FALLBACK_PATH",
            "services/decoder_ingest/influx_fallback.ndjson",
        ),
    )


def load_config(*, dry_run: bool = False) -> AppConfig:
    """從環境變數組裝 AppConfig；非 dry-run 時 Influx 欄位必填。"""
    reconnect_initial_sec = _env_float("RECONNECT_INITIAL_SEC", 1.0)
    reconnect_max_sec = _env_float("RECONNECT_MAX_SEC", 30.0)
    if reconnect_initial_sec <= 0:
        raise ConfigError("RECONNECT_INITIAL_SEC 必須 > 0")
    if reconnect_max_sec < reconnect_initial_sec:
        raise ConfigError("RECONNECT_MAX_SEC 不可小於 RECONNECT_INITIAL_SEC")

    decoders = _env_decoders(
        reconnect_initial_sec=reconnect_initial_sec,
        reconnect_max_sec=reconnect_max_sec,
    )
    if decoders is None:
        # 向後相容：沒設 DECODERS 時沿用舊式單一 DECODER_HOST/PORT/ID，
        # 組成一個元素的 list，讓下游程式碼不用分支處理單/多台。
        decoders = [
            DecoderEndpoint(
                host=_require_env("DECODER_HOST"),
                port=_env_int("DECODER_PORT", 8899),
                decoder_id=os.getenv("DECODER_ID", "decoder-01").strip()
                or "decoder-01",
                reconnect_initial_sec=reconnect_initial_sec,
                reconnect_max_sec=reconnect_max_sec,
            )
        ]

    if dry_run:
        influx = InfluxConfig(
            url=os.getenv("INFLUX_URL", "").strip(),
            token=os.getenv("INFLUX_TOKEN", "").strip(),
            org=os.getenv("INFLUX_ORG", "").strip(),
            bucket=os.getenv("INFLUX_BUCKET", "").strip(),
            decoder_id=os.getenv("DECODER_ID", "decoder-01").strip() or "decoder-01",
            batch_size=_env_int("BATCH_SIZE", 100),
            flush_interval_sec=_env_float("FLUSH_INTERVAL_SEC", 5.0),
            fallback_path=_env_path(
                "FALLBACK_PATH",
                "services/decoder_ingest/influx_fallback.ndjson",
            ),
        )
    else:
        influx = load_influx_config()

    if influx.batch_size <= 0:
        raise ConfigError("BATCH_SIZE 必須 > 0")
    if influx.flush_interval_sec <= 0:
        raise ConfigError("FLUSH_INTERVAL_SEC 必須 > 0")

    lap = LapConfig(
        transponder_prefix_len=_env_int("TRANSPONDER_PREFIX_LEN", 12),
        noise_threshold_sec=_env_float("LAP_NOISE_THRESHOLD_SEC", 10.0),
        timer_timeout_sec=_env_float("LAP_TIMER_TIMEOUT_SEC", 120.0),
        max_lap_time_sec=_env_float("LAP_MAX_LAP_TIME_SEC", 600.0),
        car_number_map=_env_car_number_map(),
        decoder_tick_hz=_env_optional_float("DECODER_TICK_HZ"),
        decoder_tick_byte_offset=_env_int("DECODER_TICK_BYTE_OFFSET", 1),
        decoder_tick_byte_len=_env_int("DECODER_TICK_BYTE_LEN", 3),
    )
    if lap.transponder_prefix_len <= 0:
        raise ConfigError("TRANSPONDER_PREFIX_LEN 必須 > 0")
    if lap.noise_threshold_sec <= 0:
        raise ConfigError("LAP_NOISE_THRESHOLD_SEC 必須 > 0")
    if lap.timer_timeout_sec <= 0:
        raise ConfigError("LAP_TIMER_TIMEOUT_SEC 必須 > 0")
    if lap.max_lap_time_sec <= lap.noise_threshold_sec:
        raise ConfigError("LAP_MAX_LAP_TIME_SEC 必須大於 LAP_NOISE_THRESHOLD_SEC")
    if lap.decoder_tick_hz is not None and lap.decoder_tick_hz <= 0:
        raise ConfigError("DECODER_TICK_HZ 必須 > 0（或留空以停用）")
    if lap.decoder_tick_byte_offset < 0:
        raise ConfigError("DECODER_TICK_BYTE_OFFSET 必須 >= 0")
    if lap.decoder_tick_byte_len <= 0:
        raise ConfigError("DECODER_TICK_BYTE_LEN 必須 > 0")

    dashboard = DashboardConfig(
        host=os.getenv("DASHBOARD_HOST", "0.0.0.0").strip() or "0.0.0.0",
        port=_env_int("DASHBOARD_PORT", 8000),
    )
    if dashboard.port <= 0 or dashboard.port > 65535:
        raise ConfigError("DASHBOARD_PORT 必須在 1–65535")

    snapshot_interval_sec = _env_float("SNAPSHOT_INTERVAL_SEC", 10.0)
    if snapshot_interval_sec <= 0:
        raise ConfigError("SNAPSHOT_INTERVAL_SEC 必須 > 0")

    auto_archive_idle_sec = _env_float("AUTO_ARCHIVE_IDLE_SEC", 1800.0)
    if auto_archive_idle_sec <= 0:
        raise ConfigError("AUTO_ARCHIVE_IDLE_SEC 必須 > 0")

    return AppConfig(
        decoders=decoders,
        influx=influx,
        lap=lap,
        dashboard=dashboard,
        raw_capture_path=_env_path(
            "RAW_CAPTURE_PATH",
            "services/decoder_ingest/raw_capture.log",
        ),
        snapshot_path=_env_path(
            "SNAPSHOT_PATH",
            "services/decoder_ingest/session_snapshot.json",
        ),
        snapshot_interval_sec=snapshot_interval_sec,
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        auto_archive_idle_sec=auto_archive_idle_sec,
        passing_calibration_path=_env_optional_path("PASSING_CALIBRATION_PATH"),
    )
