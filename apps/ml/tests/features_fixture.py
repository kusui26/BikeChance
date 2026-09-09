"""ゴールデンテストの固定フィクスチャ（W3 プラン §5.8）。

**入力も出力もファイルに置く。** 入力は `fixtures/features_golden/snapshots.csv` と
`reference.json`、期待値は `expected.csv`。どちらも人が読める形にしてあるので、
規則を変えたときに**差分がレビューできる**（`gen_golden.py` で作り直す）。

作った場面（2026-09-07 は月曜。祝日ではない）:

| ポート | システム | 仕込み |
|---|---|---|
| `p1` | HELLO | 素直に動く。`p2` と 80 m、ドコモの `d1` と 250 m |
| `p2` | HELLO | 途中で**運用停止**（`flags = 1`）し、途中で**観測が飛ぶ**（`bikes = -1`） |
| `d1` | ドコモ | 81 秒周期。**容量は動的**（`bikes + docks`）で `gap` を持たない |
| `5753` | ドコモ | **実在しないポート**。1 行も出てはいけない |

`fetched_at` は `observed_at + 40 秒`。**5 分グリッドの各点で as-of が 1 つ前の観測を
指す**ので、`fetched_at` で切っていることがそのまま出力に現れる（W3-13）。
"""

import csv
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final

import pyarrow as pa

from bikechance_ml.features import build, neighbors, static
from bikechance_ml.features.grid import JST
from bikechance_ml.features.reference import (
    NeighborRow,
    StationAttributeRow,
    StationGeoRow,
    SystemReference,
)
from bikechance_ml.jobs.snapshot_table import SCHEMA as SNAPSHOT_SCHEMA

DIRECTORY: Final[Path] = Path(__file__).resolve().parent / "fixtures" / "features_golden"
SNAPSHOTS: Final[Path] = DIRECTORY / "snapshots.csv"
REFERENCE: Final[Path] = DIRECTORY / "reference.json"
EXPECTED: Final[Path] = DIRECTORY / "expected.csv"

#: 基準日（JST）。2026-09-07 は月曜。
DAY: Final[date] = date(2026, 9, 7)

#: 観測の範囲（JST）。当日の朝から夕方までと、前日の同時刻を少しだけ。
FROM: Final[datetime] = datetime(2026, 9, 7, 8, 0, tzinfo=JST)
TO: Final[datetime] = datetime(2026, 9, 7, 14, 0, tzinfo=JST)

#: 取り込みの遅れ。**5 分周期と同じ長さにしない**（as-of が退化する）。
FETCH_DELAY: Final[timedelta] = timedelta(seconds=40)


def load_snapshots() -> pa.Table:
    """`snapshots.csv` を Parquet と同じ形の表にする。"""
    lines = [
        line
        for line in SNAPSHOTS.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    rows = list(csv.DictReader(lines))
    columns = {
        "system_id": pa.array([row["system_id"] for row in rows], type=pa.string()),
        "station_id": pa.array([row["station_id"] for row in rows], type=pa.string()),
        "observed_at": _stamps(rows, "observed_at"),
        "fetched_at": _stamps(rows, "fetched_at"),
        "bikes": _numbers(rows, "bikes"),
        "docks": _numbers(rows, "docks"),
        "flags": _numbers(rows, "flags"),
        "reported_age_s": _numbers(rows, "reported_age_s"),
    }
    return pa.table(columns, schema=SNAPSHOT_SCHEMA)


def _stamps(rows: list[dict[str, str]], name: str) -> pa.Array:
    values = [datetime.fromisoformat(row[name]).astimezone(UTC) for row in rows]
    return pa.array(values, type=SNAPSHOT_SCHEMA.field(name).type)


def _numbers(rows: list[dict[str, str]], name: str) -> pa.Array:
    return pa.array([int(row[name]) for row in rows], type=pa.int16())


def load_reference() -> tuple[
    tuple[SystemReference, ...], dict[tuple[str, str], int], frozenset[date]
]:
    """`reference.json` を参照データに直す。**`capacity_est` も一緒に返す。**"""
    document = json.loads(REFERENCE.read_text(encoding="utf-8"))
    systems = tuple(_to_system(one) for one in document["systems"])
    holidays = frozenset(date.fromisoformat(one) for one in document["holidays"])
    estimates = {
        (str(system["system_id"]), str(station["station_id"])): int(str(station["capacity_est"]))
        for system in document["systems"]
        for station in system["stations"]
    }
    return systems, estimates, holidays


def _to_system(document: dict[str, object]) -> SystemReference:
    system_id = str(document["system_id"])
    return SystemReference(
        system_id=system_id,
        geo=tuple(_to_geo(one) for one in _rows(document, "stations")),
        attributes=tuple(_to_attribute(one) for one in _rows(document, "attributes")),
        neighbors=tuple(_to_neighbor(one) for one in _rows(document, "neighbors")),
    )


def _rows(document: dict[str, object], name: str) -> list[dict[str, object]]:
    value = document[name]
    if not isinstance(value, list):
        raise TypeError(f"{name}: 配列を期待した")
    return [one for one in value if isinstance(one, dict)]


def _to_geo(row: dict[str, object]) -> StationGeoRow:
    return StationGeoRow(
        station_id=str(row["station_id"]),
        first_seen_at=datetime.fromisoformat(str(row["first_seen_at"])),
        pref_code=_optional_int(row.get("pref_code")),
        muni_code=_optional_int(row.get("muni_code")),
    )


def _to_attribute(row: dict[str, object]) -> StationAttributeRow:
    charging = row.get("is_charging_station")
    return StationAttributeRow(
        station_id=str(row["station_id"]),
        lat=_optional_float(row.get("lat")),
        lon=_optional_float(row.get("lon")),
        capacity=_optional_int(row.get("capacity")),
        is_charging_station=None if charging is None else bool(charging),
        region_id=_optional_int(row.get("region_id")),
    )


def _to_neighbor(row: dict[str, object]) -> NeighborRow:
    return NeighborRow(
        station_id=str(row["station_id"]),
        nb_system_id=str(row["nb_system_id"]),
        nb_station_id=str(row["nb_station_id"]),
        distance_m=int(str(row["distance_m"])),
        same_system=bool(row["same_system"]),
    )


def _optional_int(value: object) -> int | None:
    return None if value is None else int(str(value))


def _optional_float(value: object) -> float | None:
    return None if value is None else float(str(value))


def build_inputs() -> build.DayInputs:
    """フィクスチャから組み立ての入力を作る。**テストと生成器で同じ道を通す。**"""
    systems, estimates, holidays = load_reference()
    facts = static.to_facts(systems, estimates)
    links = neighbors.to_links(systems, facts.station_keys())
    return build.DayInputs(
        day=DAY,
        reference=build.Reference(facts=facts, links=links, holidays=holidays),
        table=load_snapshots(),
    )
