"""推論の入出力を差し替えるための仕込み（W4 プラン §6.3）。

**本番と同じものを読ませる。** PR C から、推論は
  1. 前日の**参照スナップショット**（Storage の Parquet 2 本）
  2. 自系統の **`status_snapshots` の 2 つの窓**
  3. 他系統の**直近 1 本**
  4. **`at` までに入手できた予報**（PR D）
を読む。ここではその 4 つを、本物と同じ形で組み立てる。

**形を偽らない。** 参照スナップショットは `features/reference_snapshot.py` の
関数で作り、観測は `jobs/snapshot_table.py` の `Snapshot` で持つ。だから
「テストは通るのに本番で落ちる」が起きにくい。
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Final

import pyarrow as pa

from bikechance_ml.features.constants import GRID_MINUTES, WEATHER_LEAD_HOURS
from bikechance_ml.features.grid import reference_path
from bikechance_ml.features.reference import (
    NeighborRow,
    StationAttributeRow,
    StationGeoRow,
    SystemReference,
)
from bikechance_ml.features.reference_snapshot import (
    NEIGHBORS_NAME,
    STATIONS_NAME,
    to_neighbors_table,
    to_stations_table,
)
from bikechance_ml.features.weather import SERIES as WEATHER_SERIES
from bikechance_ml.features.weather import WeatherRow
from bikechance_ml.jobs.build_features import to_parquet_bytes
from bikechance_ml.jobs.snapshot_table import SCHEMA as SNAPSHOT_SCHEMA
from bikechance_ml.jobs.snapshot_table import Snapshot, StationRow

#: 仕込みのポート。台数は「切らしている / 少ない / 余っている」の 3 通り。
STATIONS: Final[tuple[tuple[str, int, int, int], ...]] = (
    ("a", 0, 9, 7),
    ("b", 1, 8, 7),
    ("c", 7, 2, 7),
)

#: 取り込みの遅れ。**5 分周期と同じ長さにしない**（as-of が退化する）。
FETCH_DELAY: Final[timedelta] = timedelta(seconds=40)

SYSTEMS: Final[tuple[str, ...]] = ("hellocycling", "docomo-cycle")

#: 仕込みのポート（lat 35.68〜35.682 / lon 139.76）が落ちる気象格子。
WEATHER_CELL: Final[tuple[int, int]] = (714, 2236)

#: 予報を入手する時刻（毎正時からの分）。本番の実測は 17.6 分（`v_weather_files`）。
WEATHER_DELAY: Final[timedelta] = timedelta(minutes=18)


def ledger(station_ids: Sequence[str]) -> tuple[StationRow, ...]:
    """台帳。**`idx` は 0 起点で密**でなければ配列の位置と噛み合わない。"""
    return tuple(StationRow(station_id=one, idx=index) for index, one in enumerate(station_ids))


def snapshots(
    at: datetime,
    rows: Sequence[tuple[str, int, int, int]] = STATIONS,
    *,
    minutes: int = 400,
    step_minutes: int = GRID_MINUTES,
) -> tuple[Snapshot, ...]:
    """`at` までの観測を等間隔に並べる。**台数は動かさない**（値の再現性のため）。

    `minutes` は遡る長さ。既定は推論の 2 つの窓（直近 200 分と 1 日前）を
    まとめて覆える長さにしてある。
    """
    made: list[Snapshot] = []
    for step in range(minutes // step_minutes + 1):
        observed = at - timedelta(minutes=step * step_minutes)
        made.append(
            Snapshot(
                observed_at=observed,
                fetched_at=observed + FETCH_DELAY,
                bikes=[one[1] for one in rows],
                docks=[one[2] for one in rows],
                flags=[one[3] for one in rows],
                reported_age_s=[30] * len(rows),
            )
        )
    return tuple(sorted(made, key=lambda one: one.observed_at))


def fill(
    start: datetime,
    end: datetime,
    rows: Sequence[tuple[str, int, int, int]] = STATIONS,
) -> tuple[Snapshot, ...]:
    """半開区間 `[start, end)` を等間隔の観測で埋める。**本物の `list_snapshots` と同じ形。**"""
    minutes = int((end - start).total_seconds() // 60)
    return within(snapshots(end, rows, minutes=minutes + GRID_MINUTES), start, end)


def within(made: Sequence[Snapshot], start: datetime, end: datetime) -> tuple[Snapshot, ...]:
    """半開区間で絞る。**本物の `list_snapshots` と同じ切り方。**"""
    return tuple(one for one in made if start <= one.observed_at < end)


def _system(system_id: str, rows: Sequence[tuple[str, int, int, int]]) -> SystemReference:
    """参照データ 1 システムぶん。**近傍は同一システム内で総当たり**にする。"""
    station_ids = [one[0] for one in rows]
    return SystemReference(
        system_id=system_id,
        geo=tuple(
            StationGeoRow(
                station_id=one,
                first_seen_at=datetime(2026, 8, 1, tzinfo=UTC),
                pref_code=13,
                muni_code=13101,
            )
            for one in station_ids
        ),
        attributes=tuple(
            StationAttributeRow(
                station_id=one,
                lat=35.68 + index * 0.001,
                lon=139.76,
                capacity=9,
                is_charging_station=False,
                region_id=None,
            )
            for index, one in enumerate(station_ids)
        ),
        neighbors=tuple(
            NeighborRow(
                station_id=one,
                nb_system_id=system_id,
                nb_station_id=other,
                distance_m=200,
                same_system=True,
            )
            for one in station_ids
            for other in station_ids
            if one != other
        ),
    )


def reference_files(
    day: date, rows: Sequence[tuple[str, int, int, int]] = STATIONS
) -> Mapping[str, bytes]:
    """**前日の参照スナップショット**（Storage に置かれている 2 本）。"""
    systems = tuple(_system(system_id, rows) for system_id in SYSTEMS)
    built_at = datetime(2026, 9, 9, 20, 0, tzinfo=UTC)
    daily_max = {(system_id, one[0]): one[1] + one[2] for system_id in SYSTEMS for one in rows}
    stations = to_stations_table(systems, daily_max, (), built_at=built_at)
    neighbors = to_neighbors_table(systems, built_at=built_at)
    return {
        reference_path(day, STATIONS_NAME): to_parquet_bytes(stations),
        reference_path(day, NEIGHBORS_NAME): to_parquet_bytes(neighbors),
    }


def weather_rows(at: datetime, hours: int = 4) -> tuple[WeatherRow, ...]:
    """`at` の手前 `hours` 時間ぶんの発行。**値は発行時刻から読める形**にしてある。

    `precip_mm[k] = 発行の時 / 10 + k` など（ゴールデンのフィクスチャと同じ作り）。
    どの発行のどの時間帯を引いたかが、値を見れば分かる。
    """
    top = at.replace(minute=0, second=0, microsecond=0)
    made: list[WeatherRow] = []
    for back in range(hours):
        issued = top - timedelta(hours=back)
        made.append(
            WeatherRow(
                cell_lat_idx=WEATHER_CELL[0],
                cell_lon_idx=WEATHER_CELL[1],
                issued_hour=issued,
                available_at=issued + WEATHER_DELAY,
                values=_weather_values(issued.hour),
            )
        )
    return tuple(sorted(made, key=lambda one: one.available_at))


def _weather_values(hour: int) -> Mapping[str, list[float | None]]:
    leads = range(WEATHER_LEAD_HOURS)
    made: dict[str, list[float | None]] = {
        "precip_mm": [round(hour / 10 + k, 2) for k in leads],
        "temp_c": [round(20 + hour / 100 + k * 0.5, 2) for k in leads],
        "wind_kmh": [round(5 + hour / 100 + k, 2) for k in leads],
    }
    return {name: made[name] for name in WEATHER_SERIES}


def weather_within(
    made: Sequence[WeatherRow], start: datetime, end: datetime
) -> tuple[WeatherRow, ...]:
    """半開区間で絞る。**本物の `list_weather` と同じ切り方。**"""
    return tuple(one for one in made if start <= one.available_at < end)


def day_of(path: str) -> date:
    """`reference/date=YYYY-MM-DD/...` から日付を取り出す。"""
    for part in path.split("/"):
        if part.startswith("date="):
            return date.fromisoformat(part.removeprefix("date="))
    raise ValueError(f"参照スナップショットのパスではありません: {path}")


def empty_table() -> pa.Table:
    return SNAPSHOT_SCHEMA.empty_table()
