"""產生單一 F1 風格卡丁車 Grafana 看板（$device 切換 #1/#2）。

產出：
  - kart-telemetry.json  總覽（Telemetry / Motion / Track / Fleet）
"""

from __future__ import annotations

import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "infra" / "grafana" / "dashboards"

DS = {"type": "influxdb", "uid": "influxdb-kpp"}
BUCKET = "decoder"
DEVICE_VAR = "${device}"

KART_01 = "esp32-kart-01"
KART_02 = "esp32-kart-02"

# 楠梓賽道附近預設視角
MAP_LAT = 22.74230485
MAP_LON = 120.32173316
MAP_ZOOM = 18

BASEMAP = {
    "name": "Google Satellite",
    "type": "xyz",
    "config": {
        "url": "https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}",
        "attribution": "&copy; Google",
    },
}

THR_G = [
    {"color": "green", "value": None},
    {"color": "yellow", "value": 1.5},
    {"color": "red", "value": 2.5},
]
THR_DECEL = [
    {"color": "green", "value": None},
    {"color": "yellow", "value": 0.4},
    {"color": "red", "value": 0.9},
]
THR_GRIP = [
    {"color": "green", "value": None},
    {"color": "yellow", "value": 1.0},
    {"color": "red", "value": 1.4},
]
THR_GRIP_PCT = [
    {"color": "green", "value": None},
    {"color": "yellow", "value": 60},
    {"color": "red", "value": 90},
]
THR_SPD = [
    {"color": "green", "value": None},
    {"color": "yellow", "value": 10},
    {"color": "red", "value": 20},
]
THR_SATS = [
    {"color": "red", "value": None},
    {"color": "yellow", "value": 4},
    {"color": "green", "value": 8},
]
THR_HDOP = [
    {"color": "green", "value": None},
    {"color": "yellow", "value": 2},
    {"color": "red", "value": 5},
]


def _device_templating() -> dict:
    return {
        "list": [
            {
                "current": {"selected": True, "text": KART_01, "value": KART_01},
                "hide": 0,
                "includeAll": False,
                "label": "車輛",
                "multi": False,
                "name": "device",
                "options": [
                    {"selected": True, "text": KART_01, "value": KART_01},
                    {"selected": False, "text": KART_02, "value": KART_02},
                ],
                "query": f"{KART_01},{KART_02}",
                "skipUrlSync": False,
                "type": "custom",
            }
        ]
    }


def _shell(*, panels: list[dict]) -> dict:
    return {
        "annotations": {"list": []},
        "editable": True,
        "fiscalYearStartMonth": 0,
        "graphTooltip": 1,
        "id": None,
        "links": [],
        "panels": panels,
        "refresh": "2s",
        "schemaVersion": 39,
        "tags": ["kpp", "telemetry", "kart", "f1-style"],
        "templating": _device_templating(),
        "time": {"from": "now-15m", "to": "now"},
        "timepicker": {},
        "timezone": "browser",
        "title": "Kart Telemetry — F1 Style",
        "uid": "kart-telemetry",
        "version": 1,
    }


def panel_row(pid: int, title: str, y: int) -> dict:
    return {
        "collapsed": False,
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
        "id": pid,
        "panels": [],
        "title": title,
        "type": "row",
    }


def flux_last(field: str) -> str:
    return (
        f'from(bucket: "{BUCKET}")\n'
        f"  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
        f'  |> filter(fn: (r) => r._measurement == "kart_telemetry"'
        f' and r.device_id == "{DEVICE_VAR}" and r._field == "{field}")\n'
        f"  |> last()"
    )


def flux_ts(fields: list[str], *, agg: str = "mean") -> str:
    field_expr = " or ".join(f'r._field == "{f}"' for f in fields)
    return (
        f'from(bucket: "{BUCKET}")\n'
        f"  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
        f'  |> filter(fn: (r) => r._measurement == "kart_telemetry"'
        f' and r.device_id == "{DEVICE_VAR}")\n'
        f"  |> filter(fn: (r) => {field_expr})\n"
        f"  |> aggregateWindow(every: v.windowPeriod, fn: {agg}, createEmpty: false)\n"
        f'  |> yield(name: "{agg}")'
    )


