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

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Protocol

import numpy as np

from bikechance_ml.eval.dataset import Samples, Target
from bikechance_ml.features import profile
from bikechance_ml.features.arrays import Bools, Float64, Int32, Int64
from bikechance_ml.features.calendar import DOW_TYPE_ORDER

#: 1 日を 15 分で割った枠の数。
SLOTS_PER_DAY: Final[int] = 24 * 60 // 15

#: セルを使う最小の数（**プロファイルの格子点**。W3-17）。1 セルには 1 日 3 点入る
#: （`features/profile.py` の `GRID_POINTS_PER_SLOT`）ので、2 日で 6 点になる。
#: 1 点の平均は「その日その枠の観測」でしかない。
MIN_CELL_POINTS: Final[int] = 2

#: セルを使う最小の数（**学習サンプルの行**）。**2026-09-20 に 2 → 30 にした**（§12 の 167）。
#:
#: **同じ「2」でも、数えているものが違った。** プロファイルは 1 セルに 1 日 3 点入るが、
#: 抽出済みの行は **1 セル 1 日 0.3 行**しか来ない。2 行しかないセルの率は
#: **0・0.5・1 の 3 通り**しか取れず、気候値ではなく**その 2 行そのもの**である。
#:
#: **下限は「入れてよいか」ではなく「信じてよいか」で決める。** n 行から出した率の
#: 標準誤差は高々 `0.5/√n` で、**n = 25 で 0.1、n = 30 で 0.091**。B2 が B1 に足そうと
#: している差（ポートごとの癖）がその程度なので、**30 行より薄いセルは足せるものより
#: 誤差のほうが大きい**。
#:
#: **実測（2026-09-20、一様 1% の 9 日）**：学習 3 日で下限 2 にすると B3 は
#: **0.04332**（B1 は 0.04111）、6 日で下限 2 なら **0.04156**（B1 0.04117）、
#: 下限 5 でも **0.04215** と悪化する。**下限 10 以上では使えるセルがほぼ立たず、
#: B3 は 0.04112 で B1 をわずかに下回る**（混合が B1 を測り直すぶん）。
#:
#: **いまの抽出では 30 行に届くセルはまず無い**（6 日で 0 個）。つまりこの下限は
#: 「**プロファイルが無い日は気候値を作らない**」と言っているのに等しく、**それが
#: 正しい状態である**。抽出率が上がれば、同じ規則のまま自然に効き始める。
MIN_CELL_ROWS: Final[int] = 30

#: セルに寄与していなければならない**日数**（§12 の 101）。件数だけでは 1 日で満たせる。
#:
#: **2026-09-23 に 2 → 3 にした**（開発プラン §15 の D-30、W5 プラン §12 の 177）。
#: **2 日ぶんの B2 は、配らないほうが当たっていた。** 祝日（B2 が 2 日）に配った確率を、
#: B2 を落とした確率（`blend(B1, B1, h)`）に置き換えて同じ実測で測り直すと
#: **Brier −7.59%**（09-21 は −12.1%、09-22 は −6.0%、両系統とも同じ向き）。
#: 1 セル最大 6 点の率は `k/n` の 13 値しか取らず、76% のセルが 0 か 1 に張り付く
#: （§12 の 174）。**5 日ぶんの B2（平日）は落とすと +1.22%**——効いている。
#: **境目は 2 日と 5 日のあいだ**で、3 は「測って害だった 2 だけを外す最小の一歩」である。
#:
#: **下限を持つのはここ 1 か所。** プロファイル（`features/profile.py`）は切らずに
#: `n_days` を持ち、**下限は読む側が決める**（W5-03）。`build_profiles` の要約も
#: この値を渡されて数える——以前は同じ `2` がプロファイル側にも在った。
#:
#: **2026-09-25 から、これは平日の配る側の下限である**（D-37）。曜日種別ごとの形は
#: `SERVE_DAYS`、学習の行の下げ幅は `PROFILE_FIT_OFFSET_DAYS`。
MIN_CELL_DAYS: Final[int] = 3

#: 配る側の日数の下限。**曜日種別ごと**（W6 の PR A、D-37、契約 44）。`None` は**配らない**。
#:
#: **平日は 3**（D-30）——平日のセルは 10 日以上あって下限に当たらず、B2 は効いている
#: （09-24 の行で、落とすと +2.38%。W6 プラン §13.7）。**土日祝は 2026-09-28 に、9/27 の
#: 実測で 3・4・5・配らない から選ぶ**（W6 プラン §6.1 の基準。9/27 のデータを見る前に
#: 決めた）。**見せても 3 日の B2 は、落とした確率より悪かった**（EDA #8 §0.4 C）ので、
#: **決まるまでは配らない**——9/28 に測れなかったときも、この既定のまま当てはめ直せる。
SERVE_DAYS: Final[Mapping[str, int | None]] = MappingProxyType(
    {"sat": None, "sun_holiday": None, "weekday": MIN_CELL_DAYS}
)

