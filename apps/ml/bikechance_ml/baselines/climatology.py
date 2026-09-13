"""B2：気候値（W3 プラン §9.6、W3-17）。

    P(y=1 | station, dow_type, slot15(t+h)) の推定値

「このポートの、この曜日種別の、この 15 分枠は、ふだんどうか」を当てる。**時刻は
`t + h`（到着時刻）で切る**：利用者が知りたいのは着いたときの状態である。

**セルのサンプル数が下限未満なら B1 に落とす。落とした割合を必ず報告する**（W3-17）。
蓄積が短いあいだ、`(station, dow_type, slot15)` の大半は 1 サンプルにしかならない。
1 サンプルの平均は 0 か 1 になり、気候値ではなく**その日の観測そのもの**になる。
落とした割合を出さないと、**B2 が実質 B1 であることに気づけない。**

**下限は「行数」ではなく「日数」で数える**（§12 の 101）。1 つの基準時刻と 10 の水平が
あるので、**同じ 1 日から同じセルに複数の行が入る**（`t + h` が同じ 15 分枠に落ちる）。
行数で数えると、1 日しか無くても下限を満たしてしまう：実測で、下限 2 行を満たした
176,331 セルは**すべて 1 日ぶんの観測**だった。気候値は「別の日にも同じことが起きるか」
なので、日をまたいでいないセルは使わない。

セルの数は 20,745 ポート × 3 種別 × 96 枠 = 597 万で、1 日のサンプル 205 万より多い。
**日数が足りているかは、この比で見当がつく。**
"""

from dataclasses import dataclass
from typing import Final

import numpy as np

from bikechance_ml.eval.dataset import Samples, Target
from bikechance_ml.features.arrays import Bools, Float64, Int32, Int64
from bikechance_ml.features.calendar import DOW_TYPE_ORDER

#: 1 日を 15 分で割った枠の数。
SLOTS_PER_DAY: Final[int] = 24 * 60 // 15

#: セルを使う最小サンプル数（W3-17）。1 サンプルの平均は「その日の観測」でしかない。
MIN_CELL_SAMPLES: Final[int] = 2

#: セルに寄与していなければならない**日数**（§12 の 101）。行数だけでは 1 日で満たせる。
MIN_CELL_DAYS: Final[int] = 2

_MINUTES_PER_DAY: Final[int] = 24 * 60


@dataclass(frozen=True)
class Table:
    """気候値の表と、その作り方の記録。"""

    n_ports: int
    rate: Float64
    total: Float64
    positive: Float64
    counted: Int64
    usable: Bools
    min_samples: int
    min_days: int

    @property
    def cells(self) -> int:
        """下限を満たしたセルの数。"""
        return int(self.usable.sum())


@dataclass(frozen=True)
class Applied:
    """当てはめた結果と、**B1 に落とした割合**。"""

    probability: Float64
    fell_back: int
    total: int

    @property
    def fallback_ratio(self) -> float:
        return 0.0 if self.total == 0 else self.fell_back / self.total


