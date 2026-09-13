"""ベースラインを当てはめて測る（W3 プラン §5.9）。

**分割・当てはめ・評価を 1 か所に集める。** モデルごとに違う集合で測ると比較が
成立しない（§4.4 の 30b）ので、**同じ `evaluate` マスクを全モデルに配る**。

流れ：

  1. 日単位で学習・パージ・検証に分ける（`split.py`）
  2. 学習期間で B1（参照表）・B2（気候値）を当てはめる
  3. 学習期間の行の上で B1・B2 を出し、それを入力に B3 を当てはめる
  4. **検証期間の同じ行**の上で B0〜B3 を出し、切り口ごとに測る

副作用は持たない。Parquet を読むのも Markdown を書くのも `jobs/` の仕事。
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final

import numpy as np

from bikechance_ml.baselines import blend, climatology, conditional, persistence
from bikechance_ml.eval import metrics, slices
from bikechance_ml.eval.dataset import TARGETS, Samples, Target
from bikechance_ml.eval.split import DaySplit, mask_of
from bikechance_ml.features.arrays import Bools, Float64
from bikechance_ml.features.constants import HORIZONS_MIN

#: 表に並べる順。**B0 が基準**（BSS はここからの改善で見る）。
BASELINE_MODELS: Final[tuple[str, ...]] = ("B0", "B1", "B2", "B3")

#: 後方互換の別名。**新しいコードは `Outcome.models` を読む**（外から足せるので）。
MODELS: Final[tuple[str, ...]] = BASELINE_MODELS

#: バケツ別の表を出す水平。全部出すと読めないので、短いのと 1 時間を代表にする。
BUCKET_HORIZONS: Final[tuple[int, ...]] = (5, 60)


@dataclass(frozen=True)
class TargetFit:
    """1 ターゲットぶんの当てはめ結果。"""

    target: Target
    probability: dict[str, Float64]
    b1_cells: int
    b1_missing: int
    b2_cells: int
    b2_fallback_ratio: float
    coefficients: blend.Blend


@dataclass(frozen=True)
class SliceScores:
    """1 つの切り口の成績。**すべてのモデルが同じ `n` を持つ。**"""

    slice: slices.Slice
    n: int
    weight: float
    positives: float
    weighted: dict[str, metrics.Scores]
    unweighted: dict[str, metrics.Scores]


@dataclass(frozen=True)
class Outcome:
    """1 回の評価の全体。"""

    split: DaySplit
    n_fit: int
    n_eval: int
    fits: tuple[TargetFit, ...]
    overall: tuple[SliceScores, ...]
    by_horizon: tuple[SliceScores, ...]
    by_bucket: tuple[SliceScores, ...]
    #: system × **曜日種別**（水平はまとめる）。**W5 の PR D の効果はここに出る**
    by_dow_type: tuple[SliceScores, ...] = ()
    #: system × **到着時刻の時間帯**（水平はまとめる）
    by_time_of_day: tuple[SliceScores, ...] = ()
    #: 表に並べるモデルの順。**外から足したぶんも入る**（B0〜B3 と LightGBM）
    models: tuple[str, ...] = BASELINE_MODELS


#: 外から渡す予測。`extra["LGBM"]["bike"]` が**検証期間の行に対応する確率**。
type ExtraModels = Mapping[str, Mapping[str, Float64]]

#: 軸を 1 つ作る関数（`slices.by_dow_type` など）。**足すときはここに通す。**
type SliceMaker = Callable[[Samples, Target], Iterator[slices.Slice]]


class MisalignedPredictionError(ValueError):
    """外から渡された予測の行数が検証期間と合わない。**別の行で測らない。**"""


def run(samples: Samples, split: DaySplit, extra: ExtraModels | None = None) -> Outcome:
    """分割にしたがって当てはめ、測る。

    `extra` は**外で当てはめたモデル**の予測（LightGBM など）。**同じ `evaluate` マスクの
    上で測る**ために、行数が一致しなければ例外にする（§4.4 の 30b）。行の並びは
    `samples.take(eval_mask)` と同じでなければならない——呼ぶ側が `mask_of` で作る。
    """
    fit_mask = mask_of(samples, split.fit)
    eval_mask = mask_of(samples, split.evaluate)
    evaluated = samples.take(eval_mask)
    fits = tuple(_fit_target(samples, fit_mask, eval_mask, target) for target in TARGETS)
    named = _with_extra(fits, extra, len(evaluated))
    return Outcome(
        split=split,
        n_fit=int(fit_mask.sum()),
        n_eval=int(eval_mask.sum()),
        fits=named,
        overall=_score_all(evaluated, named, _overall_slices(evaluated)),
        by_horizon=_score_all(evaluated, named, _horizon_slices(evaluated)),
        by_bucket=_score_all(evaluated, named, _bucket_slices(evaluated)),
        by_dow_type=_score_all(evaluated, named, _cut_slices(evaluated, slices.by_dow_type)),
        by_time_of_day=_score_all(evaluated, named, _cut_slices(evaluated, slices.by_time_of_day)),
        models=(*BASELINE_MODELS, *sorted(extra or {})),
    )


def _with_extra(
    fits: Sequence[TargetFit], extra: ExtraModels | None, n_eval: int
) -> tuple[TargetFit, ...]:
    """外から渡された予測を、ターゲットごとの当てはめ結果に混ぜる。"""
    if not extra:
        return tuple(fits)
    merged: list[TargetFit] = []
    for fitted in fits:
        probability = dict(fitted.probability)
        for name in sorted(extra):
            values = extra[name].get(fitted.target.name)
            if values is None or len(values) != n_eval:
                raise MisalignedPredictionError(
                    f"{name}/{fitted.target.name} の行数が検証期間（{n_eval}）と違います"
                )
            probability[name] = values
        merged.append(replace(fitted, probability=probability))
    return tuple(merged)


def _fit_target(samples: Samples, fit_mask: Bools, eval_mask: Bools, target: Target) -> TargetFit:
    """1 ターゲットを当てはめ、**検証期間の行**の予測を作る。"""
    table = conditional.fit(samples, target, fit_mask)
    climate = climatology.fit(samples, target, fit_mask)
    fitted = samples.take(fit_mask)
    evaluated = samples.take(eval_mask)

    # **B3 の入力は leave-one-out で作る。** 学習期間の行にそのまま当てはめると、
    # B1 も B2 も「自分の答えを見た」推定になり、B3 が両者を信じすぎる（§12 の 101）
    fit_b1 = conditional.predict_leave_one_out(table, fitted, target)
    fit_b2 = climatology.predict_leave_one_out(climate, fitted, target, fit_b1)
    coefficients = blend.fit(
        blend.design(fit_b1, fit_b2.probability, fitted.h_min),
        fitted.y(target),
        fitted.weight,
    )

    b1, missing = conditional.predict(table, evaluated, target)
    b2 = climatology.predict(climate, evaluated, b1)
    b3 = blend.predict(coefficients, blend.design(b1, b2.probability, evaluated.h_min))
    return TargetFit(
        target=target,
        probability={
            "B0": persistence.predict(evaluated.count_of(target)),
            "B1": b1,
            "B2": b2.probability,
            "B3": b3,
        },
        b1_cells=table.cells,
        b1_missing=missing,
        b2_cells=climate.cells,
        b2_fallback_ratio=b2.fallback_ratio,
        coefficients=coefficients,
    )


def _overall_slices(samples: Samples) -> list[slices.Slice]:
    return [one for target in TARGETS for one in slices.overall(samples, target)]


def _horizon_slices(samples: Samples) -> list[slices.Slice]:
    return [one for target in TARGETS for one in slices.by_horizon(samples, target)]


def _cut_slices(samples: Samples, make: SliceMaker) -> list[slices.Slice]:
    """軸を 1 つ足すときの定型。**両ターゲットぶんを並べる。**"""
    return [one for target in TARGETS for one in make(samples, target)]


def _bucket_slices(samples: Samples) -> list[slices.Slice]:
    return [
        one
        for target in TARGETS
        for horizon in BUCKET_HORIZONS
        if horizon in HORIZONS_MIN
        for one in slices.by_bucket(samples, target, horizon)
    ]


def _score_all(
    samples: Samples, fits: Sequence[TargetFit], wanted: Sequence[slices.Slice]
) -> tuple[SliceScores, ...]:
    """切り口ごとに全モデルを測る。**空の切り口は出さない。**"""
    by_name = {one.target.name: one for one in fits}
    return tuple(_score_one(samples, by_name[one.target], one) for one in wanted if one.n > 0)


def _score_one(samples: Samples, fitted: TargetFit, part: slices.Slice) -> SliceScores:
    y = samples.y(fitted.target)[part.mask]
    weight = samples.weight[part.mask]
    return SliceScores(
        slice=part,
        n=part.n,
        weight=float(weight.astype(np.float64).sum()),
        positives=float((y * weight).sum() / weight.sum()),
        weighted={
            name: metrics.score(y, values[part.mask], weight)
            for name, values in fitted.probability.items()
        },
        unweighted={
            name: metrics.score(y, values[part.mask]) for name, values in fitted.probability.items()
        },
    )