#: 学習の行で下限を何日下げるか（**プロファイルから作るとき**。W5 プランの所見 179、契約 44）。
#:
#: leave-one-out は**自分の日を丸ごと**引くので、学習の行は 1 日薄い。配る側と同じ下限で
#: 判定すると、**日数がちょうど下限のセルだけが、混合に一度も見られないまま配られる**
#: （EDA #8：害の約 8 割がここから来ていた）。1 日下げれば、**配るセル ＝ 学習の行が
#: 引けるセル**になる。学習サンプルの行から作るとき（`FromSamples`）は引くのが行 1 つで、
#: 日数は減らないので 0 のままでよい。
PROFILE_FIT_OFFSET_DAYS: Final[int] = 1

#: 9/27 の実測で下限を選ぶ曜日種別（土日祝）。平日は `MIN_CELL_DAYS` のまま。
WEEKEND_DOW_TYPES: Final[tuple[str, ...]] = ("sat", "sun_holiday")

#: 「配らない」を日数で表すときの下限。**どのセルも届かない。**
_NEVER_DAYS: Final[int] = int(np.iinfo(np.int64).max)


class FloorError(ValueError):
    """下限の形が組めない。**曜日種別が揃っていない・日数が 1 日を切る**など。"""


@dataclass(frozen=True)
class DayFloor:
    """B2 のセルを使うのに要る**日数**（W6 の PR A、D-37）。

    **配る側は曜日種別ごと**（`serve`。`None` は配らない）で、**学習の行は `fit_offset` 日
    低く**判定する（`predict_without`）。`serve` は `DOW_TYPE_ORDER` の種別を全部持つ。

    **形を 1 つの物にするのは、表・成果物・報告書・点検が同じ形を読むため**である。
    以前は `min_days` という 1 つの数で、曜日種別も学習の行も同じ下限だった。
    """

    serve: Mapping[str, int | None]
    fit_offset: int = 0

    def __post_init__(self) -> None:
        _check_floor(self.serve, self.fit_offset)
        # 渡された辞書を後から書き換えられないように、読むだけの写しにする
        object.__setattr__(self, "serve", MappingProxyType(dict(self.serve)))

    @classmethod
    def uniform(cls, days: int, fit_offset: int = 0) -> "DayFloor":
        """**全曜日種別に同じ下限**（PR A より前の形。版 1 の成果物はこの形で読む）。"""
        return cls(serve=dict.fromkeys(DOW_TYPE_ORDER, days), fit_offset=fit_offset)

    def serve_by_dow(self) -> Int64:
        """`DOW_TYPE_ORDER` の並びの、配る側の下限。**配らない種別はどの日数も届かない。**"""
        return _by_dow(self.serve, 0)

    def fit_by_dow(self) -> Int64:
        """同じ並びの、学習の行の下限（配る側 − `fit_offset`）。"""
        return _by_dow(self.serve, self.fit_offset)

    @property
    def legacy_days(self) -> int:
        """**1 つの数にするなら**（成果物の `b2_min_days`）：配る種別のうち最も低い下限。

        「これより薄いセルはどの種別でも配らない」という意味で、1 つの数だった頃と矛盾しない。
        """
        return min(one for one in self.serve.values() if one is not None)

    def describe(self) -> str:
        """報告書の 1 行。**種別ごとの下限と、学習の行の下げ幅。**"""
        parts = [f"{dow} {_days_text(self.serve[dow])}" for dow in DOW_TYPE_ORDER]
        lowered = f"（学習の行は {self.fit_offset} 日低く）" if self.fit_offset else ""
        return "・".join(parts) + lowered


def weekend_floor(floor: DayFloor, days: int | None) -> DayFloor:
    """**土日祝の配る側だけ**を差し替えた形（平日と学習の行の下げ幅はそのまま）。

    9/27 の実測で比べる候補の版（3・4・5・配らない）を作るのに使う（W6 プラン §6.1）。
    """
    serve = {**floor.serve, **dict.fromkeys(WEEKEND_DOW_TYPES, days)}
    return DayFloor(serve=serve, fit_offset=floor.fit_offset)


