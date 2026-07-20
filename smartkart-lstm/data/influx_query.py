"""InfluxDB query helpers for SmartKart GRU pipeline.

All measurement / field names come from model/config.yaml (filled from schema discovery).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

import numpy as np
import pandas as pd
from influxdb_client import InfluxDBClient

from config_util import influx_settings, load_config


def make_client(cfg: dict | None = None) -> tuple[InfluxDBClient, dict[str, str]]:
    cfg = cfg or load_config()
    settings = influx_settings(cfg)
    client = InfluxDBClient(
        url=settings["url"],
        token=settings["token"],
        org=settings["org"],
        timeout=300_000,
    )
    return client, settings


def _qa(client: InfluxDBClient):
    return client.query_api()


def _records_to_rows(tables) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for table in tables:
        for rec in table.records:
            row = dict(rec.values)
            row["_time"] = rec.get_time()
            rows.append(row)
    return rows


def query_pivoted(
    client: InfluxDBClient,
    *,
    bucket: str,
    org: str,
    measurement: str,
    fields: Iterable[str],
    range_start: str = "-30d",
    range_stop: str | None = None,
    extra_filter: str = "",
    tags_keep: Iterable[str] = ("device_id",),
) -> pd.DataFrame:
    fields = list(fields)
    if not fields:
        return pd.DataFrame()
    field_pred = " or ".join(f'r._field == "{f}"' for f in fields)
    stop = f", stop: {range_stop}" if range_stop else ""
    tag_cols = list(tags_keep)
    row_key = '["' + '","'.join(["_time", *tag_cols]) + '"]'
    keep_cols = ["_time", *tag_cols, *fields]
    keep = '["' + '","'.join(keep_cols) + '"]'
    flux = f'''
from(bucket: "{bucket}")
  |> range(start: {range_start}{stop})
  |> filter(fn: (r) => r._measurement == "{measurement}" and ({field_pred}){extra_filter})
  |> pivot(rowKey: {row_key}, columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: {keep})
  |> sort(columns: ["_time"])
'''
    tables = _qa(client).query(flux, org=org)
    rows = _records_to_rows(tables)
    if not rows:
        return pd.DataFrame(columns=["_time", *tag_cols, *fields])
    df = pd.DataFrame(rows)
    df = df.drop(columns=[c for c in df.columns if c in ("result", "table")], errors="ignore")
    df["_time"] = pd.to_datetime(df["_time"], utc=True)
    for f in fields:
        if f in df.columns:
            df[f] = pd.to_numeric(df[f], errors="coerce")
    return df.sort_values("_time").reset_index(drop=True)


def query_telemetry(cfg: dict | None = None, *, since: datetime | None = None) -> pd.DataFrame:
    cfg = cfg or load_config()
    client, settings = make_client(cfg)
    try:
        schema = cfg["schema"]
        features = list(schema["features"])
        # Always pull GPS coords too (for fallback position / sessioning)
        extra = ["gps_lat", "gps_lon", "gps_course_deg", "gps_fresh"]
        fields = sorted(set(features + extra))
        range_start = since.astimezone(timezone.utc).isoformat() if since else cfg["influx"]["range_start"]
        return query_pivoted(
            client,
            bucket=settings["bucket"],
            org=settings["org"],
            measurement=schema["telemetry_measurement"],
            fields=fields,
            range_start=range_start,
            tags_keep=(schema["device_tag"],),
        )
    finally:
        client.close()


def query_position_source(
    cfg: dict | None = None,
    *,
    since: datetime | None = None,
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Return best available position dataframe + source field map used.

    Prefers the source with the widest usable coverage (row count), not just
    the first configured source — dr_position may exist but cover minutes only.
    """
    cfg = cfg or load_config()
    client, settings = make_client(cfg)
    range_start = since.astimezone(timezone.utc).isoformat() if since else cfg["influx"]["range_start"]
    best_df = pd.DataFrame()
    best_src: dict[str, str] | None = None
    try:
        for src in cfg["schema"]["position_sources"]:
            fields = [src["lat"], src["lon"]]
            if src.get("speed"):
                fields.append(src["speed"])
            if src.get("heading"):
                fields.append(src["heading"])
            extra = ""
            if src.get("require_gps_fix"):
                extra = " and exists r.gps_fix"
            df = query_pivoted(
                client,
                bucket=settings["bucket"],
                org=settings["org"],
                measurement=src["measurement"],
                fields=fields,
                range_start=range_start,
                tags_keep=(cfg["schema"]["device_tag"],),
                extra_filter=extra,
            )
            if len(df) > len(best_df):
                best_df, best_src = df, src
        if best_src is None:
            raise RuntimeError("no position sources configured")
        return best_df, best_src
    finally:
        client.close()


