"""ベースラインを当てはめて測る（W3 プラン §5.9）。

**分割・当てはめ・評価を 1 か所に集める。** モデルごとに違う集合で測ると比較が
成立しない（§4.4 の 30b）ので、**同じ `evaluate` マスクを全モデルに配る**。

流れ：

  1. 日単位で学習・パージ・検証に分ける（`split.py`）
  2. 学習期間で B1（参照表）・B2（気候値）を当てはめる
  3. 学習期間の行の上で B1・B2 を出し、それを入力に B3 を当てはめる
  4. **検証期間の同じ行**の上で B0〜B3 を出し、切り口ごとに測る

**B2 の作り方は外から渡す**（`climatology.Source`。W5 プラン §6.4 の PR D）。学習
サンプルの行から作るか、ポートプロファイルから作るかの違いで、**渡さなければ従来
どおり**である。渡すときは「**学習の最終日の版**」でなければならない——検証日を
含む版を渡すと、B2 が答えを見た状態で測ることになる。

副作用は持たない。Parquet を読むのも Markdown を書くのも `jobs/` の仕事。
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
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
    #: 混合（B3）の当てはめに使った行数。**自分のぶんを引けない行は外れる**
    n_blend: int = 0


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
    #: B2 をどこから作ったか（`climatology.Source.describe`）。**報告書に出す**
    climate: str = ""
    #: **B0〜B3 を当てはめた日**（W6-10）。空なら学習期間ぜんぶ。`n_fit` は学習期間ぜんぶの行
    baseline_days: tuple[date, ...] = ()
    #: B0〜B3 の当てはめに使った行の数
    n_baseline_fit: int = 0


#: 外から渡す予測。`extra["LGBM"]["bike"]` が**検証期間の行に対応する確率**。
type ExtraModels = Mapping[str, Mapping[str, Float64]]

#: 軸を 1 つ作る関数（`slices.by_dow_type` など）。**足すときはここに通す。**
type SliceMaker = Callable[[Samples, Target], Iterator[slices.Slice]]


class MisalignedPredictionError(ValueError):
    """外から渡された予測の行数が検証期間と合わない。**別の行で測らない。**"""


class BaselineDaysError(ValueError):
    """ベースラインを当てはめる日が、学習期間の中に無い。**検証日の答えを見せない。**"""


def run(
    samples: Samples,
    split: DaySplit,
    extra: ExtraModels | None = None,
    *,
    climate: climatology.Source,
    baseline_days: Sequence[date] | None = None,
) -> Outcome:
    """分割にしたがって当てはめ、測る。

    `extra` は**外で当てはめたモデル**の予測（LightGBM など）。**同じ `evaluate` マスクの
    上で測る**ために、行数が一致しなければ例外にする（§4.4 の 30b）。行の並びは
    `samples.take(eval_mask)` と同じでなければならない——呼ぶ側が `mask_of` で作る。

    `climate` は **B2 の作り方**。**必ず渡す**（既定値を置かない。W5 プラン §12 の 166）
    ——置いていたときは `fit_lightgbm` が渡し忘れ、**同じ日の同じ `v3` で違う B2 が
    出ていた**のに、例外も警告も出なかった。決め方は `jobs/climate.py` の `source_for`
    に 1 つだけある。

    `baseline_days` は **B0〜B3 を当てはめる日**（W6-10、W6 の契約 36）。**配る B3 は学習窓の
    最後の 7 日**で当てはめるので、LightGBM の門の相手もそれに揃える——学習窓ぜんぶ（28 日）で
    当てはめた B3 は**本番に無いもの**で、比べる相手が違う（W6 プランの所見 198）。渡すなら
    学習期間の中の日でなければならず、`climate` もその最後の日の版でなければならない。
    """
    source = climate
    fitted_days = _baseline_days(split, baseline_days)
    fit_mask = mask_of(samples, fitted_days)
    eval_mask = mask_of(samples, split.evaluate)
    evaluated = samples.take(eval_mask)
    fits = tuple(_fit_target(samples, fit_mask, eval_mask, target, source) for target in TARGETS)
    named = _with_extra(fits, extra, len(evaluated))
    return Outcome(
        split=split,
        n_fit=int(mask_of(samples, split.fit).sum()),
        n_eval=int(eval_mask.sum()),
        fits=named,
        overall=_score_all(evaluated, named, _overall_slices(evaluated)),
        by_horizon=_score_all(evaluated, named, _horizon_slices(evaluated)),
        by_bucket=_score_all(evaluated, named, _bucket_slices(evaluated)),
        by_dow_type=_score_all(evaluated, named, _cut_slices(evaluated, slices.by_dow_type)),
        by_time_of_day=_score_all(evaluated, named, _cut_slices(evaluated, slices.by_time_of_day)),
        models=(*BASELINE_MODELS, *sorted(extra or {})),
        climate=source.describe(),
        baseline_days=fitted_days,
        n_baseline_fit=int(fit_mask.sum()),
    )


def _baseline_days(split: DaySplit, wanted: Sequence[date] | None) -> tuple[date, ...]:
    """B0〜B3 を当てはめる日。**学習期間の外の日を渡されたら止める**（検証日の答えを見せない）。"""
    if wanted is None:
        return tuple(split.fit)
    chosen = tuple(sorted(set(wanted)))
    if not chosen or not set(chosen) <= set(split.fit):
        raise BaselineDaysError(f"学習期間 {split.describe()} の外の日を渡されました: {chosen}")
    return chosen


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


def _fit_target(
    samples: Samples,
    fit_mask: Bools,
    eval_mask: Bools,
    target: Target,
    source: climatology.Source,
) -> TargetFit:
    """1 ターゲットを当てはめ、**検証期間の行**の予測を作る。"""
    table = conditional.fit(samples, target, fit_mask)
    climate = source.table(samples, target, fit_mask)
    fitted = samples.take(np.asarray(fit_mask & source.blend_rows(samples), dtype=np.bool_))
    evaluated = samples.take(eval_mask)

    # **B3 の入力は自分のぶんを引いて作る。** 学習期間の行にそのまま当てはめると、
    # B1 も B2 も「自分の答えを見た」推定になり、B3 が両者を信じすぎる（§12 の 101）
    fit_b1 = conditional.predict_leave_one_out(table, fitted, target)
    fit_b2 = source.leave_out(climate, fitted, target, fit_b1)
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
        n_blend=len(fitted),
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
