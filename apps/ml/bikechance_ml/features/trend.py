"""ポートの「ふだんの傾向」（W9 プランの W9-01〜04、開発プランの D-47）。

**いまの台数を見ない、過去 28 日の統計である。** 来週の水曜も再来週の水曜も同じ数になるので、
画面では「予測」と書かず「ふだんの傾向」と書く（W9-06）。

**答えも過去の版も `daily.parquet`（1 日ぶんの素の集計。全格子点）から組む**（W9 プラン §7.2）。

  * **版 V の 1 時間の表** ＝ `daily(V−27 … V)` の和を 1 時間に畳んだもの。`profile(V)` と
    同じ和である（D-28 の `sum_dailies` と同じ定義）。版を 15 個落とすと 1 日 590 MB になる
    ところが、日次 43 個なら 100 MB で済む
  * **到着の日 T の答え** ＝ `daily(T)` を 1 時間に畳んだ「点の数」と「ある点の数」。学習
    サンプル（1% の抽出）ではなく全格子点で、**ラベルは同じ式**（`n_bike_ok` は `y_bike(t)`
    そのもの。W5-01）

1 時間 ＝ 15 分枠 4 つの和の比で、日数は 4 枠の最小（格子点の在る枠の中で）。**配る側の
下限**（平日 3 日・土日祝 4 日。`SERVE_DAYS`）に届かない時間は出さない。

**作り方**（P は写すプロファイルの日。`TrendWindows`）：

  1. **校正**：系統 × ターゲットごとに、「T−2 の版 → T の答え」（T ∈ [P−6, P]）で当てはめる。
     ロジスティック `sigmoid(a + b·logit(1 時間の率) + c·logit(ポートの平均))` を**平日と土日祝で
     分け**（土日祝のセルは 28 日に 4〜5 日しか無く、平日の 4 分の 1 の日数でばらつく）、その後に
     **単調な補正**（重み付きの PAV）を重ねる
  2. **門**：同じ作り方を 7 日前にずらして測る——T ∈ [P−13, P−7] で当てはめ、**P−7 の版に
     当てて P−6〜P の答え**で ECE と Brier を測る（先の日 0〜6 日。本番が P の版を P+1〜P+7 に
     出すのと同じ形）。**ECE < 0.03 で、ポートの平均より Brier が小さい組だけ出す**（W9-04）
  3. **写す**：P の版に当てる。門を通らなかった組は NULL

72 個の並びは **`DOW_TYPE_ORDER` × 0〜23 時**（`cell_index`。`fixtures/trend/layout_golden.json`
で TypeScript と突き合わせる）。

**ここは純粋な部分だけ。** Storage を読むのも DB に書くのも `jobs/build_trend.py`。
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from bikechance_ml.baselines.climatology import SERVE_DAYS
from bikechance_ml.eval import metrics
from bikechance_ml.features.arrays import Bools, Float32, Float64, Int8, Int16, Int64
from bikechance_ml.features.calendar import DOW_TYPE_ORDER
from bikechance_ml.features.profile import PROFILE_DAYS, SLOTS_PER_DAY

HOURS_PER_DAY: Final[int] = 24
#: 1 時間に入る 15 分枠の数
SLOTS_PER_HOUR: Final[int] = SLOTS_PER_DAY // HOURS_PER_DAY
#: 1 ポートの配列の長さ（曜日種別 3 × 24 時間）
CELLS: Final[int] = len(DOW_TYPE_ORDER) * HOURS_PER_DAY
TARGETS: Final[tuple[str, ...]] = ("bike", "dock")
#: 平日と傾きを分ける曜日種別（土曜・日曜祝日）
WEEKEND_DOW_TYPES: Final[frozenset[str]] = frozenset({"sat", "sun_holiday"})
#: 表に入れるときの倍率（`smallint` に収める。`station_forecasts` の `_x1000` と同じ）
SCALE: Final[int] = 1000
#: **出す組の上限**（W9-04。短期の森の配る基準と同じ。W6-15）
MAX_ECE: Final[float] = 0.03
#: logit の端。0 と 1 を避ける（1 時間の率が 0・1 のセルは少なくない）
LOGIT_EPS: Final[float] = 0.01
#: ロジスティック（ニュートン法）の反復の上限。**6 つの係数なので 10 回ほどで収まる**
LOGISTIC_STEPS: Final[int] = 30
#: 係数の動きがこれより小さくなったら止める
LOGISTIC_TOLERANCE: Final[float] = 1e-8
#: 正則化（ヘッセ行列の対角に足す）。係数が発散しないための歯止めで、値はほとんど動かさない
RIDGE: Final[float] = 1e-6
#: 単調な補正の前に、校正後の確率で行を並べて束ねる数（PAV を数百万行で回さない）
ISOTONIC_BINS: Final[int] = 1000

#: 当てはめに使う到着の日の数（P−6〜P）
FIT_DAYS: Final[int] = 7
#: 当てはめの行は「到着の何日前の版」で引くか（T−2 の版 ＝ 1 日先を出したとき）
LOOKUP_LAG_DAYS: Final[int] = 2
#: 門は何日前にずらして測るか（当てはめの窓と重ねない）
GATE_SHIFT_DAYS: Final[int] = 7

_ONE_DAY: Final[timedelta] = timedelta(days=1)
_NO_DAYS: Final[int] = np.iinfo(np.int64).max

#: 配る側の下限（日数）を 72 個に広げたもの。**`None`（配らない）は届かない値にする**
_FLOOR_DAYS: Final[Int64] = np.repeat(
    np.asarray(
        [_NO_DAYS if SERVE_DAYS[name] is None else SERVE_DAYS[name] for name in DOW_TYPE_ORDER],
        dtype=np.int64,
    ),
    HOURS_PER_DAY,
)

#: 72 個のうち土日祝のセル（`DOW_TYPE_ORDER` の並びで決まる）
_WEEKEND_CELLS: Final[Bools] = np.repeat(
    np.asarray([name in WEEKEND_DOW_TYPES for name in DOW_TYPE_ORDER], dtype=np.bool_),
    HOURS_PER_DAY,
)


def cell_index(dow_type: str, hour: int) -> int:
    """72 個の中の添字（`DOW_TYPE_ORDER` の番号 × 24 + 時）。**読む側（TypeScript）と同じ式。**"""
    return DOW_TYPE_ORDER.index(dow_type) * HOURS_PER_DAY + hour


# ── 日ごとの数（daily.parquet → 密な配列）────────────────────────
@dataclass(frozen=True)
class DayCounts:
    """1 日ぶんの素の集計を、ポート × 96 枠の密な配列にしたもの。並びは台帳（`Vocabulary`）。"""

    day: date
    #: その日の曜日種別の番号（`DOW_TYPE_ORDER`）。**1 日は 1 種別**
    dow: int
    #: 格子点の数（ポート × 96。1 枠は最大 3）
    n: Int16
    #: ターゲット → 「ある」と観測された格子点の数
    ok: Mapping[str, Int16]


@dataclass(frozen=True)
class Vocabulary:
    """ポートの台帳。**`system_id/station_id` の昇順**で、番号は配列の行。"""

    names: pa.Array

    @property
    def ports(self) -> tuple[tuple[str, str], ...]:
        return tuple(_split(str(one)) for one in self.names.to_pylist())

    def __len__(self) -> int:
        return len(self.names)


def vocabulary(tables: Iterable[pa.Table]) -> Vocabulary:
    """日次の表に現れたポートを、昇順の台帳にする。"""
    seen = [pc.unique(_names(table)) for table in tables]
    if not seen:
        return Vocabulary(pa.array([], type=pa.string()))
    merged = pc.unique(pa.concat_arrays(seen))
    return Vocabulary(pc.take(merged, pc.sort_indices(merged)))


def day_counts(day: date, table: pa.Table, ports: Vocabulary) -> DayCounts:
    """日次の表（`profile.DAILY_SCHEMA`）を密な配列にする。

    **台帳に無いポートと、同じ (ポート, 枠) の行が 2 つある日は止める**——密な配列に置くと
    後の行が前の行を黙って上書きし、点が消える（日次は 1 セル 1 行のはず。`build_day`）。
    """
    port = pc.index_in(_names(table), value_set=ports.names)
    if port.null_count:
        raise ValueError(f"{day} の日次に台帳に無いポートがある")
    rows = np.asarray(port.to_numpy(), dtype=np.int64)
    slot = np.asarray(table.column("slot15").to_numpy(), dtype=np.int64)
    if np.unique(rows * SLOTS_PER_DAY + slot).size != len(rows):
        raise ValueError(f"{day} の日次に同じ (ポート, 枠) の行が重なっている")
    return DayCounts(
        day=day,
        dow=_single_dow(day, table),
        n=_dense(len(ports), rows, slot, table.column("n")),
        ok={t: _dense(len(ports), rows, slot, table.column(f"n_{t}_ok")) for t in TARGETS},
    )


def _names(table: pa.Table) -> pa.ChunkedArray:
    return pc.binary_join_element_wise(table.column("system_id"), table.column("station_id"), "/")


def _split(name: str) -> tuple[str, str]:
    system_id, _, station_id = name.partition("/")
    return (system_id, station_id)


def _single_dow(day: date, table: pa.Table) -> int:
    """その日の曜日種別の番号。**2 種別以上・想定外の値は止める。**"""
    kinds = pc.unique(table.column("dow_type")).to_pylist()
    if len(kinds) != 1 or kinds[0] not in DOW_TYPE_ORDER:
        raise ValueError(f"{day} の日次の dow_type が 1 つでない: {kinds}")
    return DOW_TYPE_ORDER.index(str(kinds[0]))


def _dense(ports: int, rows: Int64, slot: Int64, values: pa.ChunkedArray) -> Int16:
    out = np.zeros((ports, SLOTS_PER_DAY), dtype=np.int16)
    out[rows, slot] = np.asarray(values.to_numpy(), dtype=np.int16)
    return out


# ── 版（28 日の和を 1 時間に畳む）と答え ─────────────────────────
@dataclass(frozen=True)
class HourlyProfile:
    """版 V の「曜日種別 × 24 時間」の和（ポート × 72）。`profile(V)` を 1 時間に畳んだもの。"""

    day: date
    n: Float64
    ok: Mapping[str, Float64]
    #: 4 枠の日数の最小（格子点の在る枠の中で）。**格子点の無い時間は 0**
    n_days: Int64

    def usable(self) -> Bools:
        """配る側の下限に届いた時間（ポート × 72）。"""
        return np.asarray((self.n > 0) & (self.n_days >= _FLOOR_DAYS), dtype=np.bool_)

    def rate(self, target: str) -> Float64:
        """1 時間の率（ポート × 72）。**下限に届かない時間は NaN。**"""
        with np.errstate(invalid="ignore", divide="ignore"):
            values = self.ok[target] / self.n
        return np.asarray(np.where(self.usable(), values, np.nan), dtype=np.float64)

    def port_rate(self, target: str) -> Float64:
        """ポートの平均（全セルの和の比。曜日・時刻を見ない）。格子点が無いポートは NaN。"""
        total = self.n.sum(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            values = self.ok[target].sum(axis=1) / total
        return np.asarray(np.where(total > 0, values, np.nan), dtype=np.float64)

    def observed(self) -> Bools:
        """窓の中に格子点が 1 つでも在ったポート（表に写すポート）。"""
        return np.asarray(self.n.sum(axis=1) > 0, dtype=np.bool_)


def window_days(day: date) -> tuple[date, ...]:
    """`profile(day)` に入る日（`day − 27 … day`）。`build_profiles.window_days` と同じ窓。"""
    return tuple(day - _ONE_DAY * back for back in range(PROFILE_DAYS - 1, -1, -1))


def hourly_profile(days: Mapping[date, DayCounts], version: date, ports: int) -> HourlyProfile:
    """版 V を日次の和から組む。**無い日は飛ばす**（足りないことは `n_days` に出る）。"""
    shape = (ports, len(DOW_TYPE_ORDER), SLOTS_PER_DAY)
    n, present = np.zeros(shape, dtype=np.int64), np.zeros(shape, dtype=np.int64)
    ok = {target: np.zeros(shape, dtype=np.int64) for target in TARGETS}
    for one in (days[d] for d in window_days(version) if d in days):
        n[:, one.dow, :] += one.n
        present[:, one.dow, :] += one.n > 0
        for target in TARGETS:
            ok[target][:, one.dow, :] += one.ok[target]
    return HourlyProfile(
        day=version,
        n=_fold_sum(n),
        ok={target: _fold_sum(values) for target, values in ok.items()},
        n_days=_fold_days(present),
    )


def _fold_sum(values: Int64) -> Float64:
    """(ポート × 3 × 96) → (ポート × 72)。4 枠の和。"""
    ports = values.shape[0]
    folded = values.reshape(ports, CELLS, SLOTS_PER_HOUR).sum(axis=2)
    return np.asarray(folded, dtype=np.float64)


def _fold_days(present: Int64) -> Int64:
    """4 枠の日数の最小。**格子点の無い枠は最小に入れない**（無い時間だけを 0 にする）。"""
    ports = present.shape[0]
    masked = np.where(present > 0, present, _NO_DAYS).reshape(ports, CELLS, SLOTS_PER_HOUR)
    smallest = masked.min(axis=2)
    return np.asarray(np.where(smallest == _NO_DAYS, 0, smallest), dtype=np.int64)


@dataclass(frozen=True)
class Outcome:
    """到着の日 T の答え（ポート × 24）。"""

    day: date
    dow: int
    n: Float64
    ok: Mapping[str, Float64]


def outcome(one: DayCounts) -> Outcome:
    """日次を 1 時間に畳む（点の数と「ある」点の数）。"""
    ports = one.n.shape[0]

    def fold(values: Int16) -> Float64:
        hours = values.reshape(ports, HOURS_PER_DAY, SLOTS_PER_HOUR).sum(axis=2)
        return np.asarray(hours, dtype=np.float64)

    return Outcome(one.day, one.dow, fold(one.n), {t: fold(v) for t, v in one.ok.items()})


# ── 校正の行（1 つの組ぶん。二項のまま持つ）──────────────────────
@dataclass(frozen=True)
class CalibrationRows:
    """校正の行。**1 行 ＝ (ポート, 到着の日, 時)** で、答えは率（`y`）と点の数（`weight`）。

    二項のまま持つ（0/1 に広げない）。ロジスティックの勾配も PAV の平均も、点の数で重みを
    付ければ 0/1 に広げた行と同じになる。**測るときだけ広げる**（`expanded`）。
    """

    rate: Float64
    port_rate: Float64
    weekend: Bools
    #: 「ある」点の割合（0〜1）
    y: Float64
    #: 格子点の数
    weight: Float64
    #: 到着の日（序数）。日ごとの成績に使う
    day: Int64

    def __len__(self) -> int:
        return len(self.y)


def pair_rows(
    arrival: Outcome, version: HourlyProfile, target: str, inside: Bools
) -> CalibrationRows:
    """「版 V → 到着の日 T」の行。`inside` はその系統のポート。**引けない行は落とす。**"""
    cells = arrival.dow * HOURS_PER_DAY + np.arange(HOURS_PER_DAY)
    rate = version.rate(target)[:, cells]
    port_rate = np.broadcast_to(version.port_rate(target)[:, None], rate.shape)
    keep = (arrival.n > 0) & np.isfinite(rate) & np.isfinite(port_rate) & inside[:, None]
    weight = arrival.n[keep]
    return CalibrationRows(
        rate=np.asarray(rate[keep], dtype=np.float64),
        port_rate=np.asarray(port_rate[keep], dtype=np.float64),
        weekend=np.full(len(weight), DOW_TYPE_ORDER[arrival.dow] in WEEKEND_DOW_TYPES),
        y=np.asarray(arrival.ok[target][keep] / weight, dtype=np.float64),
        weight=np.asarray(weight, dtype=np.float64),
        day=np.full(len(weight), arrival.day.toordinal(), dtype=np.int64),
    )


def concat_rows(parts: Sequence[CalibrationRows]) -> CalibrationRows:
    """日ごとの行をつなぐ。"""
    return CalibrationRows(
        rate=np.concatenate([one.rate for one in parts]),
        port_rate=np.concatenate([one.port_rate for one in parts]),
        weekend=np.concatenate([one.weekend for one in parts]),
        y=np.concatenate([one.y for one in parts]),
        weight=np.concatenate([one.weight for one in parts]),
        day=np.concatenate([one.day for one in parts]),
    )


# ── 校正 ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class Calibration:
    """系統 × ターゲットの校正。**ロジスティックの 6 つの係数と、単調な補正の折れ線。**"""

    coefficients: tuple[float, ...]
    knots_x: tuple[float, ...]
    knots_y: tuple[float, ...]

    def apply(self, rate: Float64, port_rate: Float64, weekend: Bools) -> Float64:
        """校正した確率。**NaN の行は NaN のまま**（下限に届かない時間）。"""
        logistic = _sigmoid(_features(rate, port_rate, weekend) @ np.asarray(self.coefficients))
        mapped = np.interp(logistic, np.asarray(self.knots_x), np.asarray(self.knots_y))
        return np.asarray(np.where(np.isfinite(logistic), mapped, np.nan), dtype=np.float64)

    def describe(self) -> dict[str, object]:
        """記録（`job_runs`）に残す形。折れ線は点の数だけ。"""
        return {
            "coefficients": [round(one, 4) for one in self.coefficients],
            "knots": len(self.knots_x),
        }


def logit(p: Float64) -> Float64:
    clipped = np.clip(p, LOGIT_EPS, 1 - LOGIT_EPS)
    return np.asarray(np.log(clipped / (1 - clipped)), dtype=np.float64)


def _sigmoid(z: Float64) -> Float64:
    return np.asarray(1 / (1 + np.exp(-z)), dtype=np.float64)


def _features(rate: Float64, port_rate: Float64, weekend: Bools) -> Float64:
    """`[1, logit(率), logit(平均)]` と、**土日祝だけ足す同じ 3 つ**（傾きを分ける）。"""
    base = [np.ones(len(rate)), logit(rate), logit(port_rate)]
    extra = [one * weekend for one in base]
    return np.asarray(np.column_stack([*base, *extra]), dtype=np.float64)


def fit_calibration(rows: CalibrationRows) -> Calibration:
    """当てはめる。**NaN の行は渡さない**（`pair_rows` が落としてある）。"""
    x = _features(rows.rate, rows.port_rate, rows.weekend)
    beta = _fit_logistic(x, rows.y, rows.weight)
    knots_x, knots_y = isotonic_knots(_sigmoid(x @ beta), rows.y, rows.weight)
    return Calibration(
        coefficients=tuple(float(one) for one in beta),
        knots_x=tuple(knots_x.tolist()),
        knots_y=tuple(knots_y.tolist()),
    )


def _fit_logistic(x: Float64, y: Float64, w: Float64) -> Float64:
    """重み付きのロジスティック（ニュートン法）。`y` は 0〜1 の率でよい（二項の尤度）。"""
    beta = np.zeros(x.shape[1], dtype=np.float64)
    for _ in range(LOGISTIC_STEPS):
        p = _sigmoid(x @ beta)
        gradient = x.T @ (w * (y - p))
        hessian = (x * (w * p * (1 - p))[:, None]).T @ x + RIDGE * np.eye(x.shape[1])
        step = np.linalg.solve(hessian, gradient)
        beta = beta + step
        if float(np.max(np.abs(step))) < LOGISTIC_TOLERANCE:
            break
    return np.asarray(beta, dtype=np.float64)


def isotonic_knots(p: Float64, y: Float64, w: Float64) -> tuple[Float64, Float64]:
    """**単調な補正の折れ線。** `p` の順に重みで等分して束ね（`ISOTONIC_BINS`）、PAV で
    単調にする。"""
    order = np.argsort(p, kind="stable")
    cumulative = np.cumsum(w[order])
    edges = np.searchsorted(cumulative, np.linspace(0, cumulative[-1], ISOTONIC_BINS + 1)[1:-1])
    groups = [one for one in np.split(order, edges) if one.size]
    weight = np.asarray([w[one].sum() for one in groups], dtype=np.float64)
    x = np.asarray([np.average(p[one], weights=w[one]) for one in groups], dtype=np.float64)
    mean = np.asarray([np.average(y[one], weights=w[one]) for one in groups], dtype=np.float64)
    return x, pav(mean, weight)


def pav(values: Float64, weights: Float64) -> Float64:
    """重み付きの PAV（pool adjacent violators）。**単調に増える**列を、入力と同じ長さで返す。"""
    blocks: list[list[float]] = []  # [値, 重み, 束ねた数]
    for value, weight in zip(values.tolist(), weights.tolist(), strict=True):
        blocks.append([value, weight, 1.0])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            right, left = blocks.pop(), blocks.pop()
            total = left[1] + right[1]
            blocks.append(
                [(left[0] * left[1] + right[0] * right[1]) / total, total, left[2] + right[2]]
            )
    return np.asarray(
        [value for value, _, count in blocks for _ in range(int(count))], dtype=np.float64
    )


# ── 門（7 日前にずらして測る）────────────────────────────────────
@dataclass(frozen=True)
class Scores:
    """測った成績（0/1 に広げた格子点で）。"""

    ece: float
    brier: float
    #: 比べる相手：ポートの平均（曜日・時刻を見ない）
    brier_port: float
    #: 到着の日ごとの ECE（雨の日のような 1 日のずれを読むため）
    ece_by_day: Mapping[date, float]


def expanded(rows: CalibrationRows, p: Float64) -> tuple[Int8, Float64, Float32]:
    """測るために 0/1 に広げる：**「ある」点と「無い」点の 2 行**にし、重みを点の数にする。"""
    ok = rows.y * rows.weight
    weight = np.concatenate([ok, rows.weight - ok])
    keep = weight > 0
    y = np.concatenate([np.ones(len(rows)), np.zeros(len(rows))]).astype(np.int8)[keep]
    return y, np.concatenate([p, p])[keep], weight[keep].astype(np.float32)


def score(rows: CalibrationRows, p: Float64) -> Scores:
    """校正した確率 `p` の成績。**物差しは短期の森と同じ**（`metrics.ece` の等頻度 15）。"""
    y, p_all, w = expanded(rows, p)
    _, port_all, _ = expanded(rows, rows.port_rate)
    return Scores(
        ece=metrics.ece(y, p_all, w),
        brier=metrics.brier(y, p_all, w),
        brier_port=metrics.brier(y, port_all, w),
        ece_by_day={
            date.fromordinal(day): metrics.ece(*expanded(_only_day(rows, day), p[rows.day == day]))
            for day in np.unique(rows.day).tolist()
        },
    )


def _only_day(rows: CalibrationRows, day: int) -> CalibrationRows:
    inside = rows.day == day
    return CalibrationRows(
        rate=rows.rate[inside],
        port_rate=rows.port_rate[inside],
        weekend=rows.weekend[inside],
        y=rows.y[inside],
        weight=rows.weight[inside],
        day=rows.day[inside],
    )


@dataclass(frozen=True)
class Judged:
    """1 つの組（系統 × ターゲット）の、**写す校正**と**門の成績**。"""

    calibration: Calibration
    gate: Scores
    rows_fit: int
    rows_gate: int

    @property
    def passes(self) -> bool:
        """出してよいか（W9-04）。**ECE が上限未満で、ポートの平均より良い**。Python の `bool`。"""
        return bool(self.gate.ece < MAX_ECE and self.gate.brier < self.gate.brier_port)

    def describe(self) -> dict[str, object]:
        return {
            **self.calibration.describe(),
            "ece": round(self.gate.ece, 4),
            "brier": round(self.gate.brier, 5),
            "brier_port_mean": round(self.gate.brier_port, 5),
            "ece_by_day": {str(k): round(v, 4) for k, v in self.gate.ece_by_day.items()},
            "rows_fit": self.rows_fit,
            "rows_gate": self.rows_gate,
            "passes": self.passes,
        }


def judge(serve: CalibrationRows, gate_fit: CalibrationRows, gate_test: CalibrationRows) -> Judged:
    """写す校正を当てはめ、**同じ作り方を 7 日前にずらしたもの**で門を測る。"""
    shifted = fit_calibration(gate_fit)
    p = shifted.apply(gate_test.rate, gate_test.port_rate, gate_test.weekend)
    return Judged(
        calibration=fit_calibration(serve),
        gate=score(gate_test, p),
        rows_fit=len(serve),
        rows_gate=len(gate_test),
    )


# ── 窓（P から決まる日付の組）────────────────────────────────────
@dataclass(frozen=True)
class TrendWindows:
    """写す日 P から決まる「到着の日 → 引く版」の組。**全部 P の日次までで閉じる。**"""

    profile_day: date

    def serve(self) -> tuple[tuple[date, date], ...]:
        """当てはめ：T ∈ [P−6, P]、版は T−2。"""
        return _lagged_pairs(self.profile_day)

    def gate_fit(self) -> tuple[tuple[date, date], ...]:
        """門の当てはめ：T ∈ [P−13, P−7]、版は T−2。"""
        return _lagged_pairs(self.profile_day - _ONE_DAY * GATE_SHIFT_DAYS)

    def gate_test(self) -> tuple[tuple[date, date], ...]:
        """門の測り：T ∈ [P−6, P]、**版は P−7**（先の日 0〜6 日）。"""
        version = self.profile_day - _ONE_DAY * GATE_SHIFT_DAYS
        return tuple((arrival, version) for arrival, _ in self.serve())

    def versions(self) -> tuple[date, ...]:
        """要る版（P 自身を含む）。"""
        pairs = (*self.serve(), *self.gate_fit(), *self.gate_test())
        return tuple(sorted({version for _, version in pairs} | {self.profile_day}))

    def daily_days(self) -> tuple[date, ...]:
        """要る日次（いちばん古い版の窓の頭から P まで）。"""
        first = min(self.versions()) - _ONE_DAY * (PROFILE_DAYS - 1)
        return tuple(first + _ONE_DAY * k for k in range((self.profile_day - first).days + 1))


def _lagged_pairs(last: date) -> tuple[tuple[date, date], ...]:
    """到着の日 `last − 6 … last` と、それぞれの `LOOKUP_LAG_DAYS` 日前の版。"""
    arrivals = (last - _ONE_DAY * back for back in range(FIT_DAYS - 1, -1, -1))
    return tuple((one, one - _ONE_DAY * LOOKUP_LAG_DAYS) for one in arrivals)


# ── 表の行 ────────────────────────────────────────────────────
def trend_rows(
    hourly: HourlyProfile,
    ports: Sequence[tuple[str, str]],
    judged: Mapping[tuple[str, str], Judged],
) -> list[dict[str, object]]:
    """`station_trend` の行（`upsert_station_trend` が受ける形）。**窓に観測の在るポートだけ。**

    **出さない組（門を通らない・当てはめが無い）は、その系統のその配列を全部 NULL にする。**
    下限に届かない時間も NULL。
    """
    values = {target: _calibrated(hourly, ports, judged, target) for target in TARGETS}
    n_days = hourly.n_days.tolist()
    return [
        {
            "system_id": ports[index][0],
            "station_id": ports[index][1],
            "profile_date": hourly.day.isoformat(),
            **{f"p_{target}_x1000": values[target][index] for target in TARGETS},
            "n_days": [int(one) for one in n_days[index]],
        }
        for index in np.flatnonzero(hourly.observed()).tolist()
    ]


def _calibrated(
    hourly: HourlyProfile,
    ports: Sequence[tuple[str, str]],
    judged: Mapping[tuple[str, str], Judged],
    target: str,
) -> list[list[int | None]]:
    """ポートごとの 72 個（`x1000` の整数か None）。**系統ごとにまとめて校正する。**"""
    values = np.full((len(ports), CELLS), np.nan, dtype=np.float64)
    systems = np.asarray([system_id for system_id, _ in ports])
    rate, port_rate = hourly.rate(target), hourly.port_rate(target)
    for system_id in np.unique(systems).tolist():
        one = judged.get((system_id, target))
        if one is None or not one.passes:
            continue
        inside = systems == system_id
        count = int(inside.sum())
        values[inside] = one.calibration.apply(
            rate[inside].ravel(),
            np.repeat(port_rate[inside], CELLS),
            np.tile(_WEEKEND_CELLS, count),
        ).reshape(count, CELLS)
    scaled = np.rint(values * SCALE)
    return [[None if np.isnan(v) else int(v) for v in row] for row in scaled.tolist()]
