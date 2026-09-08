"""層化抽出と重み（`features/sample.py`、W3-14、W3 プラン §9.3）。

**この 1 ファイルの主題は 3 つ。**

  * スカラ版と numpy 版が**同じ値**を出す（速い側だけを信じない）
  * 抽出は**行ごとに 1 回**（2 回抽選すると重みが厳密でなくなる）
  * 重みは**逆抽出確率**で、総和が母集団に寄る
"""

from datetime import date

import numpy as np

from bikechance_ml.features.arrays import Float64
from bikechance_ml.features.constants import (
    HORIZONS_MIN,
    STRATUM_TIGHT,
    STRATUM_UNIFORM,
    TIGHT_RATE,
    UNIFORM_RATE,
)
from bikechance_ml.features.sample import (
    counter,
    is_tight,
    rate_of,
    station_seed,
    stratum_name,
    uniform,
    uniform_array,
    weight_of,
)

DAY = date(2026, 9, 7)
DRAWS = 200_000


def draws(seed: int, count: int) -> Float64:
    return uniform_array(np.full(count, seed, dtype=np.uint64), np.arange(count, dtype=np.uint64))


# ── 2 つの実装が一致する ──────────────────────────────────────
def test_scalar_and_array_agree_exactly() -> None:
    seed = station_seed(DAY, "hellocycling", "12345")
    counters = [counter(grid, horizon) for grid in range(4) for horizon in range(len(HORIZONS_MIN))]
    scalar = np.array([uniform(seed, one) for one in counters])
    array = uniform_array(
        np.full(len(counters), seed, dtype=np.uint64), np.array(counters, dtype=np.uint64)
    )
    assert np.array_equal(scalar, array)


# ── 決定性 ────────────────────────────────────────────────────
def test_the_same_port_and_day_give_the_same_seed() -> None:
    """`random.seed` に頼らない。**並列化やポートの並び順で変わらない。**"""
    assert station_seed(DAY, "hellocycling", "a") == station_seed(DAY, "hellocycling", "a")


def test_the_seed_depends_on_all_three_parts() -> None:
    """日付・システム・ポートのどれが変わっても別の種になる。"""
    seeds = {
        station_seed(DAY, "hellocycling", "a"),
        station_seed(DAY, "hellocycling", "b"),
        station_seed(DAY, "docomo-cycle", "a"),
        station_seed(date(2026, 9, 8), "hellocycling", "a"),
    }
    assert len(seeds) == 4


def test_counter_separates_grid_points_and_horizons() -> None:
    """`(基準時刻, 水平)` の格子を 1 本に伸ばす。**重複しない。**"""
    values = [counter(grid, horizon) for grid in range(50) for horizon in range(len(HORIZONS_MIN))]
    assert len(set(values)) == len(values)


# ── 分布 ──────────────────────────────────────────────────────
def test_draws_are_uniform_in_the_unit_interval() -> None:
    values = draws(station_seed(DAY, "hellocycling", "a"), DRAWS)
    assert values.min() >= 0.0 and values.max() < 1.0
    assert abs(float(values.mean()) - 0.5) < 0.005


def test_acceptance_rates_match_the_design() -> None:
    """一様 1%、難所 4%。**誤差 0.1 ポイント以内**（20 万回）。"""
    values = draws(station_seed(DAY, "hellocycling", "a"), DRAWS)
    assert abs(float((values < UNIFORM_RATE).mean()) - UNIFORM_RATE) < 0.001
    assert abs(float((values < TIGHT_RATE).mean()) - TIGHT_RATE) < 0.001


def test_one_draw_per_row_keeps_the_weights_exact() -> None:
    """**2 回抽選しない。**

    「一様に 1% 引いてから難所を 3% 足す」と包含確率が
    `1 − 0.99 × 0.97 = 3.97%` になり、重みが 25 でなくなる。1 回の判定なら
    難所の包含確率はちょうど 4% で、重みは 25 になる。
    """
    independent = 1 - (1 - UNIFORM_RATE) * (1 - 0.03)
    assert abs(independent - TIGHT_RATE) > 0.0002
    assert weight_of(np.array([True], dtype=np.bool_))[0] == 1.0 / TIGHT_RATE


# ── 層と重み ──────────────────────────────────────────────────
def test_tight_is_decided_by_the_base_state() -> None:
    """`bikes <= 2` または `docks <= 2`。**水平によらない。**"""
    bikes = np.array([0, 3, 9, 5], dtype=np.int16)
    docks = np.array([9, 3, 1, 5], dtype=np.int16)
    assert is_tight(bikes, docks).tolist() == [True, False, True, False]


def test_weights_are_the_inverse_of_the_rate() -> None:
    tight = np.array([True, False], dtype=np.bool_)
    assert rate_of(tight).tolist() == [TIGHT_RATE, UNIFORM_RATE]
    assert weight_of(tight).tolist() == [25.0, 100.0]


def test_weight_sum_estimates_the_population() -> None:
    """**重みの総和が母集団に寄る**（W3 プラン §7.6 の不偏性の確認）。"""
    values = draws(station_seed(DAY, "hellocycling", "a"), DRAWS)
    accepted = values < UNIFORM_RATE
    estimate = float(accepted.sum()) / UNIFORM_RATE
    assert abs(estimate - DRAWS) / DRAWS < 0.05


def test_stratum_names() -> None:
    assert stratum_name(True) == STRATUM_TIGHT
    assert stratum_name(False) == STRATUM_UNIFORM