def flux_gps_path(device_id: str) -> str:
    return (
        f'from(bucket: "{BUCKET}")\n'
        f"  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
        f'  |> filter(fn: (r) => r._measurement == "kart_telemetry"'
        f' and r.device_id == "{device_id}" and r.gps_fix == "1")\n'
        f'  |> filter(fn: (r) => r._field == "gps_lat" or r._field == "gps_lon"'
        f' or r._field == "gps_speed_mps")\n'
        f"  |> aggregateWindow(every: 200ms, fn: last, createEmpty: false)\n"
        f'  |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
        f'  |> keep(columns: ["_time", "gps_lat", "gps_lon", "gps_speed_mps"])\n'
        f'  |> filter(fn: (r) => exists r.gps_lat and exists r.gps_lon)\n'
        f'  |> sort(columns: ["_time"])'
    )


def flux_dr_path() -> str:
    return (
        f'from(bucket: "{BUCKET}")\n'
        f"  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
        f'  |> filter(fn: (r) => r._measurement == "dr_position"'
        f' and r.device_id == "{DEVICE_VAR}")\n'
        f'  |> filter(fn: (r) => r._field == "lat_dr" or r._field == "lon_dr"'
        f' or r._field == "speed_mps")\n'
        f"  |> aggregateWindow(every: 200ms, fn: last, createEmpty: false)\n"
        f'  |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
        f'  |> keep(columns: ["_time", "lat_dr", "lon_dr", "speed_mps"])\n'
        f'  |> filter(fn: (r) => exists r.lat_dr and exists r.lon_dr)\n'
        f'  |> sort(columns: ["_time"])'
    )


# a_lon < 0 → 減速量（g）；grip = √(a_lat²+a_lon²)；% 相對 1.6g 圓
_GRIP_LIMIT_G = 1.6


def flux_motion_derived(*, keep: list[str], every: str = "v.windowPeriod") -> str:
    keep_cols = ", ".join(f'"{c}"' for c in ["_time", *keep])
    return (
        'import "math"\n'
        f'from(bucket: "{BUCKET}")\n'
        f"  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
        f'  |> filter(fn: (r) => r._measurement == "kart_telemetry"'
        f' and r.device_id == "{DEVICE_VAR}")\n'
        f'  |> filter(fn: (r) => r._field == "a_lat" or r._field == "a_lon")\n'
        f"  |> aggregateWindow(every: {every}, fn: last, createEmpty: false)\n"
        f'  |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
        f"  |> filter(fn: (r) => exists r.a_lat and exists r.a_lon)\n"
        f"  |> map(fn: (r) => ({{ r with\n"
        f"      decel_g: if r.a_lon < 0.0 then 0.0 - r.a_lon else 0.0,\n"
        f"      grip_g: math.sqrt(r.a_lat * r.a_lat + r.a_lon * r.a_lon),\n"
        f"      grip_pct: math.sqrt(r.a_lat * r.a_lat + r.a_lon * r.a_lon)"
        f" / {_GRIP_LIMIT_G} * 100.0,\n"
        f"  }}))\n"
        f"  |> keep(columns: [{keep_cols}])"
    )


def flux_decel_markers() -> str:
    """GPS 位置上的減速標註（decel_g 超過門檻才留）。"""
    return (
        'import "math"\n'
        f'from(bucket: "{BUCKET}")\n'
        f"  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
        f'  |> filter(fn: (r) => r._measurement == "kart_telemetry"'
        f' and r.device_id == "{DEVICE_VAR}" and r.gps_fix == "1")\n'
        f'  |> filter(fn: (r) => r._field == "a_lon" or r._field == "gps_lat"'
        f' or r._field == "gps_lon")\n'
        f"  |> aggregateWindow(every: 200ms, fn: last, createEmpty: false)\n"
        f'  |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
        f"  |> filter(fn: (r) => exists r.gps_lat and exists r.gps_lon and exists r.a_lon)\n"
        f"  |> map(fn: (r) => ({{ r with\n"
        f"      decel_g: if r.a_lon < 0.0 then 0.0 - r.a_lon else 0.0,\n"
        f"  }}))\n"
        f"  |> filter(fn: (r) => r.decel_g >= 0.25)\n"
        f'  |> keep(columns: ["_time", "gps_lat", "gps_lon", "decel_g"])\n'
        f'  |> sort(columns: ["_time"])'
    )


