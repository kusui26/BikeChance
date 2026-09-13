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
from typing import Final, Protocol

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
    """気候値の表と、その作り方の記録。**セルの番号は `cell_key` が決める。**

    `rate` 以外を持ち回るのは、**自分のぶんを引いたあとに下限を判定し直すため**である
    （`predict_without`）。配信では読まないので、成果物には書かない（`artifact.py`）。

    | 列 | `fit`（学習サンプルから） | `profile_climatology.fit`（プロファイルから） |
    |---|---|---|
    | `total` | 抽出重みの和（25 と 100） | **格子点の数**（重み無し） |
    | `positive` | 重み × ラベル の和 | ラベルが 1 の格子点の数 |
    | `counted` | 行数 | 格子点の数（`total` と同じ） |
    | `days` | セルに寄与した日数 | セルに寄与した日数 |

    **`days` を持つのは `usable` の根拠を捨てないため。** 件数だけでは 1 日で下限を
    満たせてしまう（§12 の 101）。引いたあとに判定し直すには日数も要る。
    """

    n_ports: int
    rate: Float64
    total: Float64
    positive: Float64
    counted: Int64
    #: セルに寄与した**日の数**。`min_days` と突き合わせる
    days: Int64
    usable: Bools
    min_samples: int
    min_days: int

    @property
    def cells(self) -> int:
        """下限を満たしたセルの数。"""
        return int(self.usable.sum())


@dataclass(frozen=True)
class Applied:
    """当てはめた結果と、**行ごとに気候値が引けたか**。

    **`used` を持つのは、確率を見比べて当てるのをやめるため。** 配信側は以前
    `b2.probability != b1` で判定していたが、**プロファイルから作った率はちょうど 1.0 に
    なることが多く**（実測：使えるセルの 53.1%）、B1 の 120 セルにも 1.0 が在る
    （実測：貸出 4・返却 6）。**両方が 1.0 の行を「引けなかった」と数える**
    （W5 プラン §12 の 144）。
    """

    probability: Float64
    #: 行ごとに気候値のセルが引けたか。**偽の行には `fallback` がそのまま入っている**
    used: Bools
    total: int

    @property
    def fell_back(self) -> int:
        """B1 に落とした行数。**`used` から数える**（二重に持たない）。"""
        return int((~self.used).sum())

    @property
    def fallback_ratio(self) -> float:
        return 0.0 if self.total == 0 else self.fell_back / self.total


@dataclass(frozen=True)
class Own:
    """当てはめる行が、**そのセルに自分で足しているぶん**。引いてから引き当てる。

    `fit` の表なら「自分の行 1 つ」（`own_row`）、プロファイルの表なら「**自分の日
    まるごと**」（`profile_climatology.own_day`）。**引く量が違うだけで、引き方は同じ**
    なので `predict_without` が 1 つあればよい。
    """

    total: Float64
    positive: Float64
    counted: Int64
    days: Int64


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
    return table_of(
        n_ports=samples.n_ports,
        total=np.bincount(key, weights=weight, minlength=size),
        positive=np.bincount(key, weights=weight * fitted.y(target), minlength=size),
        counted=np.asarray(np.bincount(key, minlength=size), dtype=np.int64),
        days=_days_per_cell(key, fitted.day, size),
        min_samples=min_samples,
        min_days=min_days,
    )


