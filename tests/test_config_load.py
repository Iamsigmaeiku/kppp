"""Characterization tests for decoder_ingest/config.py.

These tests pin the *current* behavior of load_config() / load_influx_config()
against fixed inputs. If the output changes during refactoring, the tests will
fail and alert us to a behavior regression.

Design principle: monkeypatch os.environ, call the function, assert exact
field values — no mocking of internals, no knowledge of implementation details.
"""

from __future__ import annotations

import os

import pytest

from services.decoder_ingest.config import (
    AppConfig,
    ConfigError,
    DecoderEndpoint,
    InfluxConfig,
    LapConfig,
    load_config,
    load_influx_config,
    normalize_tid,
    _env_car_number_map,
    _env_decoders,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_ENV = {
    "DECODER_HOST": "192.168.1.10",
    "INFLUX_URL": "http://localhost:8086",
    "INFLUX_TOKEN": "test-token",
    "INFLUX_ORG": "kpp",
    "INFLUX_BUCKET": "decoder",
}


def clean_env(monkeypatch, extra: dict[str, str] | None = None) -> None:
    """Remove all config-related env vars then apply MINIMAL_ENV + extra."""
    keys_to_clear = [
        "DECODER_HOST", "DECODER_PORT", "DECODER_ID", "DECODERS",
        "INFLUX_URL", "INFLUX_TOKEN", "INFLUX_ORG", "INFLUX_BUCKET",
        "RECONNECT_INITIAL_SEC", "RECONNECT_MAX_SEC",
        "BATCH_SIZE", "FLUSH_INTERVAL_SEC", "FALLBACK_PATH",
        "TRANSPONDER_PREFIX_LEN", "LAP_NOISE_THRESHOLD_SEC",
        "LAP_TIMER_TIMEOUT_SEC", "LAP_MAX_LAP_TIME_SEC",
        "CAR_NUMBER_MAP", "DECODER_TICK_HZ",
        "DASHBOARD_HOST", "DASHBOARD_PORT",
        "SNAPSHOT_INTERVAL_SEC", "SNAPSHOT_PATH", "RAW_CAPTURE_PATH",
        "LOG_LEVEL", "AUTO_ARCHIVE_IDLE_SEC", "AUTO_ARCHIVE_ALL_FROZEN_SEC",
        "PASSING_CALIBRATION_PATH",
    ]
    for k in keys_to_clear:
        monkeypatch.delenv(k, raising=False)
    for k, v in MINIMAL_ENV.items():
        monkeypatch.setenv(k, v)
    for k, v in (extra or {}).items():
        monkeypatch.setenv(k, v)


# ---------------------------------------------------------------------------
# load_influx_config()
# ---------------------------------------------------------------------------

class TestLoadInfluxConfig:
    def test_happy_path(self, monkeypatch):
        clean_env(monkeypatch)
        cfg = load_influx_config()
        assert cfg.url == "http://localhost:8086"
        assert cfg.token == "test-token"
        assert cfg.org == "kpp"
        assert cfg.bucket == "decoder"
        assert cfg.decoder_id == "decoder-01"  # default
        assert cfg.batch_size == 100            # default
        assert cfg.flush_interval_sec == 5.0   # default

    def test_missing_influx_url_raises(self, monkeypatch):
        clean_env(monkeypatch)
        monkeypatch.delenv("INFLUX_URL")
        with pytest.raises(ConfigError, match="INFLUX_URL"):
            load_influx_config()

    def test_missing_influx_token_raises(self, monkeypatch):
        clean_env(monkeypatch)
        monkeypatch.delenv("INFLUX_TOKEN")
        with pytest.raises(ConfigError, match="INFLUX_TOKEN"):
            load_influx_config()

    def test_custom_batch_size(self, monkeypatch):
        clean_env(monkeypatch, {"BATCH_SIZE": "50"})
        cfg = load_influx_config()
        assert cfg.batch_size == 50

    def test_invalid_batch_size_raises(self, monkeypatch):
        clean_env(monkeypatch, {"BATCH_SIZE": "not-a-number"})
        with pytest.raises(ConfigError, match="BATCH_SIZE"):
            load_influx_config()


# ---------------------------------------------------------------------------
# load_config() — single decoder (legacy DECODER_HOST/PORT)
# ---------------------------------------------------------------------------

class TestLoadConfigSingleDecoder:
    def test_defaults(self, monkeypatch):
        clean_env(monkeypatch)
        cfg = load_config(dry_run=True)
        assert isinstance(cfg, AppConfig)
        assert len(cfg.decoders) == 1
        ep = cfg.decoders[0]
        assert ep.host == "192.168.1.10"
        assert ep.port == 8899              # default
        assert ep.decoder_id == "decoder-01"  # default
        assert ep.reconnect_initial_sec == 1.0
        assert ep.reconnect_max_sec == 30.0

    def test_lap_defaults(self, monkeypatch):
        clean_env(monkeypatch)
        cfg = load_config(dry_run=True)
        lap = cfg.lap
        assert lap.transponder_prefix_len == 12
        assert lap.noise_threshold_sec == 10.0
        assert lap.timer_timeout_sec == 120.0
        assert lap.max_lap_time_sec == 600.0
        # DECODER_TICK_HZ not set → default 256000
        assert lap.decoder_tick_hz == 256000.0

    def test_decoder_tick_hz_blank_disables(self, monkeypatch):
        clean_env(monkeypatch, {"DECODER_TICK_HZ": ""})
        cfg = load_config(dry_run=True)
        assert cfg.lap.decoder_tick_hz is None

    def test_decoder_tick_hz_explicit_value(self, monkeypatch):
        clean_env(monkeypatch, {"DECODER_TICK_HZ": "128000"})
        cfg = load_config(dry_run=True)
        assert cfg.lap.decoder_tick_hz == 128000.0

    def test_snapshot_defaults(self, monkeypatch):
        clean_env(monkeypatch)
        cfg = load_config(dry_run=True)
        assert cfg.snapshot_interval_sec == 10.0
        assert cfg.auto_archive_idle_sec == 1800.0
        assert cfg.auto_archive_all_frozen_sec == 300.0
        assert cfg.passing_calibration_path is None

    def test_reconnect_initial_must_be_positive(self, monkeypatch):
        clean_env(monkeypatch, {"RECONNECT_INITIAL_SEC": "0"})
        with pytest.raises(ConfigError, match="RECONNECT_INITIAL_SEC"):
            load_config(dry_run=True)

    def test_reconnect_max_less_than_initial_raises(self, monkeypatch):
        clean_env(monkeypatch, {"RECONNECT_INITIAL_SEC": "10", "RECONNECT_MAX_SEC": "5"})
        with pytest.raises(ConfigError, match="RECONNECT_MAX_SEC"):
            load_config(dry_run=True)

    def test_batch_size_zero_raises(self, monkeypatch):
        clean_env(monkeypatch, {"BATCH_SIZE": "0"})
        with pytest.raises(ConfigError, match="BATCH_SIZE"):
            load_config(dry_run=True)

    def test_max_lap_must_exceed_noise(self, monkeypatch):
        clean_env(monkeypatch, {
            "LAP_NOISE_THRESHOLD_SEC": "60",
            "LAP_MAX_LAP_TIME_SEC": "50",  # less than noise
        })
        with pytest.raises(ConfigError, match="LAP_MAX_LAP_TIME_SEC"):
            load_config(dry_run=True)

    def test_missing_decoder_host_raises(self, monkeypatch):
        clean_env(monkeypatch)
        monkeypatch.delenv("DECODER_HOST")
        with pytest.raises(ConfigError, match="DECODER_HOST"):
            load_config(dry_run=True)


# ---------------------------------------------------------------------------
# load_config() — multi-decoder via DECODERS env var
# ---------------------------------------------------------------------------

class TestLoadConfigMultiDecoder:
    def test_two_decoders(self, monkeypatch):
        clean_env(monkeypatch, {"DECODERS": "192.168.1.10:8899:d1,192.168.1.11:8899:d2"})
        monkeypatch.delenv("DECODER_HOST", raising=False)
        cfg = load_config(dry_run=True)
        assert len(cfg.decoders) == 2
        assert cfg.decoders[0].host == "192.168.1.10"
        assert cfg.decoders[0].decoder_id == "d1"
        assert cfg.decoders[1].host == "192.168.1.11"
        assert cfg.decoders[1].decoder_id == "d2"

    def test_duplicate_decoder_id_raises(self, monkeypatch):
        clean_env(monkeypatch, {"DECODERS": "10.0.0.1:8899:same,10.0.0.2:8899:same"})
        monkeypatch.delenv("DECODER_HOST", raising=False)
        with pytest.raises(ConfigError, match="decoder_id 重複"):
            load_config(dry_run=True)

    def test_malformed_decoders_raises(self, monkeypatch):
        clean_env(monkeypatch, {"DECODERS": "onlytwoparts:8899"})
        monkeypatch.delenv("DECODER_HOST", raising=False)
        with pytest.raises(ConfigError, match="host:port:decoder_id"):
            load_config(dry_run=True)


# ---------------------------------------------------------------------------
# _env_car_number_map()
# ---------------------------------------------------------------------------

class TestCarNumberMap:
    def test_default_map_has_known_entries(self, monkeypatch):
        monkeypatch.delenv("CAR_NUMBER_MAP", raising=False)
        mapping = _env_car_number_map()
        # normalize_tid maps ...77 suffix already
        assert "14021124C877" in mapping
        assert mapping["14021124C877"] == "11"

    def test_custom_map(self, monkeypatch):
        monkeypatch.setenv("CAR_NUMBER_MAP", "AABBCCDD1177:42,AABBCCDD2277:99")
        mapping = _env_car_number_map()
        assert mapping.get("AABBCCDD1177") == "42"
        assert mapping.get("AABBCCDD2277") == "99"

    def test_malformed_entry_raises(self, monkeypatch):
        monkeypatch.setenv("CAR_NUMBER_MAP", "NOCOLON")
        with pytest.raises(ConfigError, match="transponder:車號"):
            _env_car_number_map()


# ---------------------------------------------------------------------------
# normalize_tid()
# ---------------------------------------------------------------------------

class TestNormalizeTid:
    def test_canonical_77_suffix(self):
        assert normalize_tid("14021124C878") == "14021124C877"
        assert normalize_tid("14021124C868") == "14021124C877"
        assert normalize_tid("14021124C877") == "14021124C877"

    def test_short_id_unchanged(self):
        assert normalize_tid("SHORT") == "SHORT"