def panel_stat_query(
    pid: int,
    title: str,
    query: str,
    x: int,
    y: int,
    *,
    w: int = 6,
    h: int = 5,
    unit: str = "none",
    thresholds: list[dict] | None = None,
    calc: str = "lastNotNull",
) -> dict:
    steps = thresholds or [{"color": "green", "value": None}]
    return {
        "datasource": DS,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "thresholds"},
                "thresholds": {"mode": "absolute", "steps": steps},
                "unit": unit,
                "decimals": 2,
            },
            "overrides": [],
        },
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "id": pid,
        "options": {
            "colorMode": "background",
            "graphMode": "area",
            "reduceOptions": {"calcs": [calc], "fields": "", "values": False},
        },
        "targets": [{"datasource": DS, "query": query, "refId": "A"}],
        "title": title,
        "type": "stat",
    }


def panel_ts_query(
    pid: int,
    title: str,
    query: str,
    x: int,
    y: int,
    *,
    w: int = 12,
    h: int = 9,
    unit: str = "none",
    overrides: list[dict] | None = None,
) -> dict:
    return {
        "datasource": DS,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "palette-classic"},
                "custom": {
                    "drawStyle": "line",
                    "fillOpacity": 12,
                    "lineWidth": 2,
                    "showPoints": "never",
                    "spanNulls": True,
                },
                "unit": unit,
            },
            "overrides": overrides or [],
        },
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "id": pid,
        "options": {
            "legend": {"calcs": ["mean", "max"], "displayMode": "list", "placement": "bottom"},
            "tooltip": {"mode": "multi", "sort": "none"},
        },
        "targets": [{"datasource": DS, "query": query, "refId": "A"}],
        "title": title,
        "type": "timeseries",
    }


def panel_decel_geomap(pid: int, x: int, y: int) -> dict:
    return {
        "datasource": DS,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "continuous-YlOrRd"},
                "max": 1.5,
                "min": 0.25,
                "unit": "accG",
            },
            "overrides": [],
        },
        "gridPos": {"h": 16, "w": 12, "x": x, "y": y},
        "id": pid,
        "options": {
            "view": {
                "allLayers": False,
                "id": "coords",
                "lat": MAP_LAT,
                "lon": MAP_LON,
                "zoom": MAP_ZOOM,
            },
            "controls": {
                "showZoom": False,
                "mouseWheelZoom": False,
                "showAttribution": True,
                "showScale": True,
                "showMeasure": False,
            },
            "basemap": BASEMAP,
            "layers": [
                {
                    "type": "route",
                    "name": "減速點",
                    "config": {
                        "style": {
                            "size": {"fixed": 6},
                            "color": {"field": "decel_g", "fixed": "dark-red"},
                            "opacity": 0.95,
                        }
                    },
                    "location": {
                        "mode": "coords",
                        "latitude": "gps_lat",
                        "longitude": "gps_lon",
                    },
                    "filterData": {"id": "byRefId", "options": "A"},
                },
            ],
        },
        "targets": [
            {"datasource": DS, "query": flux_decel_markers(), "refId": "A"},
        ],
        "title": "煞車點（GPS 上標示減速 >=0.25g）",
        "type": "geomap",
    }


def panel_stat(
    pid: int,
    title: str,
    field: str,
    x: int,
    y: int,
    *,
    w: int = 6,
    h: int = 5,
    unit: str = "none",
    thresholds: list[dict] | None = None,
) -> dict:
    steps = thresholds or [{"color": "green", "value": None}]
    return {
        "datasource": DS,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "thresholds"},
                "thresholds": {"mode": "absolute", "steps": steps},
                "unit": unit,
            },
            "overrides": [],
        },
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "id": pid,
        "options": {
            "colorMode": "background",
            "graphMode": "area",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
        "targets": [{"datasource": DS, "query": flux_last(field), "refId": "A"}],
        "title": title,
        "type": "stat",
    }


