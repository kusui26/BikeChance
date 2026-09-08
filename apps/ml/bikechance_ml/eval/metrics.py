"""確率の質を測る（開発プラン §7.1、W3 プラン §9.6）。

**主指標は Brier。** 精度（当たった外れた）ではなく、**確率としての良さ**を見る。
降水確率と同じで、「70% と言った日のうち 7 割で降る」ことが目的である。

**重み付きを主、重み無しを従とする**（開発プラン §6.2）。難所を 4 倍濃く抽出して
いるので、重み無しの平均は母集団の平均ではない。両方を並べて出し、差が大きいときは
抽出の効き方を疑う。

**すべての指標は同じ行の上で測る**（§4.4 の 30b）。件数が違えば比較は成立しない。
"""

from dataclasses import dataclass
from typing import Final

import numpy as np

from bikechance_ml.features.arrays import Float32, Float64, Int8

#: 確率をこの範囲に丸めてから log を取る。B0 は 0 と 1 を返すので、そのままだと
#: 外した行が無限大になる。**丸める前提を明示しておく**（log loss の解釈に効く）。
LOG_LOSS_EPSILON: Final[float] = 1e-15

#: 信頼度図とキャリブレーション誤差の区間の数。
CALIBRATION_BINS: Final[int] = 20


@dataclass(frozen=True)
class ReliabilityBin:
    """信頼度図の 1 区間。"""

    low: float
    high: float
    n: int
    weight: float
    mean_predicted: float
    mean_observed: float


@dataclass(frozen=True)
class Scores:
    """1 つの集合・1 つのモデルの成績。"""

    n: int
    weight: float
    #: 実測の陽性率（重み付き）。Brier の大きさを読むときの基準になる
    positives: float
    brier: float
    log_loss: float
    ece: float
    #: 信頼度図。**ECE の内訳**でもあるので、同じ計算から取っておく
    bins: tuple[ReliabilityBin, ...]


def brier(y: Int8, p: Float64, w: Float32 | None = None) -> float:
    """Brier スコア（重み付き平均二乗誤差）。**小さいほどよい。**"""
    return _mean(np.asarray(np.square(p - y), dtype=np.float64), w)


def log_loss(y: Int8, p: Float64, w: Float32 | None = None) -> float:
    """対数損失。**外した確信が重い。** 0 と 1 は `LOG_LOSS_EPSILON` に丸める。"""
    clipped = np.clip(p, LOG_LOSS_EPSILON, 1.0 - LOG_LOSS_EPSILON)
    terms = y * np.log(clipped) + (1 - y) * np.log(1.0 - clipped)
    return -_mean(np.asarray(terms, dtype=np.float64), w)


def ece(y: Int8, p: Float64, w: Float32 | None = None, bins: int = CALIBRATION_BINS) -> float:
    """キャリブレーション誤差。**「70% と言った日の 7 割で降る」からのずれ。**

    予測確率で等幅に区切り、区間ごとの |実測 − 予測| を重みで平均する。
    """
    return gap_of(reliability(y, p, w, bins), _total(w, len(y)))


def gap_of(bins: tuple[ReliabilityBin, ...], total_weight: float) -> float:
    """信頼度図から ECE を出す。**同じ区切りから 2 度計算しない。**"""
    gap = sum(one.weight * abs(one.mean_observed - one.mean_predicted) for one in bins)
    return gap / max(total_weight, np.finfo(np.float64).tiny)


def reliability(
    y: Int8, p: Float64, w: Float32 | None = None, bins: int = CALIBRATION_BINS
) -> tuple[ReliabilityBin, ...]:
    """信頼度図。**空の区間は返さない**（0 の点を線で結ぶと嘘の形になる）。"""
    weights = np.ones(len(y), dtype=np.float64) if w is None else w.astype(np.float64)
    edges = np.linspace(0.0, 1.0, bins + 1)
    # 右端の 1.0 を最後の区間に入れる（`digitize` は既定で外に出す）
    index = np.clip(np.digitize(p, edges[1:-1], right=False), 0, bins - 1)
    return tuple(
        _bin(
            edges[slot], edges[slot + 1], y[index == slot], p[index == slot], weights[index == slot]
        )
        for slot in range(bins)
        if bool(np.any(index == slot))
    )


def _bin(low: float, high: float, y: Int8, p: Float64, w: Float64) -> ReliabilityBin:
    total = float(w.sum())
    return ReliabilityBin(
        low=float(low),
        high=float(high),
        n=len(y),
        weight=total,
        mean_predicted=float((p * w).sum() / total),
        mean_observed=float((y * w).sum() / total),
    )


def score(y: Int8, p: Float64, w: Float32 | None = None) -> Scores:
    """1 つのモデルの成績をまとめる。"""
    bins = reliability(y, p, w)
    total = _total(w, len(y))
    return Scores(
        n=len(y),
        weight=total,
        positives=_mean(np.asarray(y, dtype=np.float64), w),
        brier=brier(y, p, w),
        log_loss=log_loss(y, p, w),
        ece=gap_of(bins, total),
        bins=bins,
    )


def skill(value: float, reference: float) -> float | None:
    """Brier スキルスコア。**参照が 0 なら測れない**ので None を返す。

    「相対 10% 改善」を Brier がほぼ 0 のバケツに当てはめると数値ノイズを追うことに
    なる（W3-16、§12 の 30a）。**発散させずに「測れない」と言う。**
    """
    return None if reference <= 0.0 else 1.0 - value / reference


def _mean(values: Float64, w: Float32 | None) -> float:
    if len(values) == 0:
        return float("nan")
    if w is None:
        return float(values.mean())
    weights = w.astype(np.float64)
    return float((values * weights).sum() / weights.sum())


def _total(w: Float32 | None, count: int) -> float:
    return float(count) if w is None else float(w.astype(np.float64).sum())
