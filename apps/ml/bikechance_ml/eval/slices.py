"""評価の切り口（W3-16、W3 プラン §4.4 の 30）。

**総合の Brier は自明な行に支配される。** 全サンプルの約 54% は「3 台以上ある」行で、
そこは h=5 で Brier 0.0002 以下しかない（§10.1）。総合値だけで判定すると、
**アプリの価値がある領域（残り 0〜2 台）で負けていても合格する。**

だから **system × ターゲット × 水平 × 台数バケツ**で必ず割って出す。総合値も出すが、
それは全体の見当をつけるためのもので、合否は割ったほうで決める。

**切り口はここでだけ作る。** 各モデルが自分で行を選ぶと、比較が成立しなくなる
（§4.4 の 30b）。
"""

from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np

from bikechance_ml.eval.dataset import BUCKET_LABELS, Samples, Target, bucket_index
from bikechance_ml.features.arrays import Bools
from bikechance_ml.features.constants import HORIZONS_MIN

#: 「割らない」を表す印。表では「全体」と書く。
ALL = "全体"


@dataclass(frozen=True)
class Slice:
    """1 つの切り口と、そこに入る行。"""

    system: str
    target: str
    horizon: int | None
    bucket: str | None
    mask: Bools

    @property
    def n(self) -> int:
        return int(self.mask.sum())

    def horizon_label(self) -> str:
        return ALL if self.horizon is None else str(self.horizon)

    def bucket_label(self) -> str:
        return ALL if self.bucket is None else self.bucket


def by_horizon(samples: Samples, target: Target) -> Iterator[Slice]:
    """system × 水平（バケツはまとめる）。**全体の見当をつけるための表。**"""
    for system_index, system in enumerate(samples.systems):
        in_system = np.asarray(samples.system == system_index, dtype=np.bool_)
        for horizon in HORIZONS_MIN:
            mask = np.asarray(in_system & (samples.h_min == horizon), dtype=np.bool_)
            yield Slice(system, target.name, horizon, None, mask)


def by_bucket(samples: Samples, target: Target, horizon: int) -> Iterator[Slice]:
    """1 つの水平について、system × 台数バケツ。**合否はここで決める。**"""
    buckets = bucket_index(samples.count_of(target))
    for system_index, system in enumerate(samples.systems):
        in_system = np.asarray(
            (samples.system == system_index) & (samples.h_min == horizon), dtype=np.bool_
        )
        for slot, label in enumerate(BUCKET_LABELS):
            mask = np.asarray(in_system & (buckets == slot), dtype=np.bool_)
            yield Slice(system, target.name, horizon, label, mask)


def overall(samples: Samples, target: Target) -> Iterator[Slice]:
    """system だけで割ったもの。"""
    for system_index, system in enumerate(samples.systems):
        mask = np.asarray(samples.system == system_index, dtype=np.bool_)
        yield Slice(system, target.name, None, None, mask)
