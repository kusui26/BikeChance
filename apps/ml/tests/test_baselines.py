"""ベースライン B0〜B3（`baselines/`、W3 プラン §9.6）。

**この 1 ファイルの主題は 3 つ。**

  * B0 は「いま在るか」だけを見る硬い予測（除外規則を通した集合の上でだけ意味を持つ）
  * B1 は**重み付き**で当てはめ、学習期間に無かったセルは黙って埋めない
  * B2 は**下限に満たないセルを B1 に落とし、その割合を返す**（W3-17）
"""

from dataclasses import replace

import numpy as np
import pytest

from bikechance_ml.baselines import blend, climatology, conditional, persistence
from bikechance_ml.eval.dataset import TARGETS, Samples, to_samples
from bikechance_ml.features.arrays import Bools
from tests import eval_fixture as fixture

BIKE, DOCK = TARGETS
DAY0, DAY1, DAY2 = fixture.DAYS


def samples_of(rows: list[dict[str, object]]) -> Samples:
    return to_samples(fixture.to_table(rows))


def all_rows(n: int) -> Bools:
    return np.ones(n, dtype=np.bool_)


# ── B0：持続 ──────────────────────────────────────────────────
def test_b0_is_one_when_a_bike_is_there() -> None:
    counts = np.array([0, 1, 9], dtype=np.int16)
    assert persistence.predict(counts).tolist() == [0.0, 1.0, 1.0]


# ── B1：条件付き持続 ──────────────────────────────────────────
def test_b1_learns_the_rate_per_cell() -> None:
    """同じ `(system, h, バケツ)` の行の**重み付き平均**になる。"""
    rows = [
        fixture.row(DAY0, "hellocycling", "a", 5, 0, 9, 1, 1),
        fixture.row(DAY0, "hellocycling", "b", 5, 0, 9, 0, 1),
        fixture.row(DAY0, "hellocycling", "c", 5, 0, 9, 0, 1),
        fixture.row(DAY0, "hellocycling", "d", 5, 0, 9, 0, 1),
    ]
    samples = samples_of(rows)
    table = conditional.fit(samples, BIKE, all_rows(4))
    probability, missing = conditional.predict(table, samples, BIKE)
    assert probability.tolist() == pytest.approx([0.25] * 4)
    assert missing == 0


def test_b1_uses_the_sampling_weights() -> None:
    """**難所を 4 倍濃く抽出している**ので、素の平均は母集団の確率にならない。"""
    rows = [
        fixture.row(DAY0, "hellocycling", "a", 5, 0, 9, 1, 1, weight=25.0),
        fixture.row(DAY0, "hellocycling", "b", 5, 0, 9, 0, 1, weight=75.0),
    ]
    samples = samples_of(rows)
    table = conditional.fit(samples, BIKE, all_rows(2))
    probability, _ = conditional.predict(table, samples, BIKE)
    assert probability[0] == pytest.approx(0.25)


def test_b1_separates_buckets_horizons_and_systems() -> None:
    rows = [
        fixture.row(DAY0, "hellocycling", "a", 5, 0, 9, 0, 1),
        fixture.row(DAY0, "hellocycling", "b", 5, 4, 5, 1, 1),
        fixture.row(DAY0, "hellocycling", "c", 60, 0, 9, 1, 1),
        fixture.row(DAY0, "docomo-cycle", "d", 5, 0, 9, 1, 1),
    ]
    samples = samples_of(rows)
    table = conditional.fit(samples, BIKE, all_rows(4))
    probability, _ = conditional.predict(table, samples, BIKE)
    assert probability.tolist() == pytest.approx([0.0, 1.0, 1.0, 1.0])


def test_b1_conditions_the_dock_target_on_docks() -> None:
    """**返せるかを、借りられるかで説明しない**（§12 の 100）。

    `bikes` は同じで `docks` が違う 2 行。`y_dock` が食い違うので、`docks` で
    条件づけていれば両方当たり、`bikes` で条件づけていれば 0.5 になる。
    """
    rows = [
        fixture.row(DAY0, "hellocycling", "a", 5, 4, 0, 1, 0),
        fixture.row(DAY0, "hellocycling", "b", 5, 4, 9, 1, 1),
    ]
    samples = samples_of(rows)
    table = conditional.fit(samples, DOCK, all_rows(2))
    probability, _ = conditional.predict(table, samples, DOCK)
    assert probability.tolist() == pytest.approx([0.0, 1.0])


