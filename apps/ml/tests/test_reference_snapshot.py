"""日次の参照スナップショット（`features/reference_snapshot.py`、W3 プラン §14.3）。

**この 1 ファイルの主題は 3 つ。**

  * 書いて読むと `SystemReference` に**そのまま戻る**（学習と推論の共通の入口）
  * `capacity_est` は**直近 7 版の最大**で、寄与した日数を隠さない
  * 観測されていない値（`-1`）を容量として数えない
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime

import pyarrow as pa
import pytest

from bikechance_ml.features.reference import (
    NeighborRow,
    StationAttributeRow,
    StationGeoRow,
    SystemReference,
)
from bikechance_ml.features.reference_snapshot import (
    CAPACITY_DAYS,
    REFERENCE_SET,
    STATIONS_SCHEMA,
    SnapshotShapeError,
    capacity_estimates,
    capacity_rows,
    daily_capacity_max,
    to_daily_max,
    to_neighbors_table,
    to_reference,
    to_stations_table,
)
from bikechance_ml.jobs.snapshot_table import SCHEMA as SNAPSHOT_SCHEMA

BUILT_AT = datetime(2026, 9, 9, 20, 0, tzinfo=UTC)
SEEN = datetime(2026, 9, 6, 11, 0, tzinfo=UTC)


def geo(station_id: str, *, pref: int | None = 13, muni: int | None = 13101) -> StationGeoRow:
    return StationGeoRow(station_id=station_id, first_seen_at=SEEN, pref_code=pref, muni_code=muni)


def attribute(station_id: str, *, lat: float | None = 35.0) -> StationAttributeRow:
    return StationAttributeRow(
        station_id=station_id,
        lat=lat,
        lon=139.0,
        capacity=12,
        is_charging_station=False,
        region_id=None,
    )


def hello(
    *,
    geos: tuple[StationGeoRow, ...],
    attributes: tuple[StationAttributeRow, ...] = (),
    neighbors: tuple[NeighborRow, ...] = (),
) -> SystemReference:
    return SystemReference(
        system_id="hellocycling", geo=geos, attributes=attributes, neighbors=neighbors
    )


def snapshot_rows(rows: list[tuple[str, str, int, int]]) -> pa.Table:
    """`(system_id, station_id, bikes, docks)` を長い形の表にする。"""
    at = datetime(2026, 9, 8, 3, 0, tzinfo=UTC)
    return pa.Table.from_pylist(
        [
            {
                "system_id": system_id,
                "station_id": station_id,
                "observed_at": at,
                "fetched_at": at,
                "bikes": bikes,
                "docks": docks,
                "flags": 7,
                "reported_age_s": 0,
            }
            for system_id, station_id, bikes, docks in rows
        ],
        schema=SNAPSHOT_SCHEMA,
    )


# ── 往復 ──────────────────────────────────────────────────────
def test_a_snapshot_round_trips_to_the_same_reference() -> None:
    """**学習と推論の共通の入口。** 書いて読むと元の `SystemReference` に戻る。"""
    source = hello(
        geos=(geo("a"), geo("b")),
        attributes=(attribute("a"), attribute("b")),
        neighbors=(
            NeighborRow(
                station_id="a",
                nb_system_id="docomo-cycle",
                nb_station_id="z",
                distance_m=91,
                same_system=False,
            ),
        ),
    )
    stations = to_stations_table([source], {}, [], built_at=BUILT_AT)
    neighbors = to_neighbors_table([source], built_at=BUILT_AT)
    assert to_reference(stations, neighbors) == (source,)


def test_neighbors_keep_the_other_system() -> None:
    """**近傍はシステムを跨ぐ**（§4.3 の 22）。相手のシステムを落とさない。"""
    link = NeighborRow(
        station_id="a",
        nb_system_id="docomo-cycle",
        nb_station_id="z",
        distance_m=91,
        same_system=False,
    )
    source = hello(geos=(geo("a"),), neighbors=(link,))
    restored = to_reference(
        to_stations_table([source], {}, [], built_at=BUILT_AT),
        to_neighbors_table([source], built_at=BUILT_AT),
    )
    assert restored[0].neighbors == (link,)


def test_a_port_without_attributes_stays_without_attributes() -> None:
    """**台帳にあって属性が無いポートがある**（実測でドコモに数件）。

    全 null の属性行をでっち上げると、`lat` が無いポートと区別が付かなくなる。
    """
    source = hello(geos=(geo("a"), geo("b")), attributes=(attribute("a"),))
    restored = to_reference(
        to_stations_table([source], {}, [], built_at=BUILT_AT),
        to_neighbors_table([source], built_at=BUILT_AT),
    )
    assert len(restored[0].geo) == 2
    assert tuple(one.station_id for one in restored[0].attributes) == ("a",)


def test_the_reference_set_is_recorded_in_the_metadata() -> None:
    """**版を列にしない。** 20 万行に同じ値を並べる必要は無い。"""
    stations = to_stations_table([hello(geos=(geo("a"),))], {}, [], built_at=BUILT_AT)
    assert stations.schema.metadata[b"reference_set"] == REFERENCE_SET.encode()
    assert "reference_set" not in stations.schema.names


def test_a_table_with_the_wrong_columns_is_refused() -> None:
    """**静かに null で埋めない**（§12 の 97 と同じ理由）。"""
    wrong = pa.table({"system_id": ["hellocycling"]})
    with pytest.raises(SnapshotShapeError):
        to_reference(wrong, wrong)


# ── その日の最大 ──────────────────────────────────────────────
def test_daily_max_is_the_largest_total_of_the_day() -> None:
    table = snapshot_rows([("hellocycling", "a", 3, 5), ("hellocycling", "a", 2, 9)])
    assert daily_capacity_max(table) == {("hellocycling", "a"): 11}


def test_unobserved_rows_are_not_counted_as_capacity() -> None:
    """`-1` は「観測されていない」。**足すと 0 台のポートに見える。**"""
    table = snapshot_rows([("hellocycling", "a", -1, -1), ("hellocycling", "a", 4, 4)])
    assert daily_capacity_max(table) == {("hellocycling", "a"): 8}


def test_a_port_never_observed_has_no_entry() -> None:
    """**鍵ごと出さない。** `0` を入れると「容量 0 のポート」になる。"""
    table = snapshot_rows([("hellocycling", "a", -1, -1)])
    assert daily_capacity_max(table) == {}


def test_the_two_systems_do_not_collide() -> None:
    """`station_id` はシステムを跨いで衝突する（実測 2,608 件）。"""
    table = snapshot_rows([("hellocycling", "1", 3, 3), ("docomo-cycle", "1", 9, 9)])
    assert daily_capacity_max(table) == {("hellocycling", "1"): 6, ("docomo-cycle", "1"): 18}


def test_an_empty_day_gives_no_estimate() -> None:
    assert daily_capacity_max(SNAPSHOT_SCHEMA.empty_table()) == {}


# ── 直近 7 版の最大 ───────────────────────────────────────────
def _stations_with(today: int | None, previous: Sequence[int | None]) -> pa.Table:
    key = ("hellocycling", "a")
    return to_stations_table(
        [hello(geos=(geo("a"),))],
        {} if today is None else {key: today},
        [{} if one is None else {key: one} for one in previous],
        built_at=BUILT_AT,
    )


def test_capacity_est_is_the_max_over_the_kept_days() -> None:
    stations = _stations_with(8, [12, 5])
    assert capacity_estimates(stations) == {("hellocycling", "a"): 12}
    assert stations.column("capacity_days").to_pylist() == [3]
    assert stations.column("capacity_daily_max").to_pylist() == [8]


def test_capacity_rows_carry_the_day_and_skip_the_unknown() -> None:
    """DB に写す行（migration 0045）。**`/v1` はこの経路でしか大きさを読めない。**"""
    rows = capacity_rows(_stations_with(8, [12, 5]), date(2026, 9, 12))
    assert rows == (
        {
            "system_id": "hellocycling",
            "station_id": "a",
            "capacity_est": 12,
            "capacity_days": 3,
            "as_of_date": "2026-09-12",
        },
    )


def test_capacity_rows_keep_zero_but_drop_none() -> None:
    """**0 と「分からない」を分ける**（W5 プラン §12 の 152、migration 0047）。

    0 は「7 日とも 1 台も 1 枠も並ばなかった」という観測である（実測 37 件）。
    1 度も観測できなかったポートだけが `capacity_est` を持たず、そこは行ごと作らない。
    """
    assert capacity_rows(_stations_with(0, [0]), date(2026, 9, 12))[0]["capacity_est"] == 0
    assert capacity_rows(_stations_with(None, [None]), date(2026, 9, 12)) == ()


def test_capacity_rows_refuse_a_table_with_other_columns() -> None:
    """**ファイル自身の列**を確かめる（§12 の 97 と同じ理由）。"""
    with pytest.raises(SnapshotShapeError):
        capacity_rows(_stations_with(8, []).drop_columns(["capacity_days"]), date(2026, 9, 12))


def test_days_without_a_value_are_not_counted() -> None:
    """**足りないことを隠さない。** 観測が無い日は日数に入れない。"""
    stations = _stations_with(8, [None, 5, None])
    assert stations.column("capacity_days").to_pylist() == [2]


def test_more_versions_than_the_window_are_ignored() -> None:
    """8 版あっても使うのは 7 版まで。**8 版目の大きい値は入らない。**"""
    stations = _stations_with(1, [1] * (CAPACITY_DAYS - 1) + [999])
    assert capacity_estimates(stations) == {("hellocycling", "a"): 1}
    assert stations.column("capacity_days").to_pylist() == [CAPACITY_DAYS]


def test_a_port_with_no_observation_at_all_has_no_estimate() -> None:
    stations = _stations_with(None, [])
    assert capacity_estimates(stations) == {}
    assert stations.column("capacity_days").to_pylist() == [0]


def test_a_written_version_can_be_folded_into_the_next_one() -> None:
    """**持ち回りの要。** 前の版から `capacity_daily_max` を読み直せる。"""
    stations = _stations_with(8, [])
    assert to_daily_max(stations) == {("hellocycling", "a"): 8}


def test_folding_skips_ports_without_a_daily_max() -> None:
    assert to_daily_max(_stations_with(None, [])) == {}


def test_folding_refuses_a_table_with_the_wrong_columns() -> None:
    with pytest.raises(SnapshotShapeError):
        to_daily_max(pa.table({"system_id": ["hellocycling"]}))


def test_the_schema_is_the_contract() -> None:
    """列を足したら **`REFERENCE_SET` を上げる**。ここが気づかせる。"""
    assert STATIONS_SCHEMA.names == [
        "system_id",
        "station_id",
        "first_seen_at",
        "pref_code",
        "muni_code",
        "has_attributes",
        "lat",
        "lon",
        "capacity",
        "is_charging_station",
        "region_id",
        "capacity_daily_max",
        "capacity_est",
        "capacity_days",
    ]
    assert REFERENCE_SET == "r0"