def panel_ts(
    pid: int,
    title: str,
    fields: list[str],
    x: int,
    y: int,
    *,
    w: int = 12,
    h: int = 9,
    unit: str = "none",
    agg: str = "mean",
    overrides: list[dict] | None = None,
) -> dict:
    return {
        "datasource": DS,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "palette-classic"},
                "custom": {
                    "drawStyle": "line",
                    "fillOpacity": 12,
                    "lineWidth": 2,
                    "showPoints": "never",
                    "spanNulls": True,
                },
                "unit": unit,
            },
            "overrides": overrides or [],
        },
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "id": pid,
        "options": {
            "legend": {"calcs": ["mean", "max"], "displayMode": "list", "placement": "bottom"},
            "tooltip": {"mode": "multi", "sort": "none"},
        },
        "targets": [{"datasource": DS, "query": flux_ts(fields, agg=agg), "refId": "A"}],
        "title": title,
        "type": "timeseries",
    }


def panel_grip(pid: int, x: int, y: int) -> dict:
    # Grafana 11 XY Chart：dims 只要 frame + x（其餘數值欄當 Y）；
    # 舊的 dims.y / 缺 frame 會直接渲染成面板中央 "Err"。
    # 參考圓固定當 frame 0，即使沒有 a_lat 資料也不會炸。
    circle_rows = ", ".join(
        f"{{a_lat: {1.6 * math.cos(i * math.pi / 16):.4f},"
        f" a_lon: {1.6 * math.sin(i * math.pi / 16):.4f}}}"
        for i in range(33)
    )
    return {
        "datasource": DS,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "palette-classic"},
                "custom": {
                    "pointSize": {"fixed": 5, "min": 1, "max": 20},
                    "show": "points",
                },
                "unit": "short",
            },
            "overrides": [
                {
                    "matcher": {"id": "byFrameRefID", "options": "A"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "#888888", "mode": "fixed"}},
                        {
                            "id": "custom.show",
                            "value": "lines",
                        },
                        {
                            "id": "custom.lineWidth",
                            "value": 1,
                        },
                        {
                            "id": "custom.pointSize",
                            "value": {"fixed": 2, "min": 1, "max": 20},
                        },
                        {"id": "displayName", "value": "1.6g ring"},
                    ],
                },
                {
                    "matcher": {"id": "byFrameRefID", "options": "B"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "orange", "mode": "fixed"}},
                        {"id": "custom.show", "value": "points"},
                        {"id": "displayName", "value": "G"},
                    ],
                },
            ],
        },
        "gridPos": {"h": 11, "w": 12, "x": x, "y": y},
        "id": pid,
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "single", "sort": "none"},
            "dims": {"frame": 0, "x": "a_lat", "exclude": ["_time"]},
            "seriesMapping": "manual",
            "series": [
                {
                    "frame": 0,
                    "x": "a_lat",
                    "y": "a_lon",
                    "name": "1.6g ring",
                    "show": "lines",
                    "pointSize": {"fixed": 2, "min": 1, "max": 20},
                    "lineWidth": 1,
                },
                {
                    "frame": 1,
                    "x": "a_lat",
                    "y": "a_lon",
                    "name": "G",
                    "show": "points",
                    "pointSize": {"fixed": 5, "min": 1, "max": 20},
                },
            ],
        },
        "targets": [
            {
                "datasource": DS,
                "query": f'import "array"\narray.from(rows: [{circle_rows}])',
                "refId": "A",
            },
            {
                "datasource": DS,
                "query": (
                    f'from(bucket: "{BUCKET}")\n'
                    f"  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
                    f'  |> filter(fn: (r) => r._measurement == "kart_telemetry"'
                    f' and r.device_id == "{DEVICE_VAR}")\n'
                    f'  |> filter(fn: (r) => r._field == "a_lat" or r._field == "a_lon")\n'
                    f"  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)\n"
                    f'  |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
                    f'  |> keep(columns: ["_time", "a_lat", "a_lon"])\n'
                    f"  |> filter(fn: (r) => exists r.a_lat and exists r.a_lon)"
                ),
                "refId": "B",
            },
        ],
        "title": "G Force（a_lat × a_lon）",
        "type": "xychart",
    }


