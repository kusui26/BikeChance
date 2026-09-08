"""as-of 結合（`features/asof.py`、W3-13）。

**この 1 ファイルの主題は「本番より賢く学習しないこと」。** 特徴量は `fetched_at`、
ラベルは `observed_at` で切る。取り違えると train/serve skew が入る。
"""

from datetime import UTC, datetime, timedelta

import numpy as np
import pyarrow as pa

from bikechance_ml.features.asof import (
    NO_ROW,
    as_of_feature,
    as_of_label,
    gather,
    to_observations,
)
from bikechance_ml.jobs.snapshot_table import SCHEMA

BASE = datetime(2026, 9, 7, 0, 0, tzinfo=UTC)


def stamp(minutes: float) -> datetime:
    return BASE + timedelta(minutes=minutes)


def ms(minutes: float) -> int:
    return int(stamp(minutes).timestamp() * 1000)


def table(rows: list[tuple[str, str, float, float, int]]) -> pa.Table:
    """`(system_id, station_id, 観測分, 取り込み分, 台数)` から表を作る。"""
    return pa.table(
        {
            "system_id": pa.array([row[0] for row in rows], type=pa.string()),
            "station_id": pa.array([row[1] for row in rows], type=pa.string()),
            "observed_at": pa.array(
                [stamp(row[2]) for row in rows], type=SCHEMA.field("observed_at").type
            ),
            "fetched_at": pa.array(
                [stamp(row[3]) for row in rows], type=SCHEMA.field("fetched_at").type
            ),
            "bikes": pa.array([row[4] for row in rows], type=pa.int16()),
            "docks": pa.array([9 - row[4] for row in rows], type=pa.int16()),
            "flags": pa.array([7] * len(rows), type=pa.int16()),
            "reported_age_s": pa.array([30] * len(rows), type=pa.int16()),
        },
        schema=SCHEMA,
    )


KEYS = (("hellocycling", "a"), ("hellocycling", "b"))


# ── 並べ替えと区間 ────────────────────────────────────────────
def test_rows_are_grouped_by_station_in_ledger_order() -> None:
    """**位置は台帳が決める。** 表の並び順ではない。"""
    observations = to_observations(
        table([("hellocycling", "b", 0, 0, 1), ("hellocycling", "a", 0, 0, 2)]), KEYS
    )
    assert observations.n_stations == 2
    assert observations.bikes[observations.rows_of(0)].tolist() == [2]
    assert observations.bikes[observations.rows_of(1)].tolist() == [1]


def test_station_absent_from_the_feed_gets_an_empty_range() -> None:
    """行が 1 つも無いポートも**位置を持つ**（近傍の行列がずれないため）。"""
    observations = to_observations(table([("hellocycling", "a", 0, 0, 2)]), KEYS)
    assert observations.rows_of(1).start == observations.rows_of(1).stop


def test_rows_outside_the_ledger_are_dropped() -> None:
    """台帳に無いポートの行は捨てる。**位置が無いので置き場所が無い。**"""
    observations = to_observations(
        table([("hellocycling", "a", 0, 0, 2), ("hellocycling", "zz", 0, 0, 5)]), KEYS
    )
    assert observations.n_rows == 1


def test_the_same_station_id_in_two_systems_is_two_ports() -> None:
    """**ID はシステムを跨いで衝突する**（実測 2,608 件）。キーは組で持つ。"""
    keys = (("hellocycling", "1"), ("docomo-cycle", "1"))
    observations = to_observations(
        table([("hellocycling", "1", 0, 0, 4), ("docomo-cycle", "1", 0, 0, 8)]), keys
    )
    assert observations.bikes[observations.rows_of(0)].tolist() == [4]
    assert observations.bikes[observations.rows_of(1)].tolist() == [8]


# ── 特徴量の as-of（fetched_at で切る）──────────────────────────
def test_feature_asof_uses_fetched_at_not_observed_at() -> None:
    """**まだ取り込んでいない観測は使えない。**

    10 分の観測は 12 分に取り込まれる。11 分の基準時刻から見えるのは 5 分の観測。
    """
    observations = to_observations(
        table(
            [
                ("hellocycling", "a", 5, 6, 3),
                ("hellocycling", "a", 10, 12, 7),
            ]
        ),
        KEYS,
    )
    rows = as_of_feature(observations, [ms(11), ms(13)])
    assert observations.bikes[rows[0, 0]] == 3
    assert observations.bikes[rows[0, 1]] == 7


def test_feature_asof_is_none_before_the_first_fetch() -> None:
    observations = to_observations(table([("hellocycling", "a", 5, 6, 3)]), KEYS)
    rows = as_of_feature(observations, [ms(0), ms(6)])
    assert rows[0, 0] == NO_ROW
    assert rows[0, 1] != NO_ROW


def test_feature_asof_takes_the_newest_observation_not_the_newest_fetch() -> None:
    """**取り込みの順と観測の順は一致しない。**

    バックアップ収集器や再構築が、後から古い観測を足すことがある。`fetched_at` が
    t 以下のうち `observed_at` が最大のものを採るので、後から入った古い観測に
    引きずられない。
    """
    observations = to_observations(
        table(
            [
                ("hellocycling", "a", 10, 11, 7),
                ("hellocycling", "a", 5, 20, 3),
            ]
        ),
        KEYS,
    )
    rows = as_of_feature(observations, [ms(25)])
    assert observations.bikes[rows[0, 0]] == 7


# ── ラベルの as-of（observed_at で切る）─────────────────────────
def test_label_asof_ignores_fetched_at() -> None:
    """未来の話なので「入手できたか」は問わない（W3 プラン §9.3）。"""
    observations = to_observations(table([("hellocycling", "a", 10, 99, 7)]), KEYS)
    rows = as_of_label(observations, [ms(11)])
    assert observations.bikes[rows[0, 0]] == 7


def test_label_asof_does_not_look_into_the_future() -> None:
    observations = to_observations(table([("hellocycling", "a", 10, 10, 7)]), KEYS)
    rows = as_of_label(observations, [ms(9), ms(10)])
    assert rows[0, 0] == NO_ROW
    assert rows[0, 1] != NO_ROW


# ── 値の取り出し ──────────────────────────────────────────────
def test_gather_puts_the_sentinel_where_there_is_no_row() -> None:
    """`-1` が「最後の行」を指す事故を起こさない（Python の負の添字）。"""
    values = np.array([10, 20, 30], dtype=np.int16)
    index = np.array([[2, NO_ROW, 0]], dtype=np.int32)
    assert gather(values, index, -1).tolist() == [[30, -1, 10]]


def test_gather_keeps_the_element_type() -> None:
    values = np.array([1.5, 2.5], dtype=np.float32)
    taken = gather(values, np.array([[NO_ROW, 1]], dtype=np.int32), np.nan)
    assert taken.dtype == np.float32
    assert np.isnan(taken[0, 0])
    assert taken[0, 1] == 2.5