def test_b1_falls_back_and_counts_unseen_cells() -> None:
    """**学習期間に無かったセルは黙って埋めない。** 件数を返す。"""
    rows = [
        fixture.row(DAY0, "hellocycling", "a", 5, 0, 9, 1, 1),
        fixture.row(DAY1, "hellocycling", "b", 5, 7, 2, 0, 1),
    ]
    samples = samples_of(rows)
    fit_only_first = np.array([True, False])
    table = conditional.fit(samples, BIKE, fit_only_first)
    probability, missing = conditional.predict(table, samples, BIKE)
    assert missing == 1
    # 落とし先は (system, h) の全体平均。学習期間には 1 行しか無いので 1.0
    assert probability[1] == pytest.approx(1.0)


# ── B2：気候値 ────────────────────────────────────────────────
def test_b2_needs_at_least_two_samples_per_cell() -> None:
    """**1 サンプルの平均は「その日の観測」でしかない**（W3-17）。"""
    rows = [
        fixture.row(DAY0, "hellocycling", "a", 5, 0, 9, 1, 1),
        fixture.row(DAY1, "hellocycling", "a", 5, 0, 9, 1, 1),
        fixture.row(DAY0, "hellocycling", "b", 5, 0, 9, 1, 1),
    ]
    samples = samples_of(rows)
    fit = np.array([True, True, True])
    table = climatology.fit(samples, BIKE, fit)
    assert table.cells == 1  # a の 1 セルだけが 2 サンプルに達する
    fallback = np.full(3, 0.5)
    applied = climatology.predict(table, samples, fallback)
    assert applied.probability.tolist() == pytest.approx([1.0, 1.0, 0.5])
    assert applied.fell_back == 1
    assert applied.fallback_ratio == pytest.approx(1 / 3)


def test_b2_cells_are_keyed_on_the_arrival_slot() -> None:
    """**時刻は `t + h`（到着時刻）で切る。** 着いたときの状態を知りたい。"""
    rows = [
        fixture.row(DAY0, "hellocycling", "a", 5, 0, 9, 1, 1, minute_of_day=600),
        fixture.row(DAY1, "hellocycling", "a", 15, 0, 9, 1, 1, minute_of_day=590),
    ]
    samples = samples_of(rows)
    # 600+5 = 605 と 590+15 = 605 → どちらも枠 40（605 // 15）
    assert climatology.slot15(samples).tolist() == [40, 40]
    table = climatology.fit(samples, BIKE, all_rows(2))
    assert table.cells == 1


def test_b2_does_not_mix_stations_with_the_same_id() -> None:
    rows = [
        fixture.row(DAY0, "hellocycling", "1", 5, 0, 9, 1, 1),
        fixture.row(DAY1, "hellocycling", "1", 5, 0, 9, 1, 1),
        fixture.row(DAY0, "docomo-cycle", "1", 5, 0, 9, 0, 1),
    ]
    samples = samples_of(rows)
    table = climatology.fit(samples, BIKE, all_rows(3))
    applied = climatology.predict(table, samples, np.full(3, 0.5))
    assert applied.probability.tolist() == pytest.approx([1.0, 1.0, 0.5])


def test_b2_does_not_borrow_the_last_ports_cell_for_an_unknown_port() -> None:
    """**表に無いポートは `port = -1` で来る**（§12 の 110）。

    `_key` が作る番号は負になり、**numpy の負インデックスは表の末尾に回り込む**ので、
    素直に引くと**最後のポートの気候値**が返る。ここは表がポート 1 つぶん（288 セル）
    しかないので `-1` は同じセルにぴたりと重なる：直す前は 0.25 ではなく 1.0
    （ポート a の気候値）が返っていた。
    """
    rows = [
        fixture.row(DAY0, "hellocycling", "a", 5, 0, 9, 1, 1),
        fixture.row(DAY1, "hellocycling", "a", 5, 0, 9, 1, 1),
    ]
    known = samples_of(rows)
    table = climatology.fit(known, BIKE, all_rows(2))
    assert table.cells == 1, "回り込む先に使えるセルが無いと、この検査は何も見ていない"

    unknown = replace(known, port=np.full(len(known), -1, dtype=np.int32))
    applied = climatology.predict(table, unknown, np.full(2, 0.25))
    assert applied.probability.tolist() == pytest.approx([0.25, 0.25])
    assert applied.fell_back == 2