def panel_geomap_device(pid: int, y: int) -> dict:
    return {
        "datasource": DS,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "continuous-GrYlRd"},
                "max": 20,
                "min": 0,
                "unit": "velocitymps",
            },
            "overrides": [],
        },
        "gridPos": {"h": 22, "w": 24, "x": 0, "y": y},
        "id": pid,
        "options": {
            "view": {
                "allLayers": True,
                "id": "coords",
                "lat": MAP_LAT,
                "lon": MAP_LON,
                "zoom": MAP_ZOOM,
            },
            "controls": {
                "showZoom": False,
                "mouseWheelZoom": False,
                "showAttribution": True,
                "showScale": True,
                "showMeasure": False,
            },
            "basemap": BASEMAP,
            "layers": [
                {
                    "type": "route",
                    "name": "DR（speed_mps 著色）",
                    "config": {
                        "style": {
                            "size": {"fixed": 3},
                            "color": {"field": "speed_mps", "fixed": "dark-green"},
                            "opacity": 0.85,
                        }
                    },
                    "location": {
                        "mode": "coords",
                        "latitude": "lat_dr",
                        "longitude": "lon_dr",
                    },
                    "filterData": {"id": "byRefId", "options": "A"},
                },
                {
                    "type": "route",
                    "name": "GPS（gps_speed_mps 著色）",
                    "config": {
                        "style": {
                            "size": {"fixed": 4},
                            "color": {"field": "gps_speed_mps", "fixed": "dark-blue"},
                            "opacity": 0.9,
                        }
                    },
                    "location": {
                        "mode": "coords",
                        "latitude": "gps_lat",
                        "longitude": "gps_lon",
                    },
                    "filterData": {"id": "byRefId", "options": "B"},
                },
            ],
        },
        "targets": [
            {"datasource": DS, "query": flux_dr_path(), "refId": "A"},
            {"datasource": DS, "query": flux_gps_path(DEVICE_VAR), "refId": "B"},
        ],
        "title": "賽道走線（$device：DR + GPS，時速著色）",
        "type": "geomap",
    }


def panel_dual_speed(pid: int, y: int) -> dict:
    return {
        "datasource": DS,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "palette-classic"},
                "custom": {
                    "drawStyle": "line",
                    "fillOpacity": 10,
                    "lineWidth": 2,
                    "showPoints": "never",
                    "spanNulls": True,
                },
                "unit": "velocitymps",
            },
            "overrides": [
                {
                    "matcher": {"id": "byFrameRefID", "options": "A"},
                    "properties": [
                        {"id": "displayName", "value": "#1 時速"},
                        {"id": "color", "value": {"fixedColor": "blue", "mode": "fixed"}},
                    ],
                },
                {
                    "matcher": {"id": "byFrameRefID", "options": "B"},
                    "properties": [
                        {"id": "displayName", "value": "#2 時速"},
                        {"id": "color", "value": {"fixedColor": "red", "mode": "fixed"}},
                    ],
                },
            ],
        },
        "gridPos": {"h": 8, "w": 24, "x": 0, "y": y},
        "id": pid,
        "options": {
            "legend": {"calcs": ["mean", "max"], "displayMode": "list", "placement": "bottom"},
            "tooltip": {"mode": "multi", "sort": "none"},
        },
        "targets": [
            {
                "datasource": DS,
                "query": flux_ts(["gps_speed_mps"], agg="mean").replace(
                    DEVICE_VAR, KART_01
                ),
                "refId": "A",
            },
            {
                "datasource": DS,
                "query": flux_ts(["gps_speed_mps"], agg="mean").replace(
                    DEVICE_VAR, KART_02
                ),
                "refId": "B",
            },
        ],
        "title": "雙車 GPS 時速",
        "type": "timeseries",
    }


