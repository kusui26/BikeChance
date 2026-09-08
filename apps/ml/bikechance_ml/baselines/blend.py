"""B3：B1 と B2 の混合（W3 プラン §9.6、W3-20）。

    logit(P) = a + b1·logit(B1) + b2·logit(B2) + b3·(h / 180)

**入力は 3 つしかないので、勾配法を自前で書く**（`scikit-learn` を足さない。W3-20）。
学習期間で係数を当てはめ、検証期間で評価する。

**入力を標準化してから降りる。** logit は概ね ±10、`h/180` は 0〜1 で桁が違うので、
素の勾配法では学習率を 1 つに決められない。平均 0・標準偏差 1 に直せば、同じ学習率で
どの列も同じ速さで動く。標準化の値は係数と一緒に持ち回る（検証期間でも同じ変換を使う）。

**重み付きで当てはめる。** 難所を 4 倍濃く抽出しているので、素の対数尤度を最大化すると
難所に寄った係数になる。

**B1・B2 は学習期間の行の上で「その行自身を含んで」当てはめた値である。** つまり
B3 の入力は学習期間では少し楽観的で、係数は B1・B2 を信じすぎる向きに寄る。日数が
増えれば薄まるが、**B3 が検証期間で B1 に負けたらまずこれを疑う。**
"""

from dataclasses import dataclass
from typing import Final

import numpy as np

from bikechance_ml.features.arrays import Float32, Float64, Int8, Int16

#: logit を取る前に確率を丸める範囲。0 と 1 をそのまま入れると無限大になる。
PROBABILITY_CLIP: Final[float] = 1e-6

#: 勾配法の設定。標準化してあるので、この学習率と回数でどの日でも収束する
#: （`tests/test_baselines_blend.py` が既知の係数を取り戻せることで確かめている）。
LEARNING_RATE: Final[float] = 0.5
ITERATIONS: Final[int] = 400

#: 水平を 0〜1 に写す基準（最長の水平）。
HORIZON_SCALE: Final[float] = 180.0

#: 標準化の分母の下限。**これより散らばりが小さい列は「動いていない」とみなす。**
#: 素の標準偏差をそのまま使うと、成果物に書き出すときの丸めで 0 になってゼロ除算になる
#: （W3 プラン §12 の 104）。動いていない列は中心を引けば 0 になるので、1 で割ってよい。
SCALE_FLOOR: Final[float] = 1e-6


@dataclass(frozen=True)
class Blend:
    """当てはめた係数と、入力の標準化。"""

    intercept: float
    weights: Float64
    center: Float64
    scale: Float64

    def raw(self) -> tuple[float, Float64]:
        """標準化を戻した係数。**表に出すのはこちら**（標準化のままだと読めない）。"""
        weights = self.weights / self.scale
        return self.intercept - float((self.center / self.scale) @ self.weights), weights

    def describe(self) -> str:
        names = ("logit(B1)", "logit(B2)", "h/180")
        intercept, weights = self.raw()
        terms = ", ".join(
            f"{name} {value:+.3f}" for name, value in zip(names, weights, strict=True)
        )
        return f"切片 {intercept:+.3f}, {terms}"


def design(first: Float64, second: Float64, h_min: Int16) -> Float64:
    """入力行列 `[logit(B1), logit(B2), h/180]` を作る。"""
    return np.stack(
        [_logit(first), _logit(second), h_min.astype(np.float64) / HORIZON_SCALE], axis=1
    )


def fit(
    x: Float64,
    y: Int8,
    w: Float32,
    iterations: int = ITERATIONS,
    learning_rate: float = LEARNING_RATE,
) -> Blend:
    """重み付きロジスティック回帰。**標準化してから素直に降りる。**"""
    center = np.asarray(x.mean(axis=0), dtype=np.float64)
    spread = np.asarray(x.std(axis=0), dtype=np.float64)
    scale = np.asarray(np.where(spread > SCALE_FLOOR, spread, 1.0), dtype=np.float64)
    scaled = (x - center) / scale
    weights = np.zeros(x.shape[1], dtype=np.float64)
    intercept = 0.0
    sample = w.astype(np.float64)
    total = float(sample.sum())
    for _ in range(iterations):
        error = _sigmoid(scaled @ weights + intercept) - y
        weights -= learning_rate * (scaled.T @ (sample * error)) / total
        intercept -= learning_rate * float((sample * error).sum()) / total
    return Blend(intercept=intercept, weights=weights, center=center, scale=scale)


def predict(model: Blend, x: Float64) -> Float64:
    """当てはめる。**学習期間と同じ標準化を使う。**"""
    scaled = (x - model.center) / model.scale
    return _sigmoid(scaled @ model.weights + model.intercept)


def _logit(p: Float64) -> Float64:
    clipped = np.clip(p, PROBABILITY_CLIP, 1.0 - PROBABILITY_CLIP)
    return np.asarray(np.log(clipped / (1.0 - clipped)), dtype=np.float64)


def _sigmoid(z: Float64) -> Float64:
    """数値的に安定な形。`z` が大きいと `exp(z)` が溢れる。"""
    positive = z >= 0
    result = np.empty_like(z)
    result[positive] = 1.0 / (1.0 + np.exp(-z[positive]))
    tail = np.exp(z[~positive])
    result[~positive] = tail / (1.0 + tail)
    return result
