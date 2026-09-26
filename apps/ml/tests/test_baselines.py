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


#: B2 の仕組みを見る検査の下限：**全曜日種別 2 日**（学習の行も 2 日。PR A より前の形）。
TWO_DAYS = climatology.DayFloor.uniform(2)


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
    """**重みは行ごとに違いうる**ので、素の平均は母集団の確率にならない。

    2026-09-16 からは一様に引くので実データの重みは定数だが、**09-15 以前の日を
    混ぜれば 25 と 100 が並ぶ**（W5 プラン §6.9 の PR I）。重みを読む経路は要る。
    """
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
    """**1 サンプルの平均は「その日の観測」でしかない**（W3-17）。

    **ここは仕組みを見る検査なので下限を 2 件・2 日に下げている**（この節の検査はみな同じ）。
    学習サンプルから作るときの既定は **30 行・平日 3 日・土日祝は配らない**（`MIN_CELL_ROWS`・
    `SAMPLES_FLOOR`。§12 の 167、D-30、D-37）で、それは別の検査で留める。
    """
    rows = [
        fixture.row(DAY0, "hellocycling", "a", 5, 0, 9, 1, 1),
        fixture.row(DAY1, "hellocycling", "a", 5, 0, 9, 1, 1),
        fixture.row(DAY0, "hellocycling", "b", 5, 0, 9, 1, 1),
    ]
    samples = samples_of(rows)
    fit = np.array([True, True, True])
    table = climatology.fit(samples, BIKE, fit, min_samples=2, floor=TWO_DAYS)
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
    table = climatology.fit(samples, BIKE, all_rows(2), min_samples=2, floor=TWO_DAYS)
    assert table.cells == 1


def test_b2_does_not_mix_stations_with_the_same_id() -> None:
    rows = [
        fixture.row(DAY0, "hellocycling", "1", 5, 0, 9, 1, 1),
        fixture.row(DAY1, "hellocycling", "1", 5, 0, 9, 1, 1),
        fixture.row(DAY0, "docomo-cycle", "1", 5, 0, 9, 0, 1),
    ]
    samples = samples_of(rows)
    table = climatology.fit(samples, BIKE, all_rows(3), min_samples=2, floor=TWO_DAYS)
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
    table = climatology.fit(known, BIKE, all_rows(2), min_samples=2, floor=TWO_DAYS)
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
    table = climatology.fit(known, BIKE, all_rows(3), min_samples=2, floor=TWO_DAYS)
    assert int(table.counted.max()) == 3

    unknown = replace(known, port=np.full(len(known), -1, dtype=np.int32))
    applied = climatology.predict_leave_one_out(table, unknown, BIKE, np.full(3, 0.25))
    assert applied.probability.tolist() == pytest.approx([0.25, 0.25, 0.25])
    assert applied.fell_back == 3


def test_b2_says_it_was_used_without_comparing_probabilities() -> None:
    """**W5 プラン §12 の 144。** 率が B1 と同じ値でも「引けた」は真である。

    配信側は以前 `b2.probability != b1` で判定していた。プロファイルから作った率は
    **ちょうど 1.0 になることが多く**（実測：使えるセルの 53.1%）、B1 の 120 セルにも
    1.0 が在る（実測：貸出 4・返却 6）。**両方が 1.0 の行を「引けなかった」と数え、
    `confidence` が 3 から 2 に落ちる。**
    """
    rows = [fixture.row(day, "hellocycling", "a", 5, 0, 9, 1, 1) for day in (DAY0, DAY1)]
    samples = samples_of(rows)
    table = climatology.fit(samples, BIKE, all_rows(2), min_samples=2, floor=TWO_DAYS)
    applied = climatology.predict(table, samples, np.ones(2))  # B1 も 1.0
    assert applied.probability.tolist() == pytest.approx([1.0, 1.0])
    assert applied.used.tolist() == [True, True], "確率が同じでも引けている"
    assert applied.fell_back == 0


def test_b2_keeps_the_day_count_behind_usable() -> None:
    """**`usable` の根拠を捨てない。** 引いたあとに下限を判定し直すのに要る。"""
    rows = [fixture.row(day, "hellocycling", "a", 5, 0, 9, 1, 1) for day in (DAY0, DAY1)]
    table = climatology.fit(samples_of(rows), BIKE, all_rows(2), min_samples=2, floor=TWO_DAYS)
    assert int(table.days.max()) == 2
    assert int(table.counted.max()) == 2