def panel_dual_geomap(pid: int, y: int) -> dict:
    return {
        "datasource": DS,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "thresholds"},
                "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}]},
                "unit": "velocitymps",
            },
            "overrides": [],
        },
        "gridPos": {"h": 22, "w": 24, "x": 0, "y": y},
        "id": pid,
        "options": {
            "view": {
                "allLayers": True,
                "id": "coords",
                "lat": MAP_LAT,
                "lon": MAP_LON,
                "zoom": MAP_ZOOM,
            },
            "controls": {
                "showZoom": False,
                "mouseWheelZoom": False,
                "showAttribution": True,
                "showScale": True,
                "showMeasure": False,
            },
            "basemap": BASEMAP,
            "layers": [
                {
                    "type": "route",
                    "name": "#1 esp32-kart-01（藍）",
                    "config": {
                        "style": {
                            "size": {"fixed": 4},
                            "color": {"fixed": "blue"},
                            "opacity": 0.9,
                        }
                    },
                    "location": {
                        "mode": "coords",
                        "latitude": "gps_lat",
                        "longitude": "gps_lon",
                    },
                    "filterData": {"id": "byRefId", "options": "A"},
                },
                {
                    "type": "route",
                    "name": "#2 esp32-kart-02（紅）",
                    "config": {
                        "style": {
                            "size": {"fixed": 4},
                            "color": {"fixed": "red"},
                            "opacity": 0.9,
                        }
                    },
                    "location": {
                        "mode": "coords",
                        "latitude": "gps_lat",
                        "longitude": "gps_lon",
                    },
                    "filterData": {"id": "byRefId", "options": "B"},
                },
            ],
        },
        "targets": [
            {"datasource": DS, "query": flux_gps_path(KART_01), "refId": "A"},
            {"datasource": DS, "query": flux_gps_path(KART_02), "refId": "B"},
        ],
        "title": "雙車 GPS 軌跡（#1 藍 / #2 紅）",
        "type": "geomap",
    }


def make_kart_telemetry() -> dict:
    # gridPos 佈局（24 欄）：
    #   Overview  y=0..12  row + 2×4 stats (w=6 h=6)
    #   Telemetry y=13..33 Speed 整排 + GY-85/MPU 各半排
    #   Motion    y=34..66 Accel/Gyro/|a|/Yaw h=11；G-force h=11
    #   Track     y=67..81 geomap h=14
    #   Fleet     y=82..104 dual speed + dual geomap
    panels: list[dict] = [
        panel_row(100, "Overview", 0),
        panel_stat(1, "GPS 時速", "gps_speed_mps", 0, 1, w=6, h=6, unit="velocitymps", thresholds=THR_SPD),
        panel_stat(2, "衛星", "gps_satellites", 6, 1, w=6, h=6, unit="none", thresholds=THR_SATS),
        panel_stat(3, "HDOP", "gps_hdop", 12, 1, w=6, h=6, unit="none", thresholds=THR_HDOP),
        panel_stat(4, "航向", "gps_course_deg", 18, 1, w=6, h=6, unit="degree"),
        panel_stat(5, "gps_fresh", "gps_fresh", 0, 7, w=6, h=6, unit="none"),
        panel_stat(6, "|a|", "accel_mag", 6, 7, w=6, h=6, unit="accG", thresholds=THR_G),
        panel_stat(7, "accel_dyn", "accel_dyn", 12, 7, w=6, h=6, unit="accG", thresholds=THR_G),
        panel_stat(8, "IMU 溫", "imu_temp_c", 18, 7, w=6, h=6, unit="celsius"),
        panel_row(101, "Telemetry", 13),
        panel_ts(
            12,
            "Speed",
            ["gps_speed_mps"],
            0,
            14,
            w=24,
            h=10,
            unit="velocitymps",
            agg="mean",
        ),
        panel_ts(
            13,
            "GY-85 Accel XYZ",
            ["gy85_ax", "gy85_ay", "gy85_az"],
            0,
            24,
            w=12,
            h=10,
            unit="accG",
            agg="max",
        ),
        panel_ts(
            14,
            "MPU6050 Accel XYZ",
            ["mpu_ax", "mpu_ay", "mpu_az"],
            12,
            24,
            w=12,
            h=10,
            unit="accG",
            agg="max",
        ),
        panel_row(102, "Motion", 34),
        panel_ts(20, "Accel XYZ", ["ax", "ay", "az"], 0, 35, h=11, unit="accG", agg="max"),
        panel_ts(21, "Gyro XYZ", ["gx", "gy", "gz"], 12, 35, h=11, unit="dps", agg="max"),
        panel_ts(
            22,
            "|a| / accel_dyn",
            ["accel_mag", "accel_dyn"],
            0,
            46,
            h=11,
            unit="accG",
            agg="max",
        ),
        panel_ts(23, "Yaw rate (gz)", ["gz"], 12, 46, h=11, unit="dps", agg="max"),
        panel_grip(24, 0, 57),
        panel_ts(
            25,
            "G lateral / longitudinal",
            ["a_lat", "a_lon"],
            12,
            57,
            h=11,
            unit="accG",
            agg="max",
        ),
        panel_row(105, "煞車與抓地力", 68),
        panel_stat_query(
            50,
            "煞車力",
            flux_motion_derived(keep=["decel_g"]) + "\n  |> last()",
            0,
            69,
            w=6,
            h=5,
            unit="accG",
            thresholds=THR_DECEL,
            calc="lastNotNull",
        ),
        panel_stat_query(
            51,
            "峰值煞車",
            flux_motion_derived(keep=["decel_g"]) + "\n  |> max(column: \"decel_g\")",
            6,
            69,
            w=6,
            h=5,
            unit="accG",
            thresholds=THR_DECEL,
            calc="max",
        ),
        panel_stat_query(
            52,
            "抓地力",
            flux_motion_derived(keep=["grip_g"]) + "\n  |> last()",
            12,
            69,
            w=6,
            h=5,
            unit="accG",
            thresholds=THR_GRIP,
            calc="lastNotNull",
        ),
        panel_stat_query(
            53,
            "抓地力 %（/1.6g）",
            flux_motion_derived(keep=["grip_pct"]) + "\n  |> last()",
            18,
            69,
            w=6,
            h=5,
            unit="percent",
            thresholds=THR_GRIP_PCT,
            calc="lastNotNull",
        ),
        panel_ts_query(
            54,
            "煞車力 / 抓地力",
            flux_motion_derived(keep=["decel_g", "grip_g"]),
            0,
            74,
            w=12,
            h=16,
            unit="accG",
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "decel_g"},
                    "properties": [
                        {"id": "displayName", "value": "煞車力"},
                        {"id": "color", "value": {"fixedColor": "red", "mode": "fixed"}},
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "grip_g"},
                    "properties": [
                        {"id": "displayName", "value": "抓地力"},
                        {"id": "color", "value": {"fixedColor": "purple", "mode": "fixed"}},
                    ],
                },
            ],
        ),
        panel_decel_geomap(55, 12, 74),
        panel_row(103, "Track", 90),
        panel_geomap_device(30, 91),
        panel_row(104, "Fleet", 113),
        panel_dual_speed(40, 114),
        panel_dual_geomap(41, 122),
    ]
    return _shell(panels=panels)