def test_b2_leave_one_out_also_ignores_an_unknown_port() -> None:
    """学習側の引き方も同じ表を引く。**片方だけ直すと、もう片方に残る。**

    3 日ぶんあるので、自分を引いても下限（2 サンプル）を満たす。つまり**回り込めば
    値が返ってしまう**状況を作ってから確かめている。
    """
    rows = [fixture.row(day, "hellocycling", "a", 5, 0, 9, 1, 1) for day in (DAY0, DAY1, DAY2)]
    known = samples_of(rows)
    table = climatology.fit(known, BIKE, all_rows(3))
    assert int(table.counted.max()) == 3

    unknown = replace(known, port=np.full(len(known), -1, dtype=np.int32))
    applied = climatology.predict_leave_one_out(table, unknown, BIKE, np.full(3, 0.25))
    assert applied.probability.tolist() == pytest.approx([0.25, 0.25, 0.25])
    assert applied.fell_back == 3


# ── B3：混合 ──────────────────────────────────────────────────
def test_b3_recovers_known_coefficients() -> None:
    """**入力 3 つなので勾配法で足りる**（`scikit-learn` を足さない。W3-20）。"""
    rng = np.random.default_rng(0)
    n = 50_000
    x = rng.normal(size=(n, 3))
    true_weights = np.array([1.5, -0.8, 0.4])
    probability = 1 / (1 + np.exp(-(x @ true_weights - 0.3)))
    y = (rng.random(n) < probability).astype(np.int8)
    model = blend.fit(x, y, np.ones(n, dtype=np.float32))
    recovered = model.weights / model.scale
    assert recovered.tolist() == pytest.approx(true_weights.tolist(), abs=0.05)


def test_b3_predictions_stay_inside_the_unit_interval() -> None:
    """logit を丸めているので、0 と 1 の入力でも壊れない。"""
    x = blend.design(
        np.array([0.0, 1.0, 0.5]),
        np.array([1.0, 0.0, 0.5]),
        np.array([5, 180, 60], dtype=np.int16),
    )
    model = blend.fit(x, np.array([1, 0, 1], dtype=np.int8), np.ones(3, dtype=np.float32))
    predicted = blend.predict(model, x)
    assert np.all(predicted > 0.0)
    assert np.all(predicted < 1.0)


def test_b3_uses_the_weights() -> None:
    """重みを無視すると、難所に寄った係数になる。"""
    x = np.zeros((2, 3))
    x[0, 0], x[1, 0] = -1.0, 1.0
    heavy = blend.fit(x, np.array([1, 0], dtype=np.int8), np.array([99.0, 1.0], dtype=np.float32))
    assert blend.predict(heavy, x).mean() > 0.5


def test_b3_design_matrix_is_logit_logit_horizon() -> None:
    x = blend.design(np.array([0.5]), np.array([0.5]), np.array([90], dtype=np.int16))
    assert x.shape == (1, 3)
    assert x[0, 0] == pytest.approx(0.0)
    assert x[0, 2] == pytest.approx(0.5)


# ── 標準化の分母（W3 プラン §12 の 104）────────────────────────
def test_blend_floors_the_standardisation_scale() -> None:
    """**動いていない列の分母は 1 にする。**

    素の標準偏差をそのまま持つと、成果物に書き出すときの丸めで 0 になり、
    読み直したモデルがゼロ除算する。中心を引けば 0 になる列なので 1 で割ってよい。
    """
    x = np.zeros((100, 3))
    x[:, 0] = np.linspace(-1, 1, 100)
    x[:, 1] = 0.5  # まったく動かない列
    x[:, 2] = 0.5 + np.linspace(0, 1e-9, 100)  # 動いているが桁が小さすぎる列
    model = blend.fit(x, np.array([1, 0] * 50, dtype=np.int8), np.ones(100, dtype=np.float32))
    assert model.scale[1] == 1.0
    assert model.scale[2] == 1.0
    assert model.scale[0] > blend.SCALE_FLOOR
    assert np.all(np.isfinite(blend.predict(model, x)))
