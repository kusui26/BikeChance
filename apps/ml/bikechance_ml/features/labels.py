"""ラベル（W3 プラン §9.3、W3-12）。

    y_bike(t, h) = 1[ bikes(t+h) >= 1 かつ is_renting(t+h) ]
    y_dock(t, h) = 1[ docks(t+h) >= 1 かつ is_returning(t+h) ]

**「借りられる」は台数だけでは決まらない。** 台数が 1 台以上あっても運用停止中なら
借りられない。停止は `t` 時点の除外規則（`exclude.py`）とは別で、**`t+h` の停止は
ラベルが 0 になる**（利用者は到着して借りられなかった）。

**衝突ガード**（W3-12）：`as-of(t+h)` が `as-of(t)` と同じ観測を指すとき、ラベルは
特徴量と同じ 1 つの観測から作られる。これは予測ではなく写経なので落とす。HELLO の
公開周期（300 秒）とグリッド（5 分）が同じことから来る危険への保険で、実測（2026-09-07
の 1 日・5,731 万ペア）では 0 件だった。**費用ゼロで、位相が月単位でずれたときにだけ効く。**
"""

import numpy as np

from bikechance_ml.features.arrays import Bools, Int8, Int16, Int32
from bikechance_ml.features.constants import FLAG_RENTING, FLAG_RETURNING


def y_bike(bikes: Int16, flags: Int16) -> Int8:
    """借りられるか。`t + h` 時点の値を渡す。"""
    return np.asarray((bikes >= 1) & _has(flags, FLAG_RENTING), dtype=np.int8)


def y_dock(docks: Int16, flags: Int16) -> Int8:
    """返せるか。`t + h` 時点の値を渡す。"""
    return np.asarray((docks >= 1) & _has(flags, FLAG_RETURNING), dtype=np.int8)


def _has(flags: Int16, bit: int) -> Bools:
    """ビットが立っているか。**`-1`（欠損）は立っていない扱い**にはしない。

    `-1` は 2 の補数で全ビットが立つので `flags & bit` は真になる。欠損の行は
    `exclude.py` が落とすが、ここでも明示的に偽にしておく（順序に依存させない）。
    """
    return np.asarray((flags >= 0) & ((flags & bit) != 0), dtype=np.bool_)


def is_collision(feature_row: Int32, label_row: Int32) -> Bools:
    """`t+h` の観測が `t` の観測と同じか（W3-12）。**行番号で比べる。**

    時刻で比べない。同じ時刻の別の行というものは無いが、行番号なら
    「同じ観測を 2 度使った」をそのまま表せる。
    """
    return np.asarray(feature_row == label_row, dtype=np.bool_)