def _validate_dashboard(name: str, dash: dict) -> None:
    banned = (
        "wingdamage",
        "tyrewear",
        "tyrecompound",
        "fuelintank",
        "enginerpm",
        "throttle",
        "brake",
        "steer",
        "clutch",
        "semi-dark-orange",
    )
    raw = json.dumps(dash, ensure_ascii=False)
    for token in banned:
        if token in raw.lower():
            raise SystemExit(f"{name} still references F1-only field: {token}")

    has_device_var = False
    has_geomap = False
    for pan in dash["panels"]:
        if pan.get("type") == "geomap":
            has_geomap = True
        for t in pan.get("targets", []):
            q = t.get("query", "")
            if not isinstance(q, str):
                continue
            if "${device}" in q:
                has_device_var = True
            if "kart_telemetry" in q and "device_id ==" not in q:
                raise SystemExit(f"{name} panel {pan.get('id')} missing device_id")
            if "dr_position" in q and "device_id ==" not in q:
                raise SystemExit(f"{name} panel {pan.get('id')} DR missing device_id")

    templating = dash.get("templating", {}).get("list", [])
    if not any(v.get("name") == "device" for v in templating):
        raise SystemExit(f"{name} missing device template variable")
    if not has_device_var:
        raise SystemExit(f"{name} missing ${{device}} in queries")
    if not has_geomap:
        raise SystemExit(f"{name} missing geomap panel")
    if dash.get("uid") != "kart-telemetry":
        raise SystemExit(f"{name} wrong uid")


def main() -> None:
    for name in (
        "esp32-kart-01.json",
        "esp32-kart-02.json",
        "gps_dual_track.json",
    ):
        p = OUT_DIR / name
        if p.exists():
            p.unlink()
            print(f"removed {p}")

    dash = make_kart_telemetry()
    _validate_dashboard("kart-telemetry.json", dash)

    out = OUT_DIR / "kart-telemetry.json"
    out.write_text(json.dumps(dash, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out} panels={len(dash['panels'])} uid={dash['uid']}")


if __name__ == "__main__":
    main()
