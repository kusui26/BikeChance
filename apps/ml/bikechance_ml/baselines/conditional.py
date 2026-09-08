"""B1：条件付き持続（W3 プラン §9.6、開発プラン §7.1）。

    P(y=1 | system, バケツ(t), h) の推定値

学習期間で参照表を作り、検証期間で当てはめる。バケツは `{0, 1, 2, 3-5, 6-10, 11+}`。

**条件に使う台数は、当てる対象と同じ側のものにする**（`y_bike` は `bikes`、`y_dock` は
`docks`）。W3 プラン §9.6 は `bikes_bucket(t)` と書いているが、あれは貸出側の式で、
返却側を `bikes` で条件づけると「返せるか」を「借りられるか」で説明することになる
（§12 の 100）。

**推定は重み付きで行う。** 難所を 4 倍濃く抽出しているので、素の平均は母集団の
条件付き確率にならない（逆抽出確率の重みを掛ければ不偏になる）。

**学習期間に無かったセルは、そのシステムと水平の全体平均に落とす。** 落とした件数を
返し、黙って 0 や 0.5 で埋めない。
"""

from dataclasses import dataclass
from typing import Final

import numpy as np

from bikechance_ml.eval.dataset import BUCKET_LABELS, Samples, Target, bucket_index
from bikechance_ml.features.arrays import Bools, Float64, Int64
from bikechance_ml.features.constants import HORIZONS_MIN

_N_BUCKETS: Final[int] = len(BUCKET_LABELS)


@dataclass(frozen=True)
class Table:
    """参照表。`rate[key]` が条件付き確率で、`seen[key]` が学習期間で見たかどうか。

    `total` と `positive` を残してあるのは、**その行自身を除いた推定**（leave-one-out）
    を出せるようにするため。B3 の入力を作るときに使う（`predict_leave_one_out`）。
    """

    n_systems: int
    rate: Float64
    total: Float64
    positive: Float64
    fallback: Float64
    seen: Bools

    @property
    def cells(self) -> int:
        return int(self.seen.sum())

    @property
    def size(self) -> int:
        return len(self.seen)


def fit(samples: Samples, target: Target, keep: Bools) -> Table:
    """学習期間の行から参照表を作る。"""
    fitted = samples.take(keep)
    n_systems = len(samples.systems)
    key = _key(fitted, target)
    weight = fitted.weight.astype(np.float64)
    size = n_systems * len(HORIZONS_MIN) * _N_BUCKETS
    total = np.bincount(key, weights=weight, minlength=size)
    positive = np.bincount(key, weights=weight * fitted.y(target), minlength=size)
    seen = np.asarray(total > 0, dtype=np.bool_)
    return Table(
        n_systems=n_systems,
        rate=np.asarray(np.divide(positive, total, out=np.zeros(size), where=seen)),
        total=total,
        positive=positive,
        fallback=_fallback(fitted, target, n_systems, weight),
        seen=seen,
    )


def predict(table: Table, samples: Samples, target: Target) -> tuple[Float64, int]:
    """当てはめる。**参照表に無かった行の数**も返す。"""
    key = _key(samples, target)
    missing = ~table.seen[key]
    horizon_key = key // _N_BUCKETS
    return np.where(missing, table.fallback[horizon_key], table.rate[key]), int(missing.sum())


def predict_leave_one_out(table: Table, samples: Samples, target: Target) -> Float64:
    """**その行自身を除いて**当てはめる。学習期間の行に使う。

    参照表は学習期間の行から作るので、同じ行に当てはめると「自分の答えを見た」推定に
    なる。B3 はそれを入力にして係数を決めるため、**B1 と B2 を信じすぎる向きに寄る**
    （実測：短い水平で B3 が B1 に負けた。§12 の 101）。
    分子と分母から自分のぶんを引けば、そのまま外した推定になる。

    引いた結果セルが空になる行（学習期間にその 1 行しか無かったセル）は、
    `(system, h)` の全体平均に落とす。
    """
    key = _key(samples, target)
    weight = samples.weight.astype(np.float64)
    total = table.total[key] - weight
    positive = table.positive[key] - weight * samples.y(target)
    usable = total > 0
    horizon_key = key // _N_BUCKETS
    return np.asarray(
        np.divide(positive, total, out=table.fallback[horizon_key].copy(), where=usable)
    )


def _key(samples: Samples, target: Target) -> Int64:
    """`(system, h, バケツ)` を 1 本の番号にする。**バケツが最下位**（水平だけに落とせる）。"""
    bucket = bucket_index(samples.count_of(target))
    return np.asarray(_horizon_key(samples) * _N_BUCKETS + bucket, dtype=np.int64)


def _horizon_key(samples: Samples) -> Int64:
    """`(system, h)` までの番号。バケツを落とした先を引くのに使う。"""
    horizon = np.searchsorted(np.array(HORIZONS_MIN, dtype=np.int16), samples.h_min)
    return np.asarray(samples.system * len(HORIZONS_MIN) + horizon, dtype=np.int64)


def _fallback(samples: Samples, target: Target, n_systems: int, weight: Float64) -> Float64:
    """`(system, h)` までの全体平均。バケツのセルが無いときに落とす先。"""
    key = _horizon_key(samples)
    size = n_systems * len(HORIZONS_MIN)
    total = np.bincount(key, weights=weight, minlength=size)
    positive = np.bincount(key, weights=weight * samples.y(target), minlength=size)
    overall = float((weight * samples.y(target)).sum() / weight.sum()) if len(weight) else 0.5
    return np.asarray(np.divide(positive, total, out=np.full(size, overall), where=total > 0))