def _check_floor(serve: Mapping[str, int | None], fit_offset: int) -> None:
    """**組めない形は作らない。** 種別の欠け・配る種別が 1 つも無い・1 日を切る学習側。"""
    if set(serve) != set(DOW_TYPE_ORDER):
        raise FloorError(
            f"下限は曜日種別 {DOW_TYPE_ORDER} を全部持つ（渡されたのは {sorted(serve)}）"
        )
    served = [one for one in serve.values() if one is not None]
    if not served:
        raise FloorError("どの曜日種別にも配らない下限は作れない")
    if fit_offset < 0 or min(served) - fit_offset < 1:
        raise FloorError(f"学習の行の下限が 1 日を切る（{dict(serve)}、{fit_offset} 日下げる）")


def _by_dow(serve: Mapping[str, int | None], offset: int) -> Int64:
    return np.asarray([_lowered(serve[dow], offset) for dow in DOW_TYPE_ORDER], dtype=np.int64)


def _lowered(days: int | None, offset: int) -> int:
    """下限を `offset` 日下げる。**配らない種別は、下げても届かないまま。**"""
    return _NEVER_DAYS if days is None else days - offset


def _days_text(days: int | None) -> str:
    return "配らない" if days is None else f"{days} 日"


#: 学習サンプルの行から作るときの既定。**引くのは行 1 つ**（日数は減らない）ので、
#: 学習の行を下げない。
SAMPLES_FLOOR: Final[DayFloor] = DayFloor(serve=SERVE_DAYS, fit_offset=0)

#: プロファイルから作るときの既定。**自分の日を丸ごと引く**ので、学習の行を 1 日下げる。
PROFILE_FLOOR: Final[DayFloor] = DayFloor(serve=SERVE_DAYS, fit_offset=PROFILE_FIT_OFFSET_DAYS)


