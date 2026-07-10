"""用 PASSING_CALIBRATION_PATH 校正 log，驗證／覆寫 DECODER_TICK_HZ。

預設 AmbRC/MyLaps 除數是 256000（圈時 = tick_delta / 256000）。本工具用來
對照官方 Lap Tm，確認現場硬體世代是否用同一個 divisor；若中位數明顯偏離
256000，再把結果貼進 .env 覆寫。

用法：
    1. 在 .env 設定 PASSING_CALIBRATION_PATH（例如
       services/decoder_ingest/calibration.log），重啟服務。
    2. 找一台車跑一段乾淨的單獨測試（避免同時多車、避免中途停很久），
       同時把 MYLAPS 官方計時螢幕的「Lap Tm」欄位由上到下依序記下來
       （見專案截圖範例：51.805, 49.769, 49.804, ...）。
    3. 執行本工具：

       python scripts/calibrate_tick_hz.py \
           --log services/decoder_ingest/calibration.log \
           --tid 140211241C6D \
           --reference 51.805,49.769,49.804,49.633,49.794,49.162,49.339,53.104,49.286,53.248,49.514

    4. 若中位數接近 256000，可維持預設；否則把印出來的 DECODER_TICK_HZ
       貼進 .env 覆寫。留空 DECODER_TICK_HZ= 會關閉 tick、改用 wall-clock。
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

LINE_PATTERN = re.compile(
    r"^(?P<ts>\S+) \| (?P<raw>\S+) \| tid=(?P<tid>\S+) tick=(?P<tick>\S+) hit=(?P<hit>\S+)$"
)

# 常見的 tick 頻率，估計值若落在其中一個附近 1% 內就順便提示，方便肉眼
# 確認校正結果合理（不是必要條件，估計值本身才是要填進 .env 的數字）。
COMMON_HZ_CANDIDATES = [100.0, 1000.0, 8000.0, 10000.0, 32768.0, 256000.0]


@dataclass(slots=True)
class Passing:
    timestamp: datetime
    tick: int


def parse_log(path: Path, tid: str) -> list[Passing]:
    passings: list[Passing] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            m = LINE_PATTERN.match(line)
            if not m:
                continue
            if m.group("tid").upper() != tid.upper():
                continue
            tick_raw = m.group("tick")
            if tick_raw == "None":
                continue
            try:
                passings.append(
                    Passing(
                        timestamp=datetime.fromisoformat(m.group("ts")),
                        tick=int(tick_raw),
                    )
                )
            except ValueError:
                print(f"警告：第 {line_no} 行格式異常，略過", file=sys.stderr)
    return passings


def dedupe_noise(passings: list[Passing], *, min_gap_sec: float) -> list[Passing]:
    """跟 LapTracker 的 noise_threshold 邏輯一致：間隔小於 min_gap_sec 的
    視為同一次通過的雙觸發雜訊，只保留第一筆。
    """
    if not passings:
        return []
    result = [passings[0]]
    for p in passings[1:]:
        if (p.timestamp - result[-1].timestamp).total_seconds() < min_gap_sec:
            continue
        result.append(p)
    return result


def tick_delta(prev: int, curr: int, *, tick_byte_len: int) -> int:
    modulus = 1 << (8 * tick_byte_len)
    return (curr - prev) % modulus


def suggest_common_hz(estimate: float) -> float | None:
    for candidate in COMMON_HZ_CANDIDATES:
        if abs(estimate - candidate) / candidate <= 0.01:
            return candidate
    return None


def main() -> int:
    # Windows 主控台預設編碼常是 cp950/cp936 而非 UTF-8，中文字元會亂碼；
    # 強制輸出用 UTF-8，避免校正結果因為顯示亂碼而看錯數字。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", required=True, type=Path, help="PASSING_CALIBRATION_PATH 產生的校正 log 檔案")
    parser.add_argument("--tid", required=True, help="要校正的 transponder_id（12 碼 hex，大小寫不拘）")
    parser.add_argument(
        "--reference",
        required=True,
        help="MYLAPS 官方 Lap Tm，依時間先後、逗號分隔，例如 51.805,49.769,49.804",
    )
    parser.add_argument(
        "--tick-byte-len",
        type=int,
        default=4,
        help="tick 計數器位元組長度（預設 4，即 32-bit / 8 hex 碼）",
    )
    parser.add_argument(
        "--min-gap-sec",
        type=float,
        default=10.0,
        help="雙觸發雜訊去重門檻，需與 LAP_NOISE_THRESHOLD_SEC 一致（預設 10.0）",
    )
    args = parser.parse_args()

    if not args.log.exists():
        print(f"錯誤：找不到 log 檔案 {args.log}", file=sys.stderr)
        return 1

    reference = [float(x.strip()) for x in args.reference.split(",") if x.strip()]
    if not reference:
        print("錯誤：--reference 至少要有一筆圈時", file=sys.stderr)
        return 1

    passings = parse_log(args.log, args.tid)
    passings = dedupe_noise(passings, min_gap_sec=args.min_gap_sec)

    lap_count = len(passings) - 1
    if lap_count <= 0:
        print(
            f"錯誤：log 裡只找到 {len(passings)} 次通過（tid={args.tid}），"
            "至少要 2 次通過才能算出 1 圈的 tick 差。",
            file=sys.stderr,
        )
        return 1

    if lap_count != len(reference):
        print(
            f"錯誤：log 算出 {lap_count} 圈的 tick 資料，但 --reference 給了 "
            f"{len(reference)} 筆官方圈時，數量對不上。\n"
            "請確認 log 只含這台車、這一段測試（沒有混到其他次通過），"
            "或調整 --min-gap-sec 後再試一次。",
            file=sys.stderr,
        )
        return 1

    estimates: list[float] = []
    print(f"{'圈':>3} | {'時間戳差(s)':>10} | {'tick差':>10} | {'官方圈時(s)':>10} | {'推算Hz':>12}")
    print("-" * 60)
    for i in range(lap_count):
        prev, curr = passings[i], passings[i + 1]
        wall_delta = (curr.timestamp - prev.timestamp).total_seconds()
        delta = tick_delta(prev.tick, curr.tick, tick_byte_len=args.tick_byte_len)
        ref = reference[i]
        estimate = delta / ref
        estimates.append(estimate)
        print(f"{i + 1:>3} | {wall_delta:>10.3f} | {delta:>10d} | {ref:>10.3f} | {estimate:>12.3f}")

    median = statistics.median(estimates)
    outliers = [
        (i + 1, e) for i, e in enumerate(estimates) if abs(e - median) / median > 0.02
    ]

    print("-" * 60)
    print(f"中位數估計值: {median:.3f} Hz")
    if len(estimates) > 1:
        print(f"標準差: {statistics.stdev(estimates):.3f}")
    if outliers:
        print(
            "警告：以下圈的估計值跟中位數相差超過 2%，可能是雜訊誤判、"
            "或官方 Lap Tm 對應順序有誤："
        )
        for lap_no, e in outliers:
            print(f"  第 {lap_no} 圈: {e:.3f} Hz")

    common = suggest_common_hz(median)
    if common is not None:
        print(f"提示：估計值接近常見頻率 {common:.0f} Hz，可考慮直接使用整數值。")

    print()
    print(f"把下面這行貼進 .env（若與預設 256000 不同才需要覆寫）：")
    print(f"DECODER_TICK_HZ={median:.3f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
