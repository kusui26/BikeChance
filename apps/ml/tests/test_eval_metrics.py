"""確率の質の指標（`eval/metrics.py`、開発プラン §7.1）。

**この 1 ファイルの主題は「手で計算できる値と一致すること」。** 指標の実装が静かに
ずれると、表の数字は出るのに意味が変わる。
"""

import math

import numpy as np
import pytest

from bikechance_ml.eval.metrics import (
    CALIBRATION_BINS,
    ECE_QUANTILE_BINS,
    LOG_LOSS_EPSILON,
    brier,
    ece,
    ece_uniform,
    log_loss,
    reliability,
    reliability_quantile,
    score,
    skill,
)
from bikechance_ml.features.arrays import Float32, Float64, Int8


def y(*values: int) -> Int8:
    return np.array(values, dtype=np.int8)


def p(*values: float) -> Float64:
    return np.array(values, dtype=np.float64)


def w(*values: float) -> Float32:
    return np.array(values, dtype=np.float32)


# ── Brier ─────────────────────────────────────────────────────
def test_brier_is_the_mean_squared_error() -> None:
    assert brier(y(1, 0), p(0.75, 0.25)) == pytest.approx(0.0625)


def test_brier_of_a_perfect_prediction_is_zero() -> None:
    assert brier(y(1, 0), p(1.0, 0.0)) == 0.0


def test_brier_weights_the_rows() -> None:
    """**重み付きが主**（開発プラン §6.2）。難所を 4 倍濃く抽出しているため。"""
    # 誤差 0.01 の行を 3、誤差 0.81 の行を 1
    assert brier(y(1, 1), p(0.9, 0.1), w(3, 1)) == pytest.approx((3 * 0.01 + 0.81) / 4)


# ── log loss ──────────────────────────────────────────────────
def test_log_loss_of_a_coin_flip() -> None:
    assert log_loss(y(1), p(0.5)) == pytest.approx(math.log(2))


def test_log_loss_clips_hard_predictions() -> None:
    """**B0 は 0 と 1 を返す。** 外した行が無限大にならないよう丸める。"""
    assert log_loss(y(1), p(0.0)) == pytest.approx(-math.log(LOG_LOSS_EPSILON))


# ── キャリブレーション ────────────────────────────────────────
def test_ece_is_the_weighted_gap_between_predicted_and_observed() -> None:
    """`p=0.9` と言った 3 行のうち当たりが 2、`p=0.1` の 1 行は外れ。"""
    value = ece(y(1, 1, 0, 0), p(0.9, 0.9, 0.1, 0.9), bins=2)
    assert value == pytest.approx((3 * abs(2 / 3 - 0.9) + 1 * abs(0.0 - 0.1)) / 4)


def test_ece_is_zero_for_a_perfectly_calibrated_prediction() -> None:
    """「70% と言った日の 7 割で降る」なら 0。"""
    labels = y(*([1] * 7 + [0] * 3))
    assert ece(labels, p(*[0.7] * 10), bins=10) == pytest.approx(0.0)


def test_reliability_skips_empty_bins() -> None:
    """**0 の点を線で結ぶと嘘の形になる。** 空の区間は返さない。"""
    bins = reliability(y(1, 0), p(0.95, 0.05), bins=10)
    assert len(bins) == 2
    assert [one.n for one in bins] == [1, 1]
    assert bins[0].mean_predicted == pytest.approx(0.05)
    assert bins[-1].mean_observed == pytest.approx(1.0)


def test_reliability_puts_one_in_the_last_bin() -> None:
    """右端の 1.0 が外に落ちない（`digitize` の既定は外に出す）。"""
    bins = reliability(y(1), p(1.0), bins=4)
    assert len(bins) == 1
    assert bins[0].high == pytest.approx(1.0)


# ── まとめ ────────────────────────────────────────────────────
def test_score_reports_the_positive_rate() -> None:
    """Brier の大きさは陽性率と一緒に読まないと意味が取れない。"""
    result = score(y(1, 1, 1, 0), p(0.8, 0.8, 0.8, 0.8))
    assert result.n == 4
    assert result.positives == pytest.approx(0.75)
    assert result.weight == pytest.approx(4.0)


def test_score_uses_the_weights_for_the_positive_rate() -> None:
    result = score(y(1, 0), p(0.5, 0.5), w(3, 1))
    assert result.positives == pytest.approx(0.75)
    assert result.weight == pytest.approx(4.0)


# ── スキルスコア ──────────────────────────────────────────────
def test_skill_is_the_relative_improvement() -> None:
    assert skill(0.5, 1.0) == pytest.approx(0.5)
    assert skill(1.5, 1.0) == pytest.approx(-0.5)


def test_skill_is_none_when_the_reference_is_zero() -> None:
    """**Brier がほぼ 0 のバケツで相対改善は測れない**（W3-16、§4.4 の 30a）。

    ドコモ h=5 の `11+` は B0・B1 とも 0.00000 で、素直に割ると発散した。
    """
    assert skill(0.0, 0.0) is None


# ── 等頻度の ECE（W5 の PR C、W5-07）──────────────────────────
# **判定に使うのは等頻度（15 区間）。** 等幅（20 区間）は過去の記録と比べるために残す。
def test_the_two_bin_counts_are_what_the_plan_says() -> None:
    """開発プラン §7.3 は「ECE（15 等頻度ビン）」と書いている。"""
    assert ECE_QUANTILE_BINS == 15
    assert CALIBRATION_BINS == 20


