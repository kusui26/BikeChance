"""時系列分割（開発プラン §6.2、W3 プラン §5.9）。

**ランダム分割は禁止。** 日単位で切り、学習と検証のあいだに**パージ**を 1 日置く。

パージが要るのは、学習期間の最後の日の夜に置いた基準時刻 `t` のラベルが `t + h`
（最長 180 分）で翌日に食い込むためである。パージを入れないと、その翌日を検証に使う
ときに**ラベルが学習側に漏れる**。1 日は水平の最大（3 時間）に対して十分に保守的で、
日単位で切るぶんの余裕でもある。

**この 1 か所だけが「どの行で当てはめ、どの行で測るか」を決める。** モデルごとに
違う集合で測ると比較が成立しない（§4.4 の 30b）ので、分割を各モデルに配らない。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

import numpy as np

from bikechance_ml.eval.dataset import Samples
from bikechance_ml.features.arrays import Bools

#: 学習と検証のあいだに空ける日数（開発プラン §6.2）。
PURGE_DAYS: Final[int] = 1


class NotEnoughDaysError(ValueError):
    """学習・パージ・検証を切り分けられるだけの日数が無い。**黙って詰めない。**"""


@dataclass(frozen=True)
class DaySplit:
    """日の割り当て。**3 つは重ならない。**"""

    fit: tuple[date, ...]
    purge: tuple[date, ...]
    evaluate: tuple[date, ...]

    def used(self) -> tuple[date, ...]:
        """**当てはめと検証に使う日。** パージ日は読むが捨てるので入れない。

        入力の素性（天気の被覆など）を突き合わせるとき、捨てる日まで数えると
        **鳴かなくてよいところで鳴く**（`features/coverage.py`）。
        """
        return (*self.fit, *self.evaluate)

    def describe(self) -> str:
        return (
            f"学習 {_span(self.fit)}（{len(self.fit)} 日）/ "
            f"パージ {_span(self.purge)}（{len(self.purge)} 日）/ "
            f"検証 {_span(self.evaluate)}（{len(self.evaluate)} 日）"
        )


def split_days(days: Sequence[date], evaluate_days: int, purge_days: int = PURGE_DAYS) -> DaySplit:
    """**新しいほうから**検証・パージ・学習に割り当てる。

    未来で当てるのだから、検証は最後の日にする。学習が 1 日も残らなければ例外にする
    （足りないまま進むと、何を測ったのか分からない表が出る）。
    """
    ordered = tuple(sorted(set(days)))
    if evaluate_days < 1 or purge_days < 0:
        raise ValueError(f"検証 {evaluate_days} 日・パージ {purge_days} 日は指定できません")
    if len(ordered) < evaluate_days + purge_days + 1:
        raise NotEnoughDaysError(
            f"{len(ordered)} 日では足りません"
            f"（検証 {evaluate_days} 日、パージ {purge_days} 日、学習は 1 日以上が要る）"
        )
    evaluate = ordered[len(ordered) - evaluate_days :]
    purge = ordered[len(ordered) - evaluate_days - purge_days : len(ordered) - evaluate_days]
    return DaySplit(
        fit=ordered[: len(ordered) - evaluate_days - purge_days], purge=purge, evaluate=evaluate
    )


def mask_of(samples: Samples, days: Sequence[date]) -> Bools:
    """その日に属する行。"""
    wanted = np.array([one.toordinal() for one in days], dtype=np.int32)
    return np.isin(samples.day, wanted)


def _span(days: Sequence[date]) -> str:
    if not days:
        return "なし"
    if len(days) == 1:
        return days[0].isoformat()
    return f"{days[0].isoformat()}〜{days[-1].isoformat()}"
