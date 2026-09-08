"""ラベルと衝突ガード（`features/labels.py`、W3-12）。

**この 1 ファイルの主題は「台数だけでラベルを決めないこと」。** 1 台あっても運用停止中
なら借りられない。
"""

import numpy as np

from bikechance_ml.features.labels import is_collision, y_bike, y_dock

OPEN = 7  # is_installed | is_renting | is_returning
SUSPENDED = 1  # is_installed だけ
MISSING = -1


def test_bike_needs_a_bike_and_permission_to_rent() -> None:
    bikes = np.array([0, 1, 5, 1], dtype=np.int16)
    flags = np.array([OPEN, OPEN, OPEN, SUSPENDED], dtype=np.int16)
    assert y_bike(bikes, flags).tolist() == [0, 1, 1, 0]


def test_dock_needs_a_dock_and_permission_to_return() -> None:
    docks = np.array([0, 1, 5, 1], dtype=np.int16)
    flags = np.array([OPEN, OPEN, OPEN, SUSPENDED], dtype=np.int16)
    assert y_dock(docks, flags).tolist() == [0, 1, 1, 0]


def test_missing_flags_do_not_count_as_permission() -> None:
    """**`-1` は 2 の補数で全ビットが立つ。** ここで明示的に落としておく。

    欠損の行は `exclude.py` が落とすが、順序に依存させない。
    """
    assert y_bike(np.array([5], dtype=np.int16), np.array([MISSING], dtype=np.int16)) == 0
    assert y_dock(np.array([5], dtype=np.int16), np.array([MISSING], dtype=np.int16)) == 0


def test_suspension_makes_the_label_zero_not_excluded() -> None:
    """`t + h` の停止は**ラベル 0**（利用者は到着して借りられなかった）。

    除外するのは `t` 時点の停止だけ（`exclude.py`）。ここを取り違えると、
    「行こうとしたら止まっていた」という一番知りたい場合が学習から消える。
    """
    assert y_bike(np.array([9], dtype=np.int16), np.array([SUSPENDED], dtype=np.int16)) == 0


def test_collision_is_compared_by_row_not_by_time() -> None:
    """同じ観測を特徴量とラベルの両方に使ったら、それは予測ではなく写経。"""
    feature = np.array([3, 3, -1], dtype=np.int32)
    label = np.array([3, 4, -1], dtype=np.int32)
    assert is_collision(feature, label).tolist() == [True, False, True]