@dataclass(frozen=True)
class Table:
    """気候値の表と、その作り方の記録。**セルの番号は `cell_key` が決める。**

    `rate` 以外を持ち回るのは、**自分のぶんを引いたあとに下限を判定し直すため**である
    （`predict_without`）。配信では読まないので、4 つの数（下の表）は成果物に書かない
    （`artifact.py`）。**下限（`min_samples`・`floor`）と厚さ（`max_days`）は記録として書く**
    ——どの下限・どの厚さで作った B2 かを、点検と測りが後から読む。

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
    #: セルに寄与した**日の数**。`floor` と突き合わせる
    days: Int64
    usable: Bools
    min_samples: int
    #: 日数の下限の形（D-37）。**`usable` はこの配る側の下限で決めてある**
    floor: DayFloor
    #: 曜日種別ごとの、セルに寄与した日数の最大（**当てはめたプロファイルの厚さ**）。
    #: 成果物に書いて、どの厚さで作った B2 かを後から読めるようにする。**知れなければ None**
    max_days: Mapping[str, int] | None = None

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
    """到着時刻 `t + h` の 15 分枠（0〜95）。**日をまたいでも枠は 0 に戻る。**

    **規則は `features/profile.target_slot` の 1 か所**（W5-01）。特徴量の `prof_*` も
    そこを通るので、**B2 と `prof_*` が別のセルを指すことが構造的に起きない**。
    """
    return profile.target_slot(samples.minute_of_day, samples.h_min)


def fit(
    samples: Samples,
    target: Target,
    keep: Bools,
    min_samples: int = MIN_CELL_ROWS,
    floor: DayFloor = SAMPLES_FLOOR,
) -> Table:
    """学習期間の**行**からセルを作る。**下限に満たないセルは使えない印を付ける。**

    既定は `MIN_CELL_ROWS`（**行**の下限）である——ここが数えるのは抽出された行で、
    プロファイルの格子点ではない（§12 の 167）。
    """
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
        floor=floor,
    )


def table_of(
    *,
    n_ports: int,
    total: Float64,
    positive: Float64,
    counted: Int64,
    days: Int64,
    min_samples: int,
    floor: DayFloor,
) -> Table:
    """数えた結果を表にする。**「使えるセル」と率の決め方はここだけ**（W5-01）。

    プロファイルから作るときも同じ関数を通る（`profile_climatology.fit`）。
    **2 つの作り方が 2 つの下限を持つと、報告した割合が配信と食い違う。**

    日数の下限は**曜日種別ごと**に当てる（D-37）。配らない種別のセルは 1 つも立たない。
    """
    usable = np.asarray(
        (counted >= min_samples) & meets_by_dow(days, floor.serve_by_dow()), dtype=np.bool_
    )
    return Table(
        n_ports=n_ports,
        rate=np.asarray(np.divide(positive, total, out=np.zeros(len(total)), where=usable)),
        total=total,
        positive=positive,
        counted=counted,
        days=days,
        usable=usable,
        min_samples=min_samples,
        floor=floor,
        max_days=max_days_by_dow(days),
    )


def meets_by_dow(days: Int64, need: Int64) -> Bools:
    """セルごとの日数が、**そのセルの曜日種別の下限**に届いているか。

    セルは `(ポート, 曜日種別, 15 分枠)` の順に並ぶ（`cell_key`）ので、3 次元に畳めば
    曜日種別の軸に下限を当てられる——セルと同じ長さの下限の配列を作らずに済む。
    """
    shaped = days.reshape(-1, len(DOW_TYPE_ORDER), SLOTS_PER_DAY)
    return np.asarray((shaped >= need[None, :, None]).reshape(-1), dtype=np.bool_)


def max_days_by_dow(days: Int64) -> dict[str, int]:
    """曜日種別ごとの、セルに寄与した日数の最大。**当てはめたプロファイルの厚さ**である。"""
    if days.size == 0:
        return dict.fromkeys(DOW_TYPE_ORDER, 0)
    most = days.reshape(-1, len(DOW_TYPE_ORDER), SLOTS_PER_DAY).max(axis=(0, 2))
    return {dow: int(most[index]) for index, dow in enumerate(DOW_TYPE_ORDER)}


def describe_thickness(max_days: Mapping[str, int] | None) -> str:
    """厚さの 1 行（当てはめの報告と点検）。**記録の無い表（PR A より前）はそう書く。**"""
    if max_days is None:
        return "記録なし"
    return "・".join(f"{dow} {max_days[dow]} 日" for dow in DOW_TYPE_ORDER)


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

    **学習の行は、配る側より `fit_offset` 日低い下限で判定する**（D-37）。プロファイルから
    作るときは自分の日を丸ごと引くので 1 日薄くなる——同じ下限だと、日数がちょうど
    下限のセルだけが混合に見られないまま配られる（W5 プランの所見 179）。

    **日数の下限もここで見る。** 以前は件数しか見ておらず、`predict`（配信）が使わない
    セル（同じ日に 3 行入っただけのセル）を leave-one-out が使っていた。**B3 は
    「配信では引けないセル」の値を入力に係数を決めていた**（W5 プラン §12 の 141）。
    """
    key, inside = _lookup(table, samples)
    total = table.total[key] - own.total
    # 曜日種別は行の到着の日のもの（`cell_key` と同じ `samples.dow_type`）
    need = table.floor.fit_by_dow()[samples.dow_type.astype(np.int64)]
    used = np.asarray(
        inside
        & (table.counted[key] - own.counted >= table.min_samples)
        & (table.days[key] - own.days >= need)
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


def dow_of_cell(key: Int64) -> Int64:
    """セルの番号から曜日種別の番号を取り出す。**`cell_key` の逆で、式はここだけ。**"""
    return np.asarray((key // SLOTS_PER_DAY) % len(DOW_TYPE_ORDER), dtype=np.int64)


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
    """**学習サンプルの行から作る**（W3 からのやり方）。プロファイルが無いときの退避路。

    **ここは「作れないときに作らない」ための道である**（§12 の 167）。抽出済みの行は
    1 セル 1 日 0.3 行しか来ないので、下限（`MIN_CELL_ROWS` ＝ 30 行）に届くセルは
    いまの抽出ではまず立たない——**実質すべて B1 に落ちる**。2〜9 行のセルを入れると
    **B3 が確実に悪くなる**ことは測ってある（実測 2026-09-20）。

    プロファイルが在るなら `profile_climatology.FromProfile` を使う（そちらは格子点を
    数えるので、2 日で 6 点入る）。
    """

    #: 行の下限。**変えられるようにしてあるのは測るためで、配る側は既定を使う**
    min_samples: int = MIN_CELL_ROWS
    #: 日数の下限の形。**引くのは行 1 つで日数は減らない**ので、学習の行を下げない
    floor: DayFloor = SAMPLES_FLOOR

    def table(self, samples: Samples, target: Target, keep: Bools) -> Table:
        return fit(samples, target, keep, min_samples=self.min_samples, floor=self.floor)

    def leave_out(
        self, table: Table, samples: Samples, target: Target, fallback: Float64
    ) -> Applied:
        return predict_leave_one_out(table, samples, target, fallback)

    def blend_rows(self, samples: Samples) -> Bools:
        """**全部使える。** 引くのは行 1 つで、どの行でも引ける。"""
        return np.ones(len(samples), dtype=np.bool_)

    def describe(self) -> str:
        """**下限も一緒に言う。** 報告書と `model_versions.metrics.climate` に残る。

        「学習サンプルから作った」だけでは、**どこまで信じた表か**が後から読めない
        （§12 の 166 で「作り方」を残すようにしたのと同じ理由）。
        """
        return f"学習サンプル（features/、下限 {self.min_samples} 行・{self.floor.describe()}）"
