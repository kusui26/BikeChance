"""門を当てて「配る形」を測る（W6 の PR F、W6-09・W6-13、契約 35・37）。

**v1 は LightGBM 単体では配らない。** セルごとに、LightGBM が門の相手（本番と同じ作り方の
B3。W6-10）を **10% 以上**改善したところだけ LightGBM を、他は B3 を配る（開発プラン §7.1、
W3-16）。この「配る形」の Brier が、v1 の本当の成績である。

**単位は 3 つ並べる**（M4 の文書 §1.4 ③。W6-09 で「水平単位にするかは本番の窓で比べて
から諮る」と決めた）。

  * 門なし … 全部 LightGBM（**上限の見当**。配らない形）
  * 水平単位 … system × ターゲット × 水平
  * **セル単位** … × 台数バケツ（**§7.1 の規則。配るのはこれ**）

**選んだ日で測ると良く見える。** 門を選んだ行と同じ行で配る形を測ると、たまたま良かった
セルを拾ったぶんだけ甘くなる。だから「**1 日目で選び、残りの日で測る**」も並べる（W6-14）。

**森を歩く行の割合は費用で重み付ける。** 森の費用は行 × 木 × 1 行 1 木の秒で、1 行 1 木の
秒はドコモのほうが 1.35 倍高い（M4 の文書 §1.3）。**台数の多いバケツは短い水平で判定の
対象外になり B3 に回る**ので、セル単位の門は規則を足さずに森を歩く行を減らす。

**ここは純粋である。** 読むのは検証の行（`Samples`）と、行ごとの確率だけ。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

import numpy as np

from bikechance_ml.eval.dataset import BUCKET_LABELS, TARGETS, Samples, Target, bucket_index
from bikechance_ml.features.arrays import Bools, Float64, Int16, Int64
from bikechance_ml.features.constants import HORIZONS_MIN

#: 採用基準（開発プラン §7.1、W3-16）。**門の相手の Brier を相対でこれ以上改善**したら通る。
MIN_IMPROVEMENT: Final[float] = 0.10

#: 相対改善を判定に使ってよい B0 の Brier の下限（W3-16）。**これ未満のセルは判定の対象外**で、
#: B3 を配る（Brier がほぼ 0 のところで相対 10% を求めるのは数値ノイズを追うのと同じ）。
JUDGEABLE_BRIER: Final[float] = 0.001

#: 門の相手と基準（`harness` の表の名前）。**相手は本番と同じ作り方の B3**（W6-10）。
REFERENCE: Final[str] = "B3"
PERSISTENCE: Final[str] = "B0"

#: 門の単位（報告書に並べる順）。
NO_GATE: Final[str] = "none"
HORIZON_GATE: Final[str] = "horizon"
CELL_GATE: Final[str] = "cell"
UNITS: Final[tuple[str, ...]] = (NO_GATE, HORIZON_GATE, CELL_GATE)
UNIT_LABELS: Final[Mapping[str, str]] = {
    NO_GATE: "門なし（全部 LightGBM）",
    HORIZON_GATE: "水平単位",
    CELL_GATE: "セル単位（§7.1。配るのはこれ）",
}

#: 門の理由。**判定の対象外**と**10% に届かない**は、どちらも B3 を配る（契約 35）。
PASSED: Final[str] = "passed"
BELOW: Final[str] = "below"
EXCLUDED: Final[str] = "excluded"

#: 1 行 1 木あたりの秒（**v0 の本番実測**。M4 の文書 §1.3）。**割合の重みにだけ使う**——
#: 絶対の秒は当てはめた機械で測る（報告書の ⑦）。
ROW_TREE_SECONDS: Final[Mapping[str, float]] = {
    "hellocycling": 0.738e-6,
    "docomo-cycle": 0.996e-6,
}

_HORIZONS: Final[Int64] = np.asarray(HORIZONS_MIN, dtype=np.int64)
_N_HORIZONS: Final[int] = len(HORIZONS_MIN)
_N_BUCKETS: Final[int] = len(BUCKET_LABELS)


class UnknownHorizonError(ValueError):
    """`HORIZONS_MIN` に無い水平の行があった。**別のセルに黙って入れない。**"""


class UnknownSystemError(ValueError):
    """1 行 1 木の秒を知らない系統の行があった。**重みを当て推量しない。**"""


@dataclass(frozen=True)
class Scored:
    """1 つの切り口（セルか水平）の、基準・相手・候補の重み付き Brier。"""

    system: str
    target: str
    h_min: int
    #: 台数バケツ。**水平単位なら None**
    bucket: str | None
    n: int
    weight: float
    b0: float
    b3: float
    model: float

    @property
    def improvement(self) -> float | None:
        """相手（B3）に対する相対改善。**相手の Brier が 0 なら測れない**（None）。"""
        return None if self.b3 <= 0.0 else (self.b3 - self.model) / self.b3

    @property
    def reason(self) -> str:
        """`passed`・`below`（10% に届かない）・`excluded`（判定の対象外）。"""
        if self.b0 < JUDGEABLE_BRIER:
            return EXCLUDED
        gain = self.improvement
        return PASSED if gain is not None and gain >= MIN_IMPROVEMENT else BELOW


@dataclass(frozen=True)
class Served:
    """門を当てた「配る形」の成績。"""

    #: 相手（B3）に対する相対改善。**system × ターゲットの組ごとに出し、平均する**
    improvement: float
    #: 森を歩く行の割合（**費用の重み付き**）
    forest_share: float


@dataclass(frozen=True)
class Judged:
    """1 つの候補に門を当てた結果（報告書の ①〜④）。"""

    model: str
    #: 検証日ぜんぶで測ったセル。**門の表はこれから作る**
    cells: tuple[Scored, ...]
    #: 検証日ぜんぶで測った水平
    horizons: tuple[Scored, ...]
    #: 単位 → 検証日ぜんぶで選んで測った配る形
    served: Mapping[str, Served]
    #: 単位 → 1 日目で選び、残りの日で測った配る形。**検証が 1 日なら空**
    holdout: Mapping[str, Served]

    def passing(self, unit: str) -> tuple[Scored, ...]:
        """門を越えた切り口（`cell` か `horizon`）。"""
        scored = self.cells if unit == CELL_GATE else self.horizons
        return tuple(one for one in scored if one.reason == PASSED)

    def judgeable(self, unit: str) -> tuple[Scored, ...]:
        """判定の対象になった切り口（B0 の Brier が下限以上）。"""
        scored = self.cells if unit == CELL_GATE else self.horizons
        return tuple(one for one in scored if one.reason != EXCLUDED)


#: ターゲット名 → モデル名 → 検証の行の確率（`harness.TargetFit.probability` の形）。
type Probabilities = Mapping[str, Mapping[str, Float64]]

#: 選ぶ行と測る行。**同じ集合なら「選んだ日で測る」**になる。
type Rows = tuple[Bools, Bools]


def judge(samples: Samples, probability: Probabilities, model: str, days: Sequence[date]) -> Judged:
    """候補 `model` に門を当てる。`samples` と確率は**検証の行**で、並びは同じである。"""
    every = np.ones(len(samples), dtype=np.bool_)
    split = holdout_masks(samples, days)
    return Judged(
        model=model,
        cells=score_units(samples, probability, model, CELL_GATE, every),
        horizons=score_units(samples, probability, model, HORIZON_GATE, every),
        served=_by_unit(samples, probability, model, (every, every)),
        holdout={} if split is None else _by_unit(samples, probability, model, split),
    )


def _by_unit(
    samples: Samples, probability: Probabilities, model: str, rows: Rows
) -> dict[str, Served]:
    return {unit: served(samples, probability, model, unit, rows) for unit in UNITS}


def holdout_masks(samples: Samples, days: Sequence[date]) -> Rows | None:
    """**1 日目で選び、残りの日で測る**ための 2 つの行の集合。検証が 1 日なら None。"""
    if len(days) < 2:
        return None
    first = np.asarray(samples.day == days[0].toordinal(), dtype=np.bool_)
    return first, np.asarray(~first, dtype=np.bool_)


# ── 切り口ごとの Brier ─────────────────────────────────────────
def horizon_index(h_min: Int16) -> Int64:
    """水平を 0 始まりの番号にする。**知らない水平があれば止める。**"""
    index = np.clip(np.searchsorted(_HORIZONS, h_min), 0, _N_HORIZONS - 1)
    if not bool(np.all(_HORIZONS[index] == h_min)):
        raise UnknownHorizonError(
            f"HORIZONS_MIN に無い水平があります: {sorted(set(h_min.tolist()))}"
        )
    return np.asarray(index, dtype=np.int64)


def unit_ids(samples: Samples, target: Target, unit: str) -> Int64:
    """行を切り口の番号にする。水平単位は `system × 水平`、セル単位は `× バケツ`。"""
    base = samples.system.astype(np.int64) * _N_HORIZONS + horizon_index(samples.h_min)
    if unit == HORIZON_GATE:
        return np.asarray(base, dtype=np.int64)
    buckets = bucket_index(samples.count_of(target)).astype(np.int64)
    return np.asarray(base * _N_BUCKETS + buckets, dtype=np.int64)


def _n_units(samples: Samples, unit: str) -> int:
    per_system = _N_HORIZONS if unit == HORIZON_GATE else _N_HORIZONS * _N_BUCKETS
    return len(samples.systems) * per_system


def score_units(
    samples: Samples, probability: Probabilities, model: str, unit: str, rows: Bools
) -> tuple[Scored, ...]:
    """`rows` の上で、切り口ごとに B0・B3・候補の重み付き Brier。**空の切り口は出さない**。"""
    return tuple(
        one
        for target in TARGETS
        for one in _score_target(samples, probability[target.name], model, target, unit, rows)
    )


def _score_target(
    samples: Samples,
    probability: Mapping[str, Float64],
    model: str,
    target: Target,
    unit: str,
    rows: Bools,
) -> list[Scored]:
    ids = unit_ids(samples, target, unit)[rows]
    size = _n_units(samples, unit)
    weight = samples.weight[rows].astype(np.float64)
    y = samples.y(target)[rows].astype(np.float64)
    count = np.bincount(ids, minlength=size)
    total = np.bincount(ids, weights=weight, minlength=size)
    brier = {
        name: _brier_by_unit(ids, probability[name][rows], y, weight, total, size)
        for name in (PERSISTENCE, REFERENCE, model)
    }
    return [
        _scored(samples, target, unit, int(key), int(count[key]), float(total[key]), brier, model)
        for key in np.nonzero(count)[0]
    ]


def _brier_by_unit(
    ids: Int64, p: Float64, y: Float64, weight: Float64, total: Float64, size: int
) -> Float64:
    """切り口ごとの重み付き Brier。**重みの和が 0 の切り口は 0**（呼ぶ側が出さない）。"""
    squared = np.bincount(ids, weights=np.square(p - y) * weight, minlength=size)
    return np.divide(squared, total, out=np.zeros(size, dtype=np.float64), where=total > 0)


def _scored(
    samples: Samples,
    target: Target,
    unit: str,
    key: int,
    n: int,
    weight: float,
    brier: Mapping[str, Float64],
    model: str,
) -> Scored:
    system, horizon, bucket = decode(key, unit)
    return Scored(
        system=samples.systems[system],
        target=target.name,
        h_min=HORIZONS_MIN[horizon],
        bucket=None if bucket is None else BUCKET_LABELS[bucket],
        n=n,
        weight=weight,
        b0=float(brier[PERSISTENCE][key]),
        b3=float(brier[REFERENCE][key]),
        model=float(brier[model][key]),
    )


def decode(key: int, unit: str) -> tuple[int, int, int | None]:
    """切り口の番号を `(system, 水平, バケツ)` の番号に戻す（`unit_ids` の逆）。"""
    if unit == HORIZON_GATE:
        return key // _N_HORIZONS, key % _N_HORIZONS, None
    return key // (_N_HORIZONS * _N_BUCKETS), (key // _N_BUCKETS) % _N_HORIZONS, key % _N_BUCKETS


# ── 配る形 ────────────────────────────────────────────────────
def routed(
    samples: Samples, probability: Probabilities, model: str, unit: str, chosen_on: Bools
) -> dict[str, Bools]:
    """ターゲットごとに、**森を歩く行**（`chosen_on` の行で門を決め、全部の行に当てる）。"""
    if unit == NO_GATE:
        return {one.name: np.ones(len(samples), dtype=np.bool_) for one in TARGETS}
    passing = [
        one
        for one in score_units(samples, probability, model, unit, chosen_on)
        if one.reason == PASSED
    ]
    return {target.name: _walks(samples, target, unit, passing) for target in TARGETS}


def _walks(samples: Samples, target: Target, unit: str, passing: Sequence[Scored]) -> Bools:
    keys = [encode(samples, one, unit) for one in passing if one.target == target.name]
    return np.asarray(np.isin(unit_ids(samples, target, unit), keys), dtype=np.bool_)


def encode(samples: Samples, one: Scored, unit: str) -> int:
    """`Scored` を切り口の番号に戻す（`decode` の逆）。"""
    base = samples.systems.index(one.system) * _N_HORIZONS + HORIZONS_MIN.index(one.h_min)
    if unit == HORIZON_GATE:
        return base
    if one.bucket is None:
        raise ValueError(f"セル単位なのにバケツがありません: {one}")
    return base * _N_BUCKETS + BUCKET_LABELS.index(one.bucket)


def served_probability(
    probability: Probabilities, model: str, walks: Mapping[str, Bools]
) -> dict[str, Float64]:
    """配る形の確率。**森を歩く行は候補、他は相手（B3）。**"""
    return {
        name: np.where(walks[name], values[model], values[REFERENCE])
        for name, values in probability.items()
    }


def served(
    samples: Samples, probability: Probabilities, model: str, unit: str, rows: Rows
) -> Served:
    """`rows[0]` で門を決め、`rows[1]` で配る形を測る。"""
    chosen_on, measured_on = rows
    walks = routed(samples, probability, model, unit, chosen_on)
    mixed = served_probability(probability, model, walks)
    return Served(
        improvement=improvement(samples, probability, mixed, measured_on),
        forest_share=forest_share(samples, walks, measured_on),
    )


def improvement(
    samples: Samples, probability: Probabilities, mixed: Mapping[str, Float64], rows: Bools
) -> float:
    """配る形の、相手（B3）に対する相対改善。**system × ターゲットの組ごとに出し、平均する。**

    行を全部まとめて 1 つの Brier にしないのは、行の多い HELLO に引かれるからである
    （M4 の文書 §1.4 の「4 組の平均」と同じ物差し）。
    """
    gains = [
        one
        for target in TARGETS
        for system in range(len(samples.systems))
        if (one := _gain(samples, probability, mixed, target, system, rows)) is not None
    ]
    return float(np.mean(gains)) if gains else 0.0


def _gain(
    samples: Samples,
    probability: Probabilities,
    mixed: Mapping[str, Float64],
    target: Target,
    system: int,
    rows: Bools,
) -> float | None:
    """1 組ぶんの相対改善。**行が無いか、相手の Brier が 0 なら None**（平均に入れない）。"""
    inside = np.asarray(rows & (samples.system == system), dtype=np.bool_)
    if not bool(inside.any()):
        return None
    y = samples.y(target)[inside].astype(np.float64)
    weight = samples.weight[inside].astype(np.float64)
    reference = _weighted_brier(probability[target.name][REFERENCE][inside], y, weight)
    if reference <= 0.0:
        return None
    return (reference - _weighted_brier(mixed[target.name][inside], y, weight)) / reference


def _weighted_brier(p: Float64, y: Float64, weight: Float64) -> float:
    return float((np.square(p - y) * weight).sum() / weight.sum())


def forest_share(samples: Samples, walks: Mapping[str, Bools], rows: Bools) -> float:
    """森を歩く行の割合。**抽出の重み（母集団へ戻す）× 系統の 1 行 1 木の秒**で重み付ける。"""
    cost = samples.weight.astype(np.float64) * system_costs(samples)
    total = sum(float(cost[rows].sum()) for _ in TARGETS)
    walking = sum(float(cost[rows & walks[one.name]].sum()) for one in TARGETS)
    return walking / total if total > 0 else 0.0


def system_costs(samples: Samples) -> Float64:
    """行ごとの 1 行 1 木の秒。**知らない系統があれば止める。**"""
    unknown = sorted(set(samples.systems) - set(ROW_TREE_SECONDS))
    if unknown:
        raise UnknownSystemError(f"1 行 1 木の秒を知らない系統です: {unknown}")
    per_system = np.asarray([ROW_TREE_SECONDS[one] for one in samples.systems], dtype=np.float64)
    return np.asarray(per_system[samples.system.astype(np.int64)], dtype=np.float64)
