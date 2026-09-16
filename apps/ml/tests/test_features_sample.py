"""一様抽出と重み（`features/sample.py`、W3-14、W5 プラン §6.9 の PR I）。

**この 1 ファイルの主題は 4 つ。**

  * スカラ版と numpy 版が**同じ値**を出す（速い側だけを信じない）
  * 抽出は**行ごとに 1 回**（2 回抽選すると重みが厳密でなくなる）
  * **層を見ない**（2026-09-16 に層化をやめた。基準時刻の台数は抽出に効かない）
  * 重みは**逆抽出確率**で、総和が母集団に寄る
"""

from datetime import date

import numpy as np

from bikechance_ml.features.arrays import Float64
from bikechance_ml.features.constants import HORIZONS_MIN, SAMPLE_RATE, SAMPLE_WEIGHT
from bikechance_ml.features.sample import (
    accepted,
    counter,
    station_seed,
    uniform,
    uniform_array,
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


def test_the_drawn_values_are_pinned_to_known_numbers() -> None:
    """**種と通し番号は 2026-09-16 の変更でも動かしていない。**

    層化をやめたのは「どの行を採るか」の判定だけで、乱数そのものは同じである。
    ここが動くと、**2026-09-15 までの日を作り直しても当時と違う行が出る**——
    値を釘で留めておけば、気づかずに動かすことができない。
    """
    seed = station_seed(DAY, "hellocycling", "12345")
    assert seed == 5397463145951707354
    assert uniform(seed, counter(0, 0)) == 0.4515862910697218
    assert uniform(seed, counter(1, 3)) == 0.0713157568365389
    other = station_seed(DAY, "docomo-cycle", "1")
    assert other == 4349026744346200012
    assert uniform(other, counter(287, 9)) == 0.4118523339324994


def test_counter_separates_grid_points_and_horizons() -> None:
    """`(基準時刻, 水平)` の格子を 1 本に伸ばす。**重複しない。**"""
    values = [counter(grid, horizon) for grid in range(50) for horizon in range(len(HORIZONS_MIN))]
    assert len(set(values)) == len(values)


def test_each_horizon_of_one_pair_is_drawn_on_its_own() -> None:
    """**水平はまとめて採らない**（開発プラン §6.2 の文章とはここが違う。§12 の 164）。

    同じ `(ポート, 基準時刻)` の 10 水平に別々の乱数が当たるので、10 本そろって
    入ることはまず無い。**散らばっているほうが推定の分散は小さい。**
    """
    seed = station_seed(DAY, "hellocycling", "12345")
    values = [uniform(seed, counter(7, horizon)) for horizon in range(len(HORIZONS_MIN))]
    assert len(set(values)) == len(HORIZONS_MIN)


# ── 分布 ──────────────────────────────────────────────────────
def test_draws_are_uniform_in_the_unit_interval() -> None:
    values = draws(station_seed(DAY, "hellocycling", "a"), DRAWS)
    assert values.min() >= 0.0 and values.max() < 1.0
    assert abs(float(values.mean()) - 0.5) < 0.005


def test_the_acceptance_rate_matches_the_design() -> None:
    """一様 1%。**誤差 0.1 ポイント以内**（20 万回）。"""
    values = draws(station_seed(DAY, "hellocycling", "a"), DRAWS)
    assert abs(float(accepted(values).mean()) - SAMPLE_RATE) < 0.001


def test_acceptance_does_not_look_at_the_base_state() -> None:
    """**層を見ない。** 引いた値だけで決まる（2026-09-16 の変更の中身そのもの）。

    母集団の 84.3% が「難所」なので、**濃く取ろうとしても濃くならない**
    （`constants.SAMPLE_RATE`）。台数で分岐する経路が戻ってきたら、ここが落ちる。
    """
    values = np.array([0.005, 0.02, 0.039, 0.5], dtype=np.float64)
    assert accepted(values).tolist() == [True, False, False, False]


def test_one_draw_per_row_keeps_the_weight_exact() -> None:
    """**2 回抽選しない。**

    「一様に 1% 引いてから、別の条件で 3% 足す」と包含確率が
    `1 - 0.99 x 0.97 = 3.97%` になり、重みが `1 / 0.04` にならない。
    1 回の判定なら包含確率はちょうど `SAMPLE_RATE` で、重みはその逆数になる。
    """
    independent = 1 - (1 - SAMPLE_RATE) * (1 - 0.03)
    assert abs(independent - 0.04) > 0.0002
    assert SAMPLE_WEIGHT == 1.0 / SAMPLE_RATE


def test_weight_sum_estimates_the_population() -> None:
    """**重みの総和が母集団に寄る**（W3 プラン §7.6 の不偏性の確認）。"""
    values = draws(station_seed(DAY, "hellocycling", "a"), DRAWS)
    estimate = float(accepted(values).sum()) * SAMPLE_WEIGHT
    assert abs(estimate - DRAWS) / DRAWS < 0.05
