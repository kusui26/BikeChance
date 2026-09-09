"""流量（`features/flow.py`、W3-11）。

**この 1 ファイルの主題は「5 分グリッドに畳んでから差分を取らないこと」。**
ドコモ（81 秒周期）を 5 分に畳むと流量の 13% が消える（開発プラン §6.7 の実測）。
ここでは同じことを小さく再現し、**畳んだ側が負ける**ことを固定する。
"""

from datetime import UTC, datetime, timedelta

import numpy as np
import pyarrow as pa

from bikechance_ml.features.asof import to_observations
from bikechance_ml.features.constants import CHANGE_CAP_MINUTES
from bikechance_ml.features.flow import compute_flow
from bikechance_ml.jobs.snapshot_table import SCHEMA

BASE = datetime(2026, 9, 7, 0, 0, tzinfo=UTC)
KEYS = (("hellocycling", "a"),)


def build(series: list[tuple[float, int]]) -> pa.Table:
    """`(分, 台数)` の並びから表を作る。`fetched_at` は観測と同じにする。"""
    stamps = [BASE + timedelta(minutes=minute) for minute, _ in series]
    return pa.table(
        {
            "system_id": pa.array(["hellocycling"] * len(series), type=pa.string()),
            "station_id": pa.array(["a"] * len(series), type=pa.string()),
            "observed_at": pa.array(stamps, type=SCHEMA.field("observed_at").type),
            "fetched_at": pa.array(stamps, type=SCHEMA.field("fetched_at").type),
            "bikes": pa.array([bikes for _, bikes in series], type=pa.int16()),
            "docks": pa.array([9 - bikes for _, bikes in series], type=pa.int16()),
            "flags": pa.array([7] * len(series), type=pa.int16()),
            "reported_age_s": pa.array([0] * len(series), type=pa.int16()),
        },
        schema=SCHEMA,
    )


def flow_of(series: list[tuple[float, int]]) -> tuple[int, int, int]:
    """最後の観測時点の `(貸出, 返却, 変化回数)`。"""
    observations = to_observations(build(series), KEYS)
    flow = compute_flow(observations)
    last = observations.n_rows - 1
    return int(flow.rentals[last]), int(flow.returns[last]), int(flow.n_changes[last])


# ── 本題：粒度を落とすと流量が消える ──────────────────────────
def test_native_cadence_sees_flow_that_the_grid_hides() -> None:
    """**5 分の中で往復した動きは、5 分ごとに見ると消える。**

    1 分刻みで 5 → 3 → 5 と動くと、貸出 2・返却 2・変化 2 回。同じ区間を 5 分刻みで
    しか見なければ 5 → 5 で「何も起きていない」ことになる。
    """
    native = flow_of([(0, 5), (1, 3), (2, 5), (5, 5)])
    coarse = flow_of([(0, 5), (5, 5)])
    assert native == (2, 2, 2)
    assert coarse == (0, 0, 0)


def test_rentals_and_returns_are_separated() -> None:
    """貸出は負の差の絶対値、返却は正の差。**片方に混ぜない。**"""
    assert flow_of([(0, 5), (1, 2), (2, 6)]) == (3, 4, 2)


def test_window_is_the_last_60_minutes() -> None:
    """窓の外の変化は数えない。**61 分前の動きは入らない。**"""
    assert flow_of([(0, 5), (1, 0), (62, 0)]) == (0, 0, 0)
    assert flow_of([(0, 5), (30, 0), (62, 0)]) == (5, 0, 1)


def test_unobserved_rows_are_not_treated_as_zero() -> None:
    """`-1` は「観測されなかった」。**0 台になったのではない**（データ辞書 §2 の (3)）。

    穴をまたいだ差分は「その間に起きた正味の変化」として 1 度だけ数える。**補間しない。**
    """
    assert flow_of([(0, 5), (1, -1), (2, 5)]) == (0, 0, 0)
    assert flow_of([(0, 5), (1, -1), (2, 1)]) == (4, 0, 1)


def test_minutes_since_last_change_is_the_cap_when_nothing_changed() -> None:
    """**0 で埋めない。** 「たった今変わった」と区別がつかなくなる。

    上限そのものを返す。「180 分以上動いていない」は分かっている情報である
    （W4 プラン §4 の W4-10）。
    """
    observations = to_observations(build([(0, 5), (1, 5), (2, 5)]), KEYS)
    flow = compute_flow(observations)
    assert (flow.minutes_since_last_change == CHANGE_CAP_MINUTES).all()


def test_minutes_since_last_change_is_capped() -> None:
    """**窓の長さで値が変わらないようにする**（学習 25 時間・推論 3 時間）。"""
    # 300 分前に変化。上限が無ければ 300 になる
    observations = to_observations(build([(0, 5), (10, 4), (310, 4)]), KEYS)
    flow = compute_flow(observations)
    assert flow.minutes_since_last_change[2] == CHANGE_CAP_MINUTES


def test_an_unobservable_station_is_still_unknown() -> None:
    """観測が 2 つ未満なら**分からない**（NaN）。上限で埋めない。"""
    observations = to_observations(build([(0, 5)]), KEYS)
    flow = compute_flow(observations)
    assert np.isnan(flow.minutes_since_last_change).all()


def test_minutes_since_last_change_counts_from_the_change() -> None:
    observations = to_observations(build([(0, 5), (10, 4), (40, 4)]), KEYS)
    flow = compute_flow(observations)
    assert flow.minutes_since_last_change[2] == 30.0


def test_a_single_observation_has_no_flow() -> None:
    """差分が取れないので 0。**例外にしない**（新しいポートで普通に起きる）。"""
    assert flow_of([(0, 5)]) == (0, 0, 0)
