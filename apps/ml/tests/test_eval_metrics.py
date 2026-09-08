"""確率の質の指標（`eval/metrics.py`、開発プラン §7.1）。

**この 1 ファイルの主題は「手で計算できる値と一致すること」。** 指標の実装が静かに
ずれると、表の数字は出るのに意味が変わる。
"""

import math

import numpy as np
import pytest

from bikechance_ml.eval.metrics import (
    LOG_LOSS_EPSILON,
    brier,
    ece,
    log_loss,
    reliability,
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