def test_b2_leave_one_out_also_wants_two_days() -> None:
    """**同じ日の 3 行は 3 サンプルだが 1 日である**（§12 の 101）。

    件数だけを見ていたころは、`predict` が使わないセルを leave-one-out が使っていた。
    B3 は「配信では引けないセル」の値を入力に係数を決めていたことになる。
    """
    rows = [
        fixture.row(DAY0, "hellocycling", "a", 5, 0, 9, 1, 1, minute_of_day=600),
        fixture.row(DAY0, "hellocycling", "a", 10, 0, 9, 1, 1, minute_of_day=595),
        fixture.row(DAY0, "hellocycling", "a", 15, 0, 9, 1, 1, minute_of_day=590),
    ]
    samples = samples_of(rows)
    table = climatology.fit(samples, BIKE, all_rows(3), min_samples=2, floor=TWO_DAYS)
    assert int(table.counted.max()) == 3, "同じセルに 3 行入っている"
    assert table.cells == 0, "1 日しか無いので配信では使わない"
    applied = climatology.predict_leave_one_out(table, samples, BIKE, np.full(3, 0.25))
    assert applied.fell_back == 3, "配信で使わないセルを混合の入力にしない"


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
    """重みを無視すると、重い行を軽んじた係数になる。"""
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


# ── B2 の下限（§12 の 167）────────────────────────────────────
def _cell_rows(count: int, *, days: int) -> Samples:
    """1 セルに `count` 行を入れる（日数は `days`）。**下限だけを見るための材料。**"""
    return samples_of(
        [
            fixture.row(fixture.DAYS[one % days], "hellocycling", "a", 5, 0, 9, 1, 1)
            for one in range(count)
        ]
    )


def test_the_floor_for_samples_is_rows_not_points() -> None:
    """**学習サンプルから作るときの下限は 30 行**（§12 の 167。2026-09-20 に 2 → 30）。

    **プロファイルの下限（2 点）と同じ数にしない。** プロファイルは 1 セルに 1 日 3 点
    入るが、抽出済みの行は 1 日 0.3 行しか来ない。**同じ「2」が違うものを数えていた**
    のが 167 である。
    """
    assert climatology.MIN_CELL_ROWS == 30
    assert climatology.MIN_CELL_POINTS == 2
    assert climatology.MIN_CELL_ROWS > climatology.MIN_CELL_POINTS


def test_a_thin_cell_is_not_usable_by_default() -> None:
    """**既定では 29 行のセルは使わない。** 30 行で初めて使う（日数は足りている状態で見る）。"""
    days = climatology.MIN_CELL_DAYS
    assert climatology.fit(_cell_rows(29, days=days), BIKE, all_rows(29)).cells == 0
    assert climatology.fit(_cell_rows(30, days=days), BIKE, all_rows(30)).cells == 1


def test_the_floor_for_days_is_three() -> None:
    """**日数の下限は 3 日**（2026-09-23 に 2 → 3。D-30、W5 プラン §12 の 177）。

    **2 日ぶんの B2 は、配らないほうが当たっていた**——祝日に配った確率を B2 を落とした
    確率に置き換えると Brier −7.59%。**「2 に戻す」とこの検査が落ちる。**
    """
    assert climatology.MIN_CELL_DAYS == 3


def test_two_days_are_not_enough_by_default() -> None:
    """**件数が足りていても 2 日では使わない。** 3 日で初めて使う（D-30）。"""
    assert climatology.fit(_cell_rows(30, days=2), BIKE, all_rows(30)).cells == 0
    assert climatology.fit(_cell_rows(30, days=3), BIKE, all_rows(30)).cells == 1


def test_the_floor_reaches_the_table_through_the_source() -> None:
    """**`FromSamples` が下限を当てはめに渡している。**

    定数を上げても `table()` が渡さなければ何も変わらない——**決めた値が効くところ
    まで**見る（§12 の 166 で同じ形の抜けを踏んだ）。
    """
    samples = _cell_rows(30, days=climatology.MIN_CELL_DAYS)
    keep = all_rows(30)
    assert climatology.FromSamples().table(samples, BIKE, keep).min_samples == 30
    assert climatology.FromSamples(min_samples=2).table(samples, BIKE, keep).min_samples == 2
    assert climatology.FromSamples().table(samples, BIKE, keep).floor == climatology.SAMPLES_FLOOR
    assert climatology.FromSamples(floor=TWO_DAYS).table(samples, BIKE, keep).floor == TWO_DAYS


def test_the_floor_is_written_into_the_report_line() -> None:
    """**下限は報告書と登録簿に残る。** 「どこまで信じた表か」が後から読めること。"""
    assert climatology.FromSamples().describe() == (
        "学習サンプル（features/、下限 30 行・sat 配らない・sun_holiday 配らない・weekday 3 日）"
    )
    assert "下限 2 行" in climatology.FromSamples(min_samples=2).describe()


def test_the_thin_cell_falls_back_to_b1() -> None:
    """**使えないセルは B1 に落ちる**（黙って 0.5 を返さない）。"""
    samples = _cell_rows(4, days=2)
    table = climatology.fit(samples, BIKE, all_rows(4))
    applied = climatology.predict(table, samples, np.full(4, 0.25))
    assert applied.probability.tolist() == pytest.approx([0.25] * 4)
    assert applied.fallback_ratio == 1.0
