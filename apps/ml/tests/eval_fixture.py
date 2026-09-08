"""ベースラインの検査で使う、手で組み立てたサンプル。

**フィクスチャは小さく、答えが手で書ける大きさにする。** 本物の 1 日ぶん
（205 万行）では、指標がずれていても気づけない。
"""

from collections.abc import Sequence
from datetime import date, datetime, timedelta

import pyarrow as pa

from bikechance_ml.eval.dataset import NEEDED_COLUMNS
from bikechance_ml.features.grid import JST

#: 検査で使う日（月曜から）。
DAYS: tuple[date, ...] = (date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9))


def row(
    day: date,
    system_id: str,
    station_id: str,
    h_min: int,
    bikes: int,
    docks: int,
    y_bike: int,
    y_dock: int,
    weight: float = 100.0,
    minute_of_day: int = 600,
    dow_type: str = "weekday",
) -> dict[str, object]:
    """1 行ぶん。**既定は「素直な平日の 10:00」。**"""
    base = datetime(day.year, day.month, day.day, tzinfo=JST) + timedelta(minutes=minute_of_day)
    return {
        "system_id": system_id,
        "station_id": station_id,
        "t": base,
        "h_min": h_min,
        "y_bike": y_bike,
        "y_dock": y_dock,
        "weight": weight,
        "bikes": bikes,
        "docks": docks,
        "minute_of_day": minute_of_day,
        "target_dow_type": dow_type,
    }


def to_table(rows: Sequence[dict[str, object]]) -> pa.Table:
    """行の並びを、学習サンプルと同じ列の表にする。"""
    types = {
        "system_id": pa.string(),
        "station_id": pa.string(),
        "t": pa.timestamp("ms", tz="UTC"),
        "h_min": pa.int16(),
        "y_bike": pa.int8(),
        "y_dock": pa.int8(),
        "weight": pa.float32(),
        "bikes": pa.int16(),
        "docks": pa.int16(),
        "minute_of_day": pa.int16(),
        "target_dow_type": pa.string(),
    }
    return pa.table(
        {name: pa.array([one[name] for one in rows], type=types[name]) for name in NEEDED_COLUMNS}
    )