def test_equal_frequency_bins_carry_the_same_weight() -> None:
    """**等頻度は名前のとおり。** 連続な予測なら、どの区間もほぼ同じ重みになる。"""
    rng = np.random.default_rng(20260913)
    values = p(*rng.random(30_000))
    labels = y(*(rng.random(30_000) < values).astype(np.int8))
    bins = reliability_quantile(labels, values)
    assert len(bins) == ECE_QUANTILE_BINS
    share = [one.weight / len(labels) for one in bins]
    assert max(share) - min(share) < 0.01, f"重みが偏っている: {share}"


def test_equal_width_bins_pile_up_where_the_predictions_do() -> None:
    """**等幅は上の端で潰れる。** これが等頻度に替えた理由（W5 プラン §2.3e）。

    配信中の確率は 66.7% が 0.95〜1.00 に集まる。等幅だとそこが 1 区間になる。
    """
    crowded = p(*([0.96] * 300 + [0.99] * 300 + [0.07] * 400))
    labels = y(*([1] * 560 + [0] * 40 + [0] * 400))
    uniform = reliability(labels, crowded)
    quantile = reliability_quantile(labels, crowded)
    assert len(uniform) == 2, "0.95〜1.00 と 0.05〜0.10 の 2 区間に潰れる"
    assert len(quantile) == 3, "値ごとに分かれる"


def test_the_same_value_never_splits_across_bins() -> None:
    """**分位の境目で同じ値を割ると、実在しないずれが出る。**

    片方が「当たった行」もう片方が「外した行」になり、どちらの区間でも実測が
    予測から離れる——**モデルは何も間違っていないのに ECE が立つ**。
    """
    tied = p(*([0.5] * 1000))
    labels = y(*([1] * 500 + [0] * 500))
    bins = reliability_quantile(labels, tied, bins=10)
    assert len(bins) == 1, "同じ値は 1 区間"
    assert bins[0].n == 1000
    assert ece(labels, tied, bins=10) == pytest.approx(0.0), "完璧に校正されている"


def test_fewer_distinct_values_than_bins_gives_one_bin_each() -> None:
    """**相異なる値が区間より少なければ、値ごとに 1 区間。** それが分解能の上限。

    配信中のベースラインは 1 水平あたり 4〜6 個の値しか取らない（W5 プラン §2.3a）。
    """
    few = p(*([0.1] * 100 + [0.5] * 100 + [0.9] * 100))
    labels = y(*([0] * 100 + [1] * 50 + [0] * 50 + [1] * 100))
    bins = reliability_quantile(labels, few, bins=ECE_QUANTILE_BINS)
    assert [one.low for one in bins] == [0.1, 0.5, 0.9]
    assert [one.high for one in bins] == [0.1, 0.5, 0.9]
    assert [one.n for one in bins] == [100, 100, 100]


def test_the_bounds_are_the_values_that_landed_there() -> None:
    """**名目の境目ではなく、実際に入った値の幅**を持つ（読めるほうを選んだ）。"""
    values = p(*([0.10, 0.12, 0.14] * 100))
    labels = y(*([0, 1, 0] * 100))
    bins = reliability_quantile(labels, values, bins=2)
    assert bins[0].low <= bins[0].high <= bins[-1].low <= bins[-1].high
    assert min(one.low for one in bins) == pytest.approx(0.10)
    assert max(one.high for one in bins) == pytest.approx(0.14)


def test_the_quantile_bins_follow_the_weights() -> None:
    """**重みで等分する**（重み無しの件数ではない）。難所を 4 倍濃く取っているため。"""
    values = p(0.1, 0.2, 0.3, 0.4)
    labels = y(0, 0, 1, 1)
    weights = w(100.0, 1.0, 1.0, 1.0)
    bins = reliability_quantile(labels, values, weights, bins=2)
    assert bins[0].n == 1, "重い 1 行だけで前半の重みを使い切る"
    assert bins[0].weight == pytest.approx(100.0)


def test_score_reports_both_definitions() -> None:
    """**2 つ並べる。** 片方だけだと「悪くなった」のか「見えるようになった」のか分からない。"""
    crowded = p(*([0.96] * 300 + [0.99] * 300 + [0.07] * 400))
    labels = y(*([1] * 560 + [0] * 40 + [0] * 400))
    result = score(labels, crowded)
    assert result.ece == pytest.approx(ece(labels, crowded))
    assert result.ece_uniform == pytest.approx(ece_uniform(labels, crowded))
    assert result.ece != pytest.approx(result.ece_uniform), "この分布では違う値になる"
    assert len(result.bins) == 2, "図は等幅 20（入るのは 2 区間）"
    assert len(result.quantile_bins) == 3, "判定は等頻度（値ごとに分かれる）"


def test_the_judgement_uses_the_quantile_one() -> None:
    """**`Scores.ece` は等頻度**（開発プラン §7.3）。合否はこちらで決める。"""
    crowded = p(*([0.96] * 300 + [0.99] * 300 + [0.07] * 400))
    labels = y(*([1] * 560 + [0] * 40 + [0] * 400))
    assert score(labels, crowded).ece == pytest.approx(ece(labels, crowded, bins=ECE_QUANTILE_BINS))


def test_an_empty_set_has_no_bins() -> None:
    """行が無ければ区間も無い（0 の点を作らない）。"""
    assert reliability_quantile(y(), p()) == ()
