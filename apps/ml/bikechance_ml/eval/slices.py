"""評価の切り口（W3-16、W3 プラン §4.4 の 30）。

**総合の Brier は自明な行に支配される。** 全サンプルの約 54% は「3 台以上ある」行で、
そこは h=5 で Brier 0.0002 以下しかない（§10.1）。総合値だけで判定すると、
**アプリの価値がある領域（残り 0〜2 台）で負けていても合格する。**

だから **system × ターゲット × 水平 × 台数バケツ**で必ず割って出す。総合値も出すが、
それは全体の見当をつけるためのもので、合否は割ったほうで決める。

**切り口はここでだけ作る。** 各モデルが自分で行を選ぶと、比較が成立しなくなる
（§4.4 の 30b）。

**軸を足すのは、定義を変えるのと違う**（W5 プラン §6.3）。等幅から等頻度へ ECE を
変えると過去の数字と比べられなくなるが、**軸を足しても既存の数字は動かない**。
だから足すのは急がなくてよく、**要るときに足せばよい**。

いま在る軸（W5 の PR C で 2 つ足した）：

  * `overall`        system だけ
  * `by_horizon`     system × 水平
  * `by_bucket`      system × 台数バケツ（1 水平ぶん）。**合否はここ**
  * `by_dow_type`    system × **曜日種別**（W5 の PR D の効果はここに出る）
  * `by_time_of_day` system × **到着時刻の時間帯**

まだ無い軸と、その理由は下の `# ── まだ無い軸` を見ること。
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

import numpy as np

from bikechance_ml.eval.dataset import BUCKET_LABELS, Samples, Target, bucket_index
from bikechance_ml.features.arrays import Bools, Int16
from bikechance_ml.features.calendar import DOW_TYPE_ORDER, dow_type_name
from bikechance_ml.features.constants import HORIZONS_MIN

#: 「割らない」を表す印。表では「全体」と書く。
ALL = "全体"

#: 1 日を分けた時間帯（開発プラン §7.3）。**到着時刻（`t + h`）で切る**——利用者が
#: 知りたいのは着いたときの状態で、いつ問い合わせたかではない。
#: 値は「その時間帯の最初の時（含む）」で、次の要素の手前までが範囲になる。
TIME_BANDS: Final[tuple[tuple[str, int], ...]] = (
    ("深夜 0-5", 0),
    ("朝 6-9", 6),
    ("昼 10-15", 10),
    ("夕 16-19", 16),
    ("夜 20-23", 20),
)

_MINUTES_PER_DAY: Final[int] = 24 * 60


@dataclass(frozen=True)
class Slice:
    """1 つの切り口と、そこに入る行。

    `cut` は**軸を足したときの札**（曜日種別・時間帯など）。**どの軸かは `Outcome` の
    どの並びに入っているかで決まる**——`horizon` と `bucket` が既にそうなっている。
    """

    system: str
    target: str
    horizon: int | None
    bucket: str | None
    mask: Bools
    cut: str | None = None

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


def by_dow_type(samples: Samples, target: Target) -> Iterator[Slice]:
    """system × **曜日種別**（水平はまとめる）。**W5 の PR D の効果はここに出る。**

    **水平をまとめてよい。** 混ぜると自明な行（h=5）に引かれるが、**どの曜日種別も
    同じ割合で 10 水平を持つ**ので、種別どうしの比較は成立する（W3-16 が警告している
    のは「総合値だけで合否を決めること」で、同じ組成どうしを比べることではない）。

    **名前は `dow_type_name` を通す。** `DOW_TYPES[index]` と書くと 1 つずれる
    （W5 プラン §12 の 132）。
    """
    for system_index, system in enumerate(samples.systems):
        in_system = np.asarray(samples.system == system_index, dtype=np.bool_)
        for index in range(len(DOW_TYPE_ORDER)):
            mask = np.asarray(in_system & (samples.dow_type == index), dtype=np.bool_)
            yield Slice(system, target.name, None, None, mask, dow_type_name(index))


def by_time_of_day(samples: Samples, target: Target) -> Iterator[Slice]:
    """system × **到着時刻の時間帯**（水平はまとめる）。

    **切るのは `t + h`**（利用者が着く時刻）である。通勤の山と谷で当たり方が違う
    （開発プラン §7.3）。**`t` で切ると、朝に問い合わせて昼に着く行が「朝」に入る。**
    """
    hour = target_hour(samples)
    for system_index, system in enumerate(samples.systems):
        in_system = np.asarray(samples.system == system_index, dtype=np.bool_)
        for label, mask in _bands(hour):
            yield Slice(system, target.name, None, None, np.asarray(in_system & mask), label)


def target_hour(samples: Samples) -> Int16:
    """到着時刻（`t + h`）の JST の時。**日をまたいでも 0 に戻る。**"""
    minute = (samples.minute_of_day.astype(np.int32) + samples.h_min) % _MINUTES_PER_DAY
    return np.asarray(minute // 60, dtype=np.int16)


def _bands(hour: Int16) -> Iterator[tuple[str, Bools]]:
    """時間帯ごとの真偽。**最後の帯は 24 時まで**（次の帯が無いので閉じる）。"""
    starts = [one for _, one in TIME_BANDS]
    for index, (label, start) in enumerate(TIME_BANDS):
        end = starts[index + 1] if index + 1 < len(starts) else 24
        yield label, np.asarray((hour >= start) & (hour < end), dtype=np.bool_)


# ── まだ無い軸 ────────────────────────────────────────────────
# 開発プラン §7.3 は 9 つの切り口を挙げている。上に無いものと、その理由：
#
#   * **鮮度（`staleness_s`）** … `Samples` に列が無い。足すと**配信側
#     （`models/predictor.py`）も渡す義務を負う**——あちらは評価をしないので、
#     0 で埋めた嘘の列を持つことになる。**要るときに、持ち回りの形ごと決める**
#   * **プロファイルの日数（`prof_n_days`）** … 列そのものが `features/` に無い
#     （PR J で `FEATURE_SET` を v4 に上げるときに入る）。**空の軸を先に作らない**
#   * **都道府県** … `pref_code` は在るが `Samples` に読んでいない。上と同じ判断
#   * **再配置直後** … 検知規則（開発プラン §6.5）ごと未実装。W8
#
# **軸を足しても既存の数字は動かない**ので、これらは後から足してよい。