def table_of(
    *,
    n_ports: int,
    total: Float64,
    positive: Float64,
    counted: Int64,
    days: Int64,
    min_samples: int,
    min_days: int,
) -> Table:
    """数えた結果を表にする。**「使えるセル」と率の決め方はここだけ**（W5-01）。

    プロファイルから作るときも同じ関数を通る（`profile_climatology.fit`）。
    **2 つの作り方が 2 つの下限を持つと、報告した割合が配信と食い違う。**
    """
    usable = np.asarray((counted >= min_samples) & (days >= min_days), dtype=np.bool_)
    return Table(
        n_ports=n_ports,
        rate=np.asarray(np.divide(positive, total, out=np.zeros(len(total)), where=usable)),
        total=total,
        positive=positive,
        counted=counted,
        days=days,
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
    """当てはめる。使えないセルは `fallback`（B1 の予測）に落とす。**配信はこれだけ。**"""
    key, inside = _lookup(table, samples)
    return _applied(
        rate=table.rate[key],
        used=np.asarray(inside & table.usable[key], dtype=np.bool_),
        fallback=fallback,
    )


def predict_without(table: Table, samples: Samples, fallback: Float64, own: Own) -> Applied:
    """**自分のぶんを引いてから**当てはめる。**下限は引いたあとで判定し直す。**

    学習期間の行に当てはめるときに使う。そのまま当てはめると B1・B2 が「自分の答えを
    見た」推定になり、**B3 が両者を信じすぎる**（W3 プラン §12 の 102）。

    引く量は表の作り方で決まる（`Own`）。**下限（件数・日数）は引いたあとの残りで
    判定する**——残りが薄いセルは、配信のときに使えないセルと同じ扱いにする。

    **日数の下限もここで見る。** 以前は件数しか見ておらず、`predict`（配信）が使わない
    セル（同じ日に 3 行入っただけのセル）を leave-one-out が使っていた。**B3 は
    「配信では引けないセル」の値を入力に係数を決めていた**（W5 プラン §12 の 141）。
    """
    key, inside = _lookup(table, samples)
    total = table.total[key] - own.total
    used = np.asarray(
        inside
        & (table.counted[key] - own.counted >= table.min_samples)
        & (table.days[key] - own.days >= table.min_days)
        & (total > 0),
        dtype=np.bool_,
    )
    positive = table.positive[key] - own.positive
    return _applied(
        rate=np.asarray(np.divide(positive, total, out=np.zeros(len(total)), where=used)),
        used=used,
        fallback=fallback,
    )


def own_row(samples: Samples, target: Target) -> Own:
    """`fit` で作った表での「自分のぶん」。**その行 1 つ**（重みは抽出重み）。

    **日数は引かない。** 行を 1 つ抜いても、**同じ日の別の行が同じセルに残っている**
    ことがある（1 つの基準時刻と 10 の水平があるので、`t + h` が同じ枠に落ちる）。
    ここで引けるのは「1 行」だけで、日ごと引けるのはプロファイルのほうである。
    """
    weight = samples.weight.astype(np.float64)
    return Own(
        total=weight,
        positive=weight * samples.y(target),
        counted=np.ones(len(samples), dtype=np.int64),
        days=np.zeros(len(samples), dtype=np.int64),
    )


def predict_leave_one_out(
    table: Table, samples: Samples, target: Target, fallback: Float64
) -> Applied:
    """**その行自身を除いて**当てはめる（`conditional.predict_leave_one_out` と同じ理由）。

    気候値はセルが小さいので、自分を含めた推定は「その日の観測そのもの」になりやすい。
    """
    return predict_without(table, samples, fallback, own_row(samples, target))


def _applied(*, rate: Float64, used: Bools, fallback: Float64) -> Applied:
    """引けた行だけ気候値を使い、残りは `fallback`（B1）にする。**作るのはここだけ。**"""
    return Applied(
        probability=np.asarray(np.where(used, rate, fallback)),
        used=used,
        total=len(used),
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
    """学習サンプルの行から、引くセルの番号を作る。

    ポートの番号は `(system_id, station_id)` の組から振ってある（`dataset.py`）。
    `station_id` だけで振ると、システムを跨いで衝突する 2,608 件が**同じセルに混ざる**。

    **表に無いポートの番号は負になる。** 引く前に `_lookup` を通すこと。
    """
    return cell_key(
        samples.port.astype(np.int64), samples.dow_type.astype(np.int64), slot15(samples)
    )


def cell_key(port: Int64, dow_type: Int64, slot: Int64) -> Int64:
    """`(ポート, 曜日種別, 15 分枠)` を 1 本の番号にする。**式はここだけ**（W5-01）。

    プロファイルの行も同じ関数で番号にする（`profile_climatology`）。**2 か所で同じ式を
    書くと、片方を直したときに静かに別のセルを指す**（W5 プラン §12 の 132 と同じ形）。
    """
    return np.asarray(
        (port * len(DOW_TYPE_ORDER) + dow_type) * SLOTS_PER_DAY + slot, dtype=np.int64
    )


# ── B2 の作り方（学習側）──────────────────────────────────────
class Source(Protocol):
    """**B2 の表をどう作るか。** 学習サンプルの行からか、プロファイルからか。

    分かれるのは学習側の 3 つだけ——**表を作る・自分のぶんを引いて当てはめる・混合の
    当てはめに使える行**。**配信（`predict`）は分かれない**：表の形が同じだからで、
    「配信側の分岐を増やさない」は W5 プラン §6.4 の条件である。
    """

    def table(self, samples: Samples, target: Target, keep: Bools) -> Table:
        """当てはめた表。"""

    def leave_out(
        self, table: Table, samples: Samples, target: Target, fallback: Float64
    ) -> Applied:
        """**自分のぶんを引いて**当てはめた確率（B3 の入力）。"""

    def blend_rows(self, samples: Samples) -> Bools:
        """**混合の当てはめに使ってよい行。** 自分のぶんを引けない行は外す。"""

    def describe(self) -> str:
        """報告書に出す 1 行。**どこから作ったかが読めること。**"""


@dataclass(frozen=True)
class FromSamples:
    """**学習サンプルの行から作る**（W3 からのやり方）。プロファイルが無いときの既定。

    1 セル 1 日あたり 1.62 行（中央 1）しか無く、**下限 2 件 2 日を満たすセルが
    ほとんど無い**（実測。W5 プラン §2.3d）。プロファイルが在るなら
    `profile_climatology.FromProfile` を使う。
    """

    def table(self, samples: Samples, target: Target, keep: Bools) -> Table:
        return fit(samples, target, keep)

    def leave_out(
        self, table: Table, samples: Samples, target: Target, fallback: Float64
    ) -> Applied:
        return predict_leave_one_out(table, samples, target, fallback)

    def blend_rows(self, samples: Samples) -> Bools:
        """**全部使える。** 引くのは行 1 つで、どの行でも引ける。"""
        return np.ones(len(samples), dtype=np.bool_)

    def describe(self) -> str:
        return "学習サンプル（features/）"
