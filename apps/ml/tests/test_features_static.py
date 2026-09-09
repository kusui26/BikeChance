"""ポートの静的な特徴量（`features/static.py`、開発プラン §6.3）。

**この 1 ファイルの主題は 2 つ。**

  * **台帳の順が位置を決める**（属性が無いポートでも行を落とさない）
  * **ドコモの容量は推定。しかし未来を覗かない**（累積最大）
"""

from datetime import UTC, datetime, timedelta

import numpy as np
import pyarrow as pa

from bikechance_ml.features.asof import Observations, to_observations
from bikechance_ml.features.reference import (
    StationAttributeRow,
    StationGeoRow,
    SystemReference,
)
from bikechance_ml.features.static import (
    NO_CODE,
    declared_capacity_grid,
    fill_ratio,
    gap,
    station_age_days,
    to_facts,
)
from bikechance_ml.jobs.snapshot_table import SCHEMA

BASE = datetime(2026, 9, 7, tzinfo=UTC)


def system(system_id: str, station_ids: list[str], with_attributes: list[str]) -> SystemReference:
    return SystemReference(
        system_id=system_id,
        geo=tuple(StationGeoRow(one, BASE, 13, 13101) for one in station_ids),
        attributes=tuple(
            StationAttributeRow(one, 35.0, 139.0, 9, True, None) for one in with_attributes
        ),
        neighbors=(),
    )


def test_stations_are_ordered_by_system_then_id() -> None:
    """**位置は決定的**でなければならない（近傍の行列がこの順に並ぶ）。"""
    facts = to_facts(
        [system("hellocycling", ["b", "a"], ["a", "b"]), system("docomo-cycle", ["z"], ["z"])]
    )
    assert facts.station_keys() == (
        ("hellocycling", "a"),
        ("hellocycling", "b"),
        ("docomo-cycle", "z"),
    )


def test_a_station_without_attributes_keeps_its_place() -> None:
    """`status` にしか現れないポートがある。**行を落とすと位置がずれる。**"""
    facts = to_facts([system("hellocycling", ["a", "b"], ["a"])])
    assert len(facts) == 2
    assert np.isnan(facts.lat[1])
    assert facts.declared_capacity[1] == NO_CODE


def test_system_quirks_become_per_station_masks() -> None:
    """`gap` と `reported_age_s` は HELLO でしか意味を持たない（データ辞書 §11）。"""
    facts = to_facts([system("hellocycling", ["a"], ["a"]), system("docomo-cycle", ["z"], ["z"])])
    assert facts.has_gap.tolist() == [True, False]
    assert facts.has_reported_age.tolist() == [True, False]
    assert facts.has_dynamic_capacity.tolist() == [False, True]


def test_missing_codes_become_the_sentinel() -> None:
    facts = to_facts(
        [
            SystemReference(
                "docomo-cycle",
                (StationGeoRow("z", BASE, None, None),),
                (StationAttributeRow("z", None, None, None, None, 3),),
                (),
            )
        ]
    )
    assert facts.pref_code.tolist() == [NO_CODE]
    assert facts.muni_code.tolist() == [NO_CODE]
    assert facts.region_id.tolist() == [3]
    assert facts.is_charging_station.tolist() == [NO_CODE]


def test_station_age_grows_with_the_base_time() -> None:
    first_seen = np.array([int(BASE.timestamp() * 1000)], dtype=np.int64)
    grid = [int((BASE + timedelta(days=2)).timestamp() * 1000)]
    assert station_age_days(first_seen, grid)[0, 0] == 2.0


# ── 容量 ──────────────────────────────────────────────────────
def observations_of(values: list[int]) -> Observations:
    stamps = [BASE + timedelta(minutes=index) for index in range(len(values))]
    table = pa.table(
        {
            "system_id": pa.array(["docomo-cycle"] * len(values), type=pa.string()),
            "station_id": pa.array(["z"] * len(values), type=pa.string()),
            "observed_at": pa.array(stamps, type=SCHEMA.field("observed_at").type),
            "fetched_at": pa.array(stamps, type=SCHEMA.field("fetched_at").type),
            "bikes": pa.array(values, type=pa.int16()),
            "docks": pa.array([1] * len(values), type=pa.int16()),
            "flags": pa.array([7] * len(values), type=pa.int16()),
            "reported_age_s": pa.array([0] * len(values), type=pa.int16()),
        },
        schema=SCHEMA,
    )
    return to_observations(table, (("docomo-cycle", "z"),))


def test_capacity_est_comes_from_the_reference_snapshot() -> None:
    """**動的な容量は前日までの 7 日の推定値**（W3 プラン §14.2 の 2）。

    以前はビルド窓の累積最大だった。窓の長さで値が変わるのをやめ、
    参照スナップショットの `capacity_est` を使う（§13.3）。
    """
    facts = to_facts([system("docomo-cycle", ["z"], ["z"])], {("docomo-cycle", "z"): 14})
    assert facts.capacity_est.tolist() == [14]


def test_a_port_missing_from_the_snapshot_has_no_estimate() -> None:
    """**0 で埋めない。** 当日現れたポートは前日の版に無く、容量は「無い」。"""
    facts = to_facts([system("docomo-cycle", ["z"], ["z"])], {})
    assert facts.capacity_est.tolist() == [NO_CODE]


def test_declared_capacity_is_spread_over_the_grid() -> None:
    facts = to_facts([system("hellocycling", ["a"], ["a"])])
    assert declared_capacity_grid(facts, 3).tolist() == [[9, 9, 9]]


# ── 導出 ──────────────────────────────────────────────────────
def test_fill_ratio_is_null_without_a_capacity() -> None:
    """**0 で埋めない。** 容量が分からないことと満車は別。"""
    bikes = np.array([3, 3], dtype=np.int32)
    capacity = np.array([9, NO_CODE], dtype=np.int32)
    ratio = fill_ratio(bikes, capacity)
    assert abs(float(ratio[0]) - 1 / 3) < 1e-6
    assert np.isnan(ratio[1])


def test_gap_can_be_negative() -> None:
    """**非負を仮定しない**（開発プラン §6.3、実測 0.038%）。"""
    values = gap(
        np.array([9, 9], dtype=np.int32),
        np.array([5, 9], dtype=np.int32),
        np.array([2, 3], dtype=np.int32),
    )
    assert values.tolist() == [2.0, -3.0]


def test_gap_is_null_without_a_capacity() -> None:
    values = gap(
        np.array([NO_CODE], dtype=np.int32),
        np.array([5], dtype=np.int32),
        np.array([2], dtype=np.int32),
    )
    assert np.isnan(values[0])
