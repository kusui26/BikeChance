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

#: **信頼度図**の区間の数（**等幅**）。図として読むための刻みで、合否には使わない。
CALIBRATION_BINS: Final[int] = 20

#: **判定に使う ECE** の区間の数（**等頻度**。開発プラン §7.3 の「15 等頻度ビン」）。
#:
#: **等幅では上の端が潰れる。** 配信中の確率は 20 等幅のうち 4 区間にしか入らず、
#: **66.7% が最上位（0.95〜1.00）に集まる**（W5 プラン §2.3e）。その中は 0.962 から
#: 0.999 まで分かれているのに、等幅の ECE はそれを 1 点として平均してしまう——
#: **アプリの約束（85% 以上＝「ほぼ大丈夫」。開発プラン §9.3）がいちばん潰れている帯**である。
#:
#: **同じ値は同じ区間に入れる**（`reliability_quantile`）。相異なる値が区間の数より
#: 少なければ、**値ごとに 1 区間**になる——それが分解能の上限で、いちばん細かい見方になる。
ECE_QUANTILE_BINS: Final[int] = 15


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
    #: **判定に使う ECE**（等頻度 15。開発プラン §7.3）。合否はこれで決める
    ece: float
    #: **等幅 20 の ECE**。図（`bins`）と同じ区切りで、**過去の数字と比べるため**に残す
    #: （2026-09-13 より前の記録はすべてこちら。W5 プラン §12 の 133）
    ece_uniform: float
    #: 信頼度図（**等幅 20**）。`ece_uniform` の内訳でもあるので、同じ計算から取っておく
    bins: tuple[ReliabilityBin, ...]
    #: 判定に使った側の区間（**等頻度**）。**なぜその ECE になったかを読むため**
    quantile_bins: tuple[ReliabilityBin, ...]


def brier(y: Int8, p: Float64, w: Float32 | None = None) -> float:
    """Brier スコア（重み付き平均二乗誤差）。**小さいほどよい。**"""
    return _mean(np.asarray(np.square(p - y), dtype=np.float64), w)


def log_loss(y: Int8, p: Float64, w: Float32 | None = None) -> float:
    """対数損失。**外した確信が重い。** 0 と 1 は `LOG_LOSS_EPSILON` に丸める。"""
    clipped = np.clip(p, LOG_LOSS_EPSILON, 1.0 - LOG_LOSS_EPSILON)
    terms = y * np.log(clipped) + (1 - y) * np.log(1.0 - clipped)
    return -_mean(np.asarray(terms, dtype=np.float64), w)


def ece(y: Int8, p: Float64, w: Float32 | None = None, bins: int = ECE_QUANTILE_BINS) -> float:
    """キャリブレーション誤差。**「70% と言った日の 7 割で降る」からのずれ。**

    **等頻度で区切る**（開発プラン §7.3）。区間ごとの |実測 − 予測| を重みで平均する。
    等幅の値が要るときは `ece_uniform`。
    """
    return gap_of(reliability_quantile(y, p, w, bins), _total(w, len(y)))


def ece_uniform(
    y: Int8, p: Float64, w: Float32 | None = None, bins: int = CALIBRATION_BINS
) -> float:
    """**等幅**で区切った ECE。**2026-09-13 より前に記録した数字はこちら。**

    残してあるのは**比べるため**だけで、合否には使わない（W5-07）。
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


def reliability_quantile(
    y: Int8, p: Float64, w: Float32 | None = None, bins: int = ECE_QUANTILE_BINS
) -> tuple[ReliabilityBin, ...]:
    """**等頻度**（分位）で区切った信頼度図。**判定に使う ECE はここから出す。**

    **同じ値は必ず同じ区間に入る。** 分位の境目で同じ予測値を 2 つに割ると、片方が
    「当たった行」もう片方が「外した行」になって、**実在しないずれが出る**——モデルは
    何も間違っていないのに ECE が立つ。**それを防いでいるのは `searchsorted` そのもの**
    である：同じ値は必ず同じ添字になる。

    **境目に等しい値は下の区間に入れる**（`side="left"`）。境目は「重みの半分を
    使い切った行の値」なので、**その行を含めて下**にするのが素直である。上に送ると、
    重い行が 1 つあるだけで下の区間が空になる（壊して確かめた。W5 プラン §12 の 140）。

    **区間の数は要求より少なくなることがある。** 相異なる値が要求より少なければ
    **値ごとに 1 区間**になり、それが分解能の上限である——配信中のベースラインは
    1 水平あたり 4〜6 個の値しか取らないので（W5 プラン §2.3a）、15 は上限として働く。

    `low` / `high` は**その区間に実際に入った値の最小と最大**である（名目の境目では
    ない）。値が 1 つしかない区間では `low == high` になり、そのほうが読める。
    """
    weights = np.ones(len(y), dtype=np.float64) if w is None else w.astype(np.float64)
    if len(y) == 0:
        return ()
    index = np.searchsorted(_quantile_edges(p, weights, bins), p, side="left")
    return tuple(
        _quantile_bin(y[index == slot], p[index == slot], weights[index == slot])
        for slot in np.unique(index)
    )


def _quantile_edges(p: Float64, w: Float64, bins: int) -> Float64:
    """重みで等分した境目。**昇順で、重複しうる。**

    **重複を落とす必要は無い。** 同じ境目が 2 つ並んでも `searchsorted` は同じ値に
    同じ添字を返すので、区切り方は変わらない（空の添字が増えるだけで、それは
    `np.unique(index)` が飛ばす）。**落とすコードを書いていたが、壊しても検査が 1 つも
    落ちなかったので外した**（W5 プラン §12 の 140）。
    """
    order = np.argsort(p, kind="stable")
    cumulative = np.cumsum(w[order])
    wanted = np.linspace(0.0, 1.0, bins + 1)[1:-1] * float(cumulative[-1])
    positions = np.clip(np.searchsorted(cumulative, wanted, side="left"), 0, len(p) - 1)
    return np.asarray(p[order][positions], dtype=np.float64)


def _quantile_bin(y: Int8, p: Float64, w: Float64) -> ReliabilityBin:
    """1 区間ぶん。**境目ではなく、実際に入った値の幅**を持たせる。"""
    return _bin(float(p.min()), float(p.max()), y, p, w)


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
    """1 つのモデルの成績をまとめる。**ECE は 2 つ出す**（W5-07）。

    判定に使うのは等頻度のほうだが、**等幅も残す**——過去の記録はすべて等幅で、
    片方だけにすると「悪くなった」のか「見えるようになった」のかが分からなくなる。
    """
    bins = reliability(y, p, w)
    quantile = reliability_quantile(y, p, w)
    total = _total(w, len(y))
    return Scores(
        n=len(y),
        weight=total,
        positives=_mean(np.asarray(y, dtype=np.float64), w),
        brier=brier(y, p, w),
        log_loss=log_loss(y, p, w),
        ece=gap_of(quantile, total),
        ece_uniform=gap_of(bins, total),
        bins=bins,
        quantile_bins=quantile,
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
