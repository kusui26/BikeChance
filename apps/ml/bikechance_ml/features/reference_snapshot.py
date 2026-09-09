"""日次の参照スナップショット（W3 プラン §14.3）。

**参照データを日付で固定する。** いまは `stations` / `station_attributes` /
`station_neighbors` の「現在の値」を、学習も推論もその場で読んでいる。そのため
過去の日を作り直すと値が変わり（§13.2）、ドコモの `capacity` はビルド窓の長さに
依存する（§13.3）。日次で 1 組の Parquet に固めれば、どちらも閉じる。

**読む規則は学習も推論も同じにする**：「基準時刻の**前日**の版を読む」。
「学習は前日、推論は最新」にすると、そこが新しい train/serve skew になる。

**この表は `SystemReference` を往復できる形にしてある。** 列を足したり意味を変えたり
したら `REFERENCE_SET` を上げる（`FEATURE_SET` と同じ作法）。

`is_active` は**入れない**。`list_station_geo` が読まないのと同じ理由で、活性なポート
だけを残すと生存者バイアスになる。`idx`（台帳の位置）も入れない。**`SystemReference`
が持たないものは持たせない。**
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Final

import pyarrow as pa
import pyarrow.compute as pc

from bikechance_ml.features.constants import MISSING
from bikechance_ml.features.reference import (
    NeighborRow,
    StationAttributeRow,
    StationGeoRow,
    SystemReference,
)

#: 参照スナップショットの形の版。**列や意味を変えたら上げる。**
REFERENCE_SET: Final[str] = "r0"

#: `capacity_est` を作る日数（開発プラン §3 の `capacity_est = 過去 7 日の max`）。
CAPACITY_DAYS: Final[int] = 7

#: ポート 1 行。`stations`（台帳）を主にして、属性は左外部結合で並べる。
#: **属性が無いポートがある**ので `has_attributes` で区別する（全 null と区別が付かない）。
STATIONS_SCHEMA: Final[pa.Schema] = pa.schema(
    [
        pa.field("system_id", pa.string(), nullable=False),
        pa.field("station_id", pa.string(), nullable=False),
        pa.field("first_seen_at", pa.timestamp("ms", tz="UTC"), nullable=False),
        pa.field("pref_code", pa.int32(), nullable=True),
        pa.field("muni_code", pa.int32(), nullable=True),
        pa.field("has_attributes", pa.bool_(), nullable=False),
        pa.field("lat", pa.float64(), nullable=True),
        pa.field("lon", pa.float64(), nullable=True),
        pa.field("capacity", pa.int32(), nullable=True),
        pa.field("is_charging_station", pa.bool_(), nullable=True),
        pa.field("region_id", pa.int32(), nullable=True),
        # その日の max(bikes + docks)。観測が無ければ null
        pa.field("capacity_daily_max", pa.int32(), nullable=True),
        # 直近 CAPACITY_DAYS 版の max。**ビルド窓に依存しない**
        pa.field("capacity_est", pa.int32(), nullable=True),
        # capacity_est に寄与した日数。**足りないことを隠さない**
        pa.field("capacity_days", pa.int16(), nullable=False),
    ]
)

#: 近傍 1 ペア（片方向）。`NeighborRow` と同じ形にする。
NEIGHBORS_SCHEMA: Final[pa.Schema] = pa.schema(
    [
        pa.field("system_id", pa.string(), nullable=False),
        pa.field("station_id", pa.string(), nullable=False),
        pa.field("nb_system_id", pa.string(), nullable=False),
        pa.field("nb_station_id", pa.string(), nullable=False),
        pa.field("distance_m", pa.int32(), nullable=False),
        pa.field("same_system", pa.bool_(), nullable=False),
    ]
)


class SnapshotShapeError(RuntimeError):
    """読んだ Parquet の列が想定と違う。**静かに null で埋めない。**"""


# ── 書く（純粋）──────────────────────────────────────────────
def to_stations_table(
    systems: Sequence[SystemReference],
    daily_max: Mapping[tuple[str, str], int],
    previous: Sequence[Mapping[tuple[str, str], int]],
    *,
    built_at: datetime,
) -> pa.Table:
    """台帳・属性・容量を 1 つの表にする。**台帳の順を保つ。**

    `previous` は古い版から新しい版の順でなくてよい（最大を取るだけ）。当日を含めて
    `CAPACITY_DAYS` 版までを使う。
    """
    rows: list[dict[str, object]] = []
    for system in systems:
        attributes = {one.station_id: one for one in system.attributes}
        for geo in system.geo:
            key = (system.system_id, geo.station_id)
            rows.append(
                _station_row(
                    system.system_id, geo, attributes.get(geo.station_id), key, daily_max, previous
                )
            )
    return _table(rows, STATIONS_SCHEMA, built_at=built_at)


def _station_row(
    system_id: str,
    geo: StationGeoRow,
    attribute: StationAttributeRow | None,
    key: tuple[str, str],
    daily_max: Mapping[tuple[str, str], int],
    previous: Sequence[Mapping[tuple[str, str], int]],
) -> dict[str, object]:
    today = daily_max.get(key)
    est, days = _rolling_max(key, today, previous)
    return {
        "system_id": system_id,
        "station_id": geo.station_id,
        "first_seen_at": geo.first_seen_at,
        "pref_code": geo.pref_code,
        "muni_code": geo.muni_code,
        "has_attributes": attribute is not None,
        "lat": None if attribute is None else attribute.lat,
        "lon": None if attribute is None else attribute.lon,
        "capacity": None if attribute is None else attribute.capacity,
        "is_charging_station": None if attribute is None else attribute.is_charging_station,
        "region_id": None if attribute is None else attribute.region_id,
        "capacity_daily_max": today,
        "capacity_est": est,
        "capacity_days": days,
    }


def _rolling_max(
    key: tuple[str, str],
    today: int | None,
    previous: Sequence[Mapping[tuple[str, str], int]],
) -> tuple[int | None, int]:
    """当日と前の版から `capacity_est` と寄与日数を出す。**無い日は数えない。**"""
    values = [one for one in (today, *(day.get(key) for day in previous)) if one is not None]
    kept = values[:CAPACITY_DAYS]
    return (max(kept) if kept else None, len(kept))


def to_neighbors_table(systems: Sequence[SystemReference], *, built_at: datetime) -> pa.Table:
    """近傍を 1 つの表にする。**片方向の行がそのまま入る**（両向きが別の行）。"""
    rows: list[dict[str, object]] = [
        {
            "system_id": system.system_id,
            "station_id": one.station_id,
            "nb_system_id": one.nb_system_id,
            "nb_station_id": one.nb_station_id,
            "distance_m": one.distance_m,
            "same_system": one.same_system,
        }
        for system in systems
        for one in system.neighbors
    ]
    return _table(rows, NEIGHBORS_SCHEMA, built_at=built_at)


def _table(rows: list[dict[str, object]], schema: pa.Schema, *, built_at: datetime) -> pa.Table:
    """**版を列にせず、スキーマのメタデータに入れる**（20 万行に同じ値を並べない）。"""
    table = pa.Table.from_pylist(rows, schema=schema)
    return table.replace_schema_metadata(
        {"reference_set": REFERENCE_SET, "built_at": built_at.isoformat()}
    )


# ── その日の最大（純粋）──────────────────────────────────────
def daily_capacity_max(snapshots: pa.Table) -> dict[tuple[str, str], int]:
    """`bikes + docks` の、その日の最大を `(system_id, station_id)` ごとに出す。

    **観測されていない行（`-1`）は数えない。** 1 度も観測が無いポートは鍵ごと出さない
    （`0` を入れると「容量 0 のポート」に見える）。
    """
    if snapshots.num_rows == 0:
        return {}
    observed = snapshots.filter(
        pc.and_(
            pc.not_equal(snapshots.column("bikes"), MISSING),
            pc.not_equal(snapshots.column("docks"), MISSING),
        )
    )
    if observed.num_rows == 0:
        return {}
    totals = pc.add(
        observed.column("bikes").cast(pa.int32()), observed.column("docks").cast(pa.int32())
    )
    grouped = (
        observed.append_column("total", totals)
        .group_by(["system_id", "station_id"])
        .aggregate([("total", "max")])
    )
    return {
        (str(system), str(station)): int(value)
        for system, station, value in zip(
            grouped.column("system_id").to_pylist(),
            grouped.column("station_id").to_pylist(),
            grouped.column("total_max").to_pylist(),
            strict=True,
        )
    }


def to_daily_max(stations: pa.Table) -> dict[tuple[str, str], int]:
    """既に書いた版から `capacity_daily_max` を読み直す（前の版を畳むときに使う）。"""
    _require(stations, STATIONS_SCHEMA)
    return {
        (str(system), str(station)): int(value)
        for system, station, value in zip(
            stations.column("system_id").to_pylist(),
            stations.column("station_id").to_pylist(),
            stations.column("capacity_daily_max").to_pylist(),
            strict=True,
        )
        if value is not None
    }


# ── 読む（純粋）──────────────────────────────────────────────
def to_reference(stations: pa.Table, neighbors: pa.Table) -> tuple[SystemReference, ...]:
    """スナップショットを `SystemReference` に戻す。**学習と推論の共通の入口。**"""
    _require(stations, STATIONS_SCHEMA)
    _require(neighbors, NEIGHBORS_SCHEMA)
    geo = _by_system(stations, _to_geo)
    attributes = _by_system(stations, _to_attribute, keep=lambda row: bool(row["has_attributes"]))
    links = _by_system(neighbors, _to_neighbor)
    system_ids = sorted(set(geo) | set(attributes) | set(links))
    return tuple(
        SystemReference(
            system_id=system_id,
            geo=tuple(geo.get(system_id, ())),
            attributes=tuple(attributes.get(system_id, ())),
            neighbors=tuple(links.get(system_id, ())),
        )
        for system_id in system_ids
    )


def capacity_estimates(stations: pa.Table) -> dict[tuple[str, str], int]:
    """`capacity_est` を引ける形にする。**無いポートは鍵ごと出さない。**"""
    _require(stations, STATIONS_SCHEMA)
    return {
        (str(system), str(station)): int(value)
        for system, station, value in zip(
            stations.column("system_id").to_pylist(),
            stations.column("station_id").to_pylist(),
            stations.column("capacity_est").to_pylist(),
            strict=True,
        )
        if value is not None
    }


def _require(table: pa.Table, schema: pa.Schema) -> None:
    """**ファイル自身の列**を確かめる（§12 の 97 と同じ理由）。"""
    if table.schema.names != schema.names:
        raise SnapshotShapeError(f"列が違う: 期待 {schema.names} / 実際 {table.schema.names}")


type Row = Mapping[str, object]


def _by_system[T](
    table: pa.Table,
    make: Callable[[Row], T],
    keep: Callable[[Row], bool] | None = None,
) -> dict[str, list[T]]:
    """`system_id` ごとに行を組み立てる。**表の順を保つ。**"""
    grouped: dict[str, list[T]] = {}
    for row in table.to_pylist():
        if keep is not None and not keep(row):
            continue
        grouped.setdefault(str(row["system_id"]), []).append(make(row))
    return grouped


def _to_geo(row: Mapping[str, object]) -> StationGeoRow:
    return StationGeoRow(
        station_id=str(row["station_id"]),
        first_seen_at=_as_datetime(row["first_seen_at"]),
        pref_code=_as_optional_int(row["pref_code"]),
        muni_code=_as_optional_int(row["muni_code"]),
    )


def _to_attribute(row: Mapping[str, object]) -> StationAttributeRow:
    return StationAttributeRow(
        station_id=str(row["station_id"]),
        lat=_as_optional_float(row["lat"]),
        lon=_as_optional_float(row["lon"]),
        capacity=_as_optional_int(row["capacity"]),
        is_charging_station=_as_optional_bool(row["is_charging_station"]),
        region_id=_as_optional_int(row["region_id"]),
    )


def _to_neighbor(row: Mapping[str, object]) -> NeighborRow:
    return NeighborRow(
        station_id=str(row["station_id"]),
        nb_system_id=str(row["nb_system_id"]),
        nb_station_id=str(row["nb_station_id"]),
        distance_m=int(row["distance_m"]),  # type: ignore[call-overload]
        same_system=bool(row["same_system"]),
    )


def _as_datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise SnapshotShapeError(f"時刻を期待した: {type(value).__name__}")
    return value


def _as_optional_int(value: object) -> int | None:
    return None if value is None else int(value)  # type: ignore[call-overload]


def _as_optional_float(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]


def _as_optional_bool(value: object) -> bool | None:
    return None if value is None else bool(value)
