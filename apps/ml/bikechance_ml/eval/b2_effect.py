"""B2 が効いているか（W6 の PR A、D-37、契約 44）。**純粋な部分だけ**を持つ。

**B3（配る確率）と「B2 を落とした確率」を、同じ行で比べる**（EDA #7 §6・EDA #8 と同じ物差し）。
落とした確率は、同じ成果物の係数で `blend(B1, B1, h)` を出したもの——**B2 が引けないセルで
配信が出す値そのもの**である。だから「落とすと悪くなる」なら、その B2 は配る価値がある。

**判定の基準は W6 プラン §6.1 に、9/27 のデータを見る前に書いた。** ここはそれを写しただけで、
**基準をここで変えない**：

1. 全体（行 × ターゲットを重みで合わせた Brier）で「落とすと」が正で、**ポートを単位にした
   ブートストラップ**（系統ごとに引き直す・1,000 回・種 20260927）の 95% 区間の下限も正
2. system × ターゲットの 4 組のうち 3 組以上で、B3 の Brier が落とした確率以下

下限 K は、候補 k のうち **k 以上がすべて「効いた」になる最小の k**。無ければ配らない。

**ポートを単位に引き直す**のは、同じポートの行（水平・基準の時刻）が互いに似ていて、行を
独立とみなすと区間が狭く出すぎるからである。**日による揺れは区間に入らない**（1 日しか無い）。
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from bikechance_ml.baselines import blend, climatology, conditional
from bikechance_ml.baselines.artifact import Artifact, TargetModel
from bikechance_ml.eval import metrics
from bikechance_ml.eval.dataset import TARGETS, Samples, Target
from bikechance_ml.features.arrays import Bools, Float32, Float64, Int8, Strings
from bikechance_ml.models.predictor import to_samples

#: 「効いた」とするのに要る、B3 が落とした確率以下だった組の数（4 組のうち。基準の 2）。
MIN_PAIRS_NOT_WORSE: Final[int] = 3

#: ポート単位のブートストラップの回数と種（基準の 1。**変えると 9/25 に決めた基準から外れる**）。
BOOTSTRAP_REPS: Final[int] = 1_000
BOOTSTRAP_SEED: Final[int] = 20260927

#: 95% 区間の端（百分位）。
INTERVAL_PERCENTILES: Final[tuple[float, float]] = (2.5, 97.5)


@dataclass(frozen=True)
class Pair:
    """1 つの system × ターゲットの測り。**Brier は重み付き。**"""

    system: str
    target: str
    rows: int
    #: B2 が引けた行の割合（引けない行は B3 と落とした確率が同じになる）
    used: float
    b1: float
    b3: float
    dropped: float
    ece_b3: float
    ece_dropped: float

    @property
    def gain(self) -> float:
        """落とすと：（落とした Brier − B3 の Brier）÷ B3 の Brier。**正なら B2 が効いている。**"""
        return _relative(self.dropped, self.b3)


@dataclass(frozen=True)
class Effect:
    """1 つの成果物を、1 日の行に当てた測り。"""

    pairs: tuple[Pair, ...]
    #: 全体の「落とすと」
    gain: float
    #: ポート単位のブートストラップの 95% 区間
    low: float
    high: float
    #: 全体の B3 の B1 との差（（B3 − B1）÷ B1。EDA #8 の「B1 との差」）
    versus_b1: float

    @property
    def pairs_not_worse(self) -> int:
        """B3 の Brier が「B2 を落とした確率」以下だった組の数。"""
        return sum(1 for one in self.pairs if one.b3 <= one.dropped)

    @property
    def helped(self) -> bool:
        """**「効いた」**（W6 プラン §6.1 の 1 と 2 を両方満たす）。"""
        return self.gain > 0 and self.low > 0 and self.pairs_not_worse >= MIN_PAIRS_NOT_WORSE


def choose_floor(effects: Mapping[int, Effect]) -> int | None:
    """**k 以上がすべて「効いた」になる最小の k。** 無ければ None（配らない）。

    揃わない結果（3 が効いて 4 が効かない など）は、厚いほうに寄せる。
    """
    candidates = sorted(effects)
    for k in candidates:
        if all(effects[other].helped for other in candidates if other >= k):
            return k
    return None


def arriving_on(table: pa.Table, dow_type: str) -> pa.Table:
    """**到着時刻の曜日種別**が `dow_type` の行だけ（日をまたいで別の種別に着く行は除く）。"""
    return table.filter(pc.equal(table.column("target_dow_type"), dow_type))


def measure(artifact: Artifact, table: pa.Table, at: datetime) -> Effect:
    """成果物を学習サンプルの行（ラベルつき）に当て、**B3 と落とした確率を同じ行で比べる。**

    `at` は `to_samples` に渡す基準の時刻で、B1・B2・B3 の値には効かない（日を読まない）。
    """
    scored = [
        one for system in artifact.systems for one in _score_system(artifact, system, table, at)
    ]
    return _summarize(tuple(pair for pair, _ in scored), [errors for _, errors in scored])


@dataclass(frozen=True)
class RowErrors:
    """1 組ぶんの、行ごとの重み付き二乗誤差。**ポートで束ねて引き直す。**"""

    system: str
    station: Strings
    b1: Float64
    b3: Float64
    dropped: Float64


def _score_system(
    artifact: Artifact, system: str, table: pa.Table, at: datetime
) -> Iterator[tuple[Pair, RowErrors]]:
    part = table.filter(pc.equal(table.column("system_id"), system))
    if part.num_rows == 0:
        return
    samples = to_samples(artifact, system, at, part)
    stations = np.asarray(part.column("station_id").to_pylist(), dtype=np.str_)
    weight = np.asarray(part.column("weight").to_numpy(zero_copy_only=False), dtype=np.float32)
    for target in TARGETS:
        y = np.asarray(part.column(target.label).to_numpy(zero_copy_only=False), dtype=np.int8)
        model = artifact.targets[target.name]
        yield _score_target(model, samples, target, (system, stations, y, weight))


@dataclass(frozen=True)
class _Predicted:
    """1 組ぶんの 3 つの確率と、B2 が引けたか。"""

    b1: Float64
    b3: Float64
    dropped: Float64
    used: Bools


def _predict(model: TargetModel, samples: Samples, target: Target) -> _Predicted:
    """**配信と同じ関数**（`conditional` → `climatology` → `blend`）で出す。落とした確率は
    同じ係数に B1 を 2 度渡したもの——B2 が引けないセルで配信が出す値である。
    """
    b1, _ = conditional.predict(model.b1, samples, target)
    b2 = climatology.predict(model.b2, samples, b1)
    return _Predicted(
        b1=b1,
        b3=blend.predict(model.b3, blend.design(b1, b2.probability, samples.h_min)),
        dropped=blend.predict(model.b3, blend.design(b1, b1, samples.h_min)),
        used=b2.used,
    )


def _score_target(
    model: TargetModel,
    samples: Samples,
    target: Target,
    rows: tuple[str, Strings, Int8, Float32],
) -> tuple[Pair, RowErrors]:
    system, stations, y, weight = rows
    got = _predict(model, samples, target)
    errors = RowErrors(
        system=system,
        station=stations,
        b1=_sq(got.b1, y, weight),
        b3=_sq(got.b3, y, weight),
        dropped=_sq(got.dropped, y, weight),
    )
    return _pair(target, got, errors, (y, weight)), errors


def _pair(
    target: Target, got: _Predicted, errors: RowErrors, labelled: tuple[Int8, Float32]
) -> Pair:
    """1 組の重み付き Brier と ECE。**Brier は行ごとの二乗誤差の和を、重みの和で割る。**"""
    y, weight = labelled
    total = float(weight.sum())
    return Pair(
        system=errors.system,
        target=target.name,
        rows=len(y),
        used=float(got.used.mean()) if len(y) else 0.0,
        b1=float(errors.b1.sum()) / total,
        b3=float(errors.b3.sum()) / total,
        dropped=float(errors.dropped.sum()) / total,
        ece_b3=metrics.ece(y, got.b3, weight),
        ece_dropped=metrics.ece(y, got.dropped, weight),
    )


def _summarize(pairs: tuple[Pair, ...], errors: Sequence[RowErrors]) -> Effect:
    b1 = sum(float(one.b1.sum()) for one in errors)
    b3 = sum(float(one.b3.sum()) for one in errors)
    dropped = sum(float(one.dropped.sum()) for one in errors)
    low, high = bootstrap_interval(port_sums(errors))
    return Effect(
        pairs=pairs,
        gain=_relative(dropped, b3),
        low=low,
        high=high,
        versus_b1=_relative(b3, b1),
    )


def port_sums(errors: Sequence[RowErrors]) -> list[tuple[Float64, Float64]]:
    """系統ごとに、**ポートごとの** B3 と落とした確率の二乗誤差の和（両ターゲットを合わせる）。

    系統は現れた順（成果物の `systems` の並び）、ポートは名前の昇順に並べる——引き直しの
    並びを決めておかないと、同じ種でも区間が変わる。
    """
    grouped: list[tuple[Float64, Float64]] = []
    for system in dict.fromkeys(one.system for one in errors):
        parts = [one for one in errors if one.system == system]
        stations = np.concatenate([one.station for one in parts])
        names, index = np.unique(stations, return_inverse=True)
        b3 = np.bincount(
            index, weights=np.concatenate([one.b3 for one in parts]), minlength=len(names)
        )
        dropped = np.bincount(
            index, weights=np.concatenate([one.dropped for one in parts]), minlength=len(names)
        )
        grouped.append((np.asarray(b3, dtype=np.float64), np.asarray(dropped, dtype=np.float64)))
    return grouped


def bootstrap_interval(
    groups: Sequence[tuple[Float64, Float64]],
    reps: int = BOOTSTRAP_REPS,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float]:
    """**ポートを単位に、系統ごとに引き直した**「落とすと」の 95% 区間。**種で決まる。**"""
    rng = np.random.default_rng(seed)
    draws = np.asarray([_one_draw(groups, rng) for _ in range(reps)], dtype=np.float64)
    low, high = np.percentile(draws, INTERVAL_PERCENTILES)
    return float(low), float(high)


def _one_draw(groups: Sequence[tuple[Float64, Float64]], rng: np.random.Generator) -> float:
    b3 = dropped = 0.0
    for sums_b3, sums_dropped in groups:
        pick = rng.integers(0, len(sums_b3), size=len(sums_b3))
        b3 += float(sums_b3[pick].sum())
        dropped += float(sums_dropped[pick].sum())
    return _relative(dropped, b3)


def check_candidate(artifact: Artifact, days: int, dow_type: str) -> str | None:
    """候補 k の成果物が、**本当に k 日の版か**。違えば理由を返す（W6 プラン §6.1）。

    見るのは 2 つ：`dow_type` の配る側の下限が k であること、その曜日種別の厚さ
    （`b2_max_days`。当てはめたプロファイルの日数の最大）がちょうど k 日であること。
    """
    for name, model in sorted(artifact.targets.items()):
        serve = model.b2.floor.serve[dow_type]
        thick = None if model.b2.max_days is None else model.b2.max_days[dow_type]
        if serve != days or thick != days:
            return (
                f"{artifact.model_version}/{name}: {dow_type} の下限 {serve}・厚さ {thick}"
                f"（k = {days} のはず）"
            )
    return None


def _sq(p: Float64, y: Int8, weight: Float32) -> Float64:
    return np.asarray(weight * np.square(p - y), dtype=np.float64)


def _relative(value: float, base: float) -> float:
    return (value - base) / base if base > 0 else 0.0