def slot15(samples: Samples) -> Int64:
    """到着時刻 `t + h` の 15 分枠（0〜95）。**日をまたいでも枠は 0 に戻る。**"""
    minute = (samples.minute_of_day.astype(np.int64) + samples.h_min) % _MINUTES_PER_DAY
    return np.asarray(minute // 15, dtype=np.int64)


def fit(
    samples: Samples,
    target: Target,
    keep: Bools,
    min_samples: int = MIN_CELL_SAMPLES,
    min_days: int = MIN_CELL_DAYS,
) -> Table:
    """学習期間の行からセルを作る。**下限に満たないセルは使えない印を付ける。**"""
    fitted = samples.take(keep)
    key = _key(fitted)
    size = samples.n_ports * len(DOW_TYPE_ORDER) * SLOTS_PER_DAY
    weight = fitted.weight.astype(np.float64)
    total = np.bincount(key, weights=weight, minlength=size)
    positive = np.bincount(key, weights=weight * fitted.y(target), minlength=size)
    counted = np.bincount(key, minlength=size)
    usable = np.asarray(
        (counted >= min_samples) & (_days_per_cell(key, fitted.day, size) >= min_days),
        dtype=np.bool_,
    )
    return Table(
        n_ports=samples.n_ports,
        rate=np.asarray(np.divide(positive, total, out=np.zeros(size), where=usable)),
        total=total,
        positive=positive,
        counted=np.asarray(counted, dtype=np.int64),
        usable=usable,
        min_samples=min_samples,
        min_days=min_days,
    )


def _days_per_cell(key: Int64, day: Int32, size: int) -> Int64:
    """セルごとに**寄与した日の数**を数える。

    `(セル, 日)` の組を一意化してからセルで数える。組の数は学習期間の行数を超えない
    ので、日数が増えても表は大きくならない。
    """
    days, day_index = np.unique(day, return_inverse=True)
    pairs = np.unique(key * len(days) + day_index)
    return np.asarray(np.bincount(pairs // len(days), minlength=size), dtype=np.int64)


def predict(table: Table, samples: Samples, fallback: Float64) -> Applied:
    """当てはめる。使えないセルは `fallback`（B1 の予測）に落とす。"""
    key, inside = _lookup(table, samples)
    usable = np.asarray(inside & table.usable[key], dtype=np.bool_)
    return Applied(
        probability=np.where(usable, table.rate[key], fallback),
        fell_back=int((~usable).sum()),
        total=len(samples),
    )


def predict_leave_one_out(
    table: Table, samples: Samples, target: Target, fallback: Float64
) -> Applied:
    """**その行自身を除いて**当てはめる。学習期間の行に使う（`conditional` と同じ理由）。

    気候値はセルが小さいので、自分を含めた推定は「その日の観測そのもの」になりやすい。
    自分を引いたあとサンプル数が下限に満たなくなるセルは `fallback`（B1）に落とす。
    """
    key, inside = _lookup(table, samples)
    weight = samples.weight.astype(np.float64)
    total = table.total[key] - weight
    positive = table.positive[key] - weight * samples.y(target)
    usable = inside & (table.counted[key] - 1 >= table.min_samples) & (total > 0)
    rate = np.divide(positive, total, out=fallback.copy(), where=usable)
    return Applied(
        probability=np.asarray(np.where(usable, rate, fallback)),
        fell_back=int((~usable).sum()),
        total=len(samples),
    )


def _lookup(table: Table, samples: Samples) -> tuple[Int64, Bools]:
    """引くセルの番号と、それが**表の中に収まっているか**を返す。

    **表に無いポートは番号が負で来る。** 配信では、成果物を作ったあとに現れたポートに
    `port = -1` が振られる（`jobs/infer.py` の `to_samples`）。`_key` はそれを
    `-288 〜 -1` にするが、**numpy の負インデックスは表の末尾に回り込む**ので、素直に
    引くと**最後のポートの気候値**が返る。本番で 5 ポートが別のポートの履歴を読み、
    しかも `confidence = 3`（気候値が効いた）として配られていた（§12 の 110）。

    範囲外は「使えないセル」として扱い、`predict` が `fallback`（B1）に落とす。**予測を
    出さなくする必要はない。** 新しいポートでも B1（システム × バケツ × 水平）は正しく
    出るので、気候値の層だけを外せばよい。

    番号は 0 に丸めて返す（引く先を有効な添字にしておく。値は `usable` で捨てる）。
    """
    key = _key(samples)
    inside = np.asarray((key >= 0) & (key < table.usable.size), dtype=np.bool_)
    return np.asarray(np.where(inside, key, 0), dtype=np.int64), inside


def _key(samples: Samples) -> Int64:
    """`(ポート, dow_type, slot15)` を 1 本の番号にする。

    ポートの番号は `(system_id, station_id)` の組から振ってある（`dataset.py`）。
    `station_id` だけで振ると、システムを跨いで衝突する 2,608 件が**同じセルに混ざる**。

    **表に無いポートの番号は負になる。** 引く前に `_lookup` を通すこと。
    """
    return np.asarray(
        (samples.port.astype(np.int64) * len(DOW_TYPE_ORDER) + samples.dow_type) * SLOTS_PER_DAY
        + slot15(samples),
        dtype=np.int64,
    )
