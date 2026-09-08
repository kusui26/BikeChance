"""B0：持続（W3 プラン §9.6、開発プラン §7.1）。

    P = 1[ bikes(t) >= 1 ]      （返却側は docks(t)）

**0 か 1 しか返さない硬い予測**なので、Brier はそのまま「ひっくり返った割合」になる。
短い水平では非常に強く、HELLO の h=5 で Brier 0.00454（＝ 5 分後に借りられなくなるのは
0.45%）。**これを 1% 上回るのがどれだけ難しいか**が、この線を引く理由である（§10.1）。

**`is_renting(t)` を式に足していないのは、除外規則が先に落としているから。**
`t` 時点で貸出停止のポートは学習サンプルに残らない（`features/exclude.py`）ので、
残った行では常に真になる。**除外を外すと B0 だけが有利になり、B1 が 217% 悪く見える**
（W3-15）。この式は「除外を通した集合の上でだけ意味を持つ」。
"""

import numpy as np

from bikechance_ml.features.arrays import Float64, Int16


def predict(counts: Int16) -> Float64:
    """`t` 時点の台数から 0 / 1 を返す。"""
    return np.asarray(counts >= 1, dtype=np.float64)