def query_decoder_passings(cfg: dict | None = None) -> pd.DataFrame:
    cfg = cfg or load_config()
    client, settings = make_client(cfg)
    try:
        m = cfg["schema"]["decoder_measurement"]
        flux = f'''
from(bucket: "{settings["bucket"]}")
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "{m}" and r.event_type == "passing" and r._field == "last_lap_time")
  |> keep(columns: ["_time", "_value", "session_id", "decoder_id"])
  |> sort(columns: ["_time"])
'''
        tables = _qa(client).query(flux, org=settings["org"])
        rows = []
        for table in tables:
            for rec in table.records:
                rows.append(
                    {
                        "_time": rec.get_time(),
                        "last_lap_time": float(rec.get_value()),
                        "session_id": rec.values.get("session_id"),
                        "decoder_id": rec.values.get("decoder_id"),
                    }
                )
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        df["_time"] = pd.to_datetime(df["_time"], utc=True)
        return df.sort_values("_time").reset_index(drop=True)
    finally:
        client.close()


def assign_telemetry_sessions(
    tele: pd.DataFrame, *, gap_sec: float = 60.0, device_col: str = "device_id"
) -> pd.DataFrame:
    """Split continuous telemetry streams into pseudo-sessions by time gap."""
    if tele.empty:
        out = tele.copy()
        out["session_id"] = []
        return out
    parts: list[pd.DataFrame] = []
    for device, g in tele.groupby(device_col, sort=False):
        g = g.sort_values("_time").copy()
        dt = g["_time"].diff().dt.total_seconds().fillna(0.0)
        new_sess = (dt > gap_sec).cumsum()
        g["session_id"] = [
            f"{device}|{g['_time'].iloc[i].strftime('%Y%m%d-%H%M%S')}|{int(new_sess.iloc[i])}"
            for i in range(len(g))
        ]
        # collapse identical consecutive labels already sequential — rewrite by group
        labels = []
        sid = None
        idx = 0
        times = g["_time"].tolist()
        ns = new_sess.tolist()
        for i, n in enumerate(ns):
            if i == 0 or n != ns[i - 1]:
                sid = f"{device}|{times[i].strftime('%Y%m%d-%H%M%S')}"
                idx += 1
            labels.append(sid)
        g["session_id"] = labels
        parts.append(g)
    return pd.concat(parts, ignore_index=True)


def merge_asof_position(
    tele: pd.DataFrame,
    pos: pd.DataFrame,
    src: dict[str, str],
    *,
    device_col: str = "device_id",
    tolerance_ms: int = 100,
) -> pd.DataFrame:
    """Attach position columns onto telemetry timeline via asof merge per device."""
    if tele.empty:
        return tele.copy()
    if pos.empty:
        out = tele.copy()
        out["pos_lat"] = np.nan
        out["pos_lon"] = np.nan
        out["pos_speed"] = np.nan
        out["pos_heading"] = np.nan
        out["pos_source"] = src["measurement"]
        return out

    lat_c, lon_c = src["lat"], src["lon"]
    pieces: list[pd.DataFrame] = []
    for device, tg in tele.groupby(device_col, sort=False):
        pg = pos[pos[device_col] == device] if device_col in pos.columns else pos
        tg = tg.sort_values("_time")
        pg = pg.sort_values("_time")
        right = pg[["_time", lat_c, lon_c]].copy()
        rename = {lat_c: "pos_lat", lon_c: "pos_lon"}
        if src.get("speed") and src["speed"] in pg.columns:
            right[src["speed"]] = pg[src["speed"]].values
            rename[src["speed"]] = "pos_speed"
        if src.get("heading") and src["heading"] in pg.columns:
            right[src["heading"]] = pg[src["heading"]].values
            rename[src["heading"]] = "pos_heading"
        right = right.rename(columns=rename)
        merged = pd.merge_asof(
            tg,
            right,
            on="_time",
            direction="nearest",
            tolerance=pd.Timedelta(milliseconds=tolerance_ms),
        )
        merged["pos_source"] = src["measurement"]
        pieces.append(merged)
    out = pd.concat(pieces, ignore_index=True)
    # Fallback: use gps_* from telemetry when pos missing
    if "gps_lat" in out.columns:
        out["pos_lat"] = out["pos_lat"].fillna(out["gps_lat"])
        out["pos_lon"] = out["pos_lon"].fillna(out["gps_lon"])
    if "pos_speed" not in out.columns:
        out["pos_speed"] = np.nan
    if "gps_speed_mps" in out.columns:
        out["pos_speed"] = out["pos_speed"].fillna(out["gps_speed_mps"])
    if "pos_heading" not in out.columns:
        out["pos_heading"] = np.nan
    if "gps_course_deg" in out.columns:
        out["pos_heading"] = out["pos_heading"].fillna(out["gps_course_deg"])
    return out


def count_points_since(cfg: dict | None, since: datetime) -> int:
    cfg = cfg or load_config()
    client, settings = make_client(cfg)
    try:
        m = cfg["schema"]["telemetry_measurement"]
        field = cfg["schema"]["features"][0]
        flux = f'''
from(bucket: "{settings["bucket"]}")
  |> range(start: {since.astimezone(timezone.utc).isoformat()})
  |> filter(fn: (r) => r._measurement == "{m}" and r._field == "{field}")
  |> count()
  |> group()
  |> sum(column: "_value")
'''
        tables = _qa(client).query(flux, org=settings["org"])
        for table in tables:
            for rec in table.records:
                return int(rec.get_value() or 0)
        return 0
    finally:
        client.close()
