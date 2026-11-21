"""雨と到着の時間帯で割った Brier（W12 プランの W12-06。W11 プランの所見 248）。

**W11 の新しい森は乾いた日の比べで選び、本降りの雨の夜に負けた**（shadow の 1 日目、
11-17）。学習の窓に強い雨の夜が無かった（W12 プラン §2）。**選ぶ前に雨の夜を見る**ために、
**同じ検証の行**を「到着の時間帯 × 到着の時刻の降水の予報」で割り、モデルごとの Brier を並べる。

* **時間帯は `slices.bands` を通す**（到着の時刻 `t + h`。報告書の §10 と同じ 5 つ）。合格の条件に
  使う「夕方〜夜（16〜23 時）」を 1 つ足す
* **雨は `precip_mm_target`**（到着の時刻の降水。**発行時の予報の値**で、答えは見ていない）が
  `RAIN_MM` 以上。値が無い行は「不明」に分ける——**0 と読まない**（天気の無い行を「雨なし」に
  混ぜると、雨の夜の行が少ない日に差が薄まる）
* Brier は `weight` で重み付けた平均（`metrics.brier` と同じ）。系統とターゲットをまとめた行は、
  **重み付き二乗誤差の和を重みの和で割る**（平均の平均にしない）

副作用は持たない。行列から雨の列を取るのは `jobs/fit_lightgbm.py`。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np

from bikechance_ml.eval import slices
from bikechance_ml.eval.dataset import Samples
from bikechance_ml.features.arrays import Bools, Float64, Int8

#: 雨とみなす降水（mm/時）。**W11 の比べ（所見 248）と W12 プラン §2 の表と同じ 0.5 mm。**
RAIN_MM: Final[float] = 0.5

#: 合格の条件に使う帯（W12-06：16〜24 時）。`slices.TIME_BANDS` の「夕」と「夜」を合わせる。
EVENING: Final[str] = "夕方〜夜 16-23"

#: 「夕方〜夜」の始まりの時（含む）。終わりは 24 時。
EVENING_START_HOUR: Final[int] = 16

RAIN: Final[str] = "雨"
DRY: Final[str] = "雨なし"
UNKNOWN: Final[str] = "不明"

#: 「割らない」を表す印（`slices.ALL` と同じ「全体」）。
ALL: Final[str] = slices.ALL

#: 表に並べる雨の順（「全体」は 3 つを合わせたもの）。
#: **「不明」は行が在るときだけ出す**（`render`）。
RAIN_ORDER: Final[tuple[str, ...]] = (RAIN, DRY, UNKNOWN, ALL)

#: Brier を書く桁。報告書の §3〜§11 と同じ 5 桁。
BRIER_DIGITS: Final[int] = 5

PERCENT: Final[float] = 100.0


@dataclass(frozen=True)
class Cell:
    """1 つの切り口（系統 × ターゲット × 時間帯 × 雨）。

    **モデルごとの重み付き二乗誤差の和**を持つ（まとめるときは和を取ってから割る）。
    """

    system: str
    target: str
    band: str
    rain: str
    n: int
    weight: float
    #: モデル → **重み付き二乗誤差の和**。Brier はこれを `weight` で割る
    sse: Mapping[str, float]

    def brier(self, model: str) -> float:
        """重み付きの Brier。**行が無ければ NaN**（0 と書かない）。"""
        return self.sse[model] / self.weight if self.weight > 0 else float("nan")


def rain_classes(precip_mm: Float64) -> dict[str, Bools]:
    """雨・雨なし・不明の真偽。**3 つは重ならず、すべての行がどれかに入る。**"""
    known = np.asarray(~np.isnan(precip_mm), dtype=np.bool_)
    wet = np.asarray(known & (np.nan_to_num(precip_mm, nan=0.0) >= RAIN_MM), dtype=np.bool_)
    return {RAIN: wet, DRY: np.asarray(known & ~wet, dtype=np.bool_), UNKNOWN: ~known}


def band_masks(samples: Samples) -> dict[str, Bools]:
    """到着の時間帯の真偽。**「夕方〜夜」と「全体」は、ほかの帯と重なる**（足して数えない）。"""
    hour = slices.target_hour(samples)
    evening = np.asarray(hour >= EVENING_START_HOUR, dtype=np.bool_)
    return {
        EVENING: evening,
        **dict(slices.bands(hour)),
        ALL: np.ones(len(samples), dtype=np.bool_),
    }


def cells(
    samples: Samples,
    precip_mm: Float64,
    labels: Mapping[str, Int8],
    probability: Mapping[str, Mapping[str, Float64]],
) -> tuple[Cell, ...]:
    """系統 × ターゲット × 時間帯 × 雨の切り口と、**系統とターゲットをまとめた**切り口。

    `labels` はターゲット → 答え、`probability` はターゲット → モデル → 予測で、**どれも
    `samples` と同じ行の並び**（`harness.Outcome.fits` の `probability` をそのまま渡せる）。
    """
    refuse_misaligned(samples, precip_mm, labels, probability)
    bands = band_masks(samples)
    rains = {**rain_classes(precip_mm), ALL: np.ones(len(samples), dtype=np.bool_)}
    weight = np.asarray(samples.weight, dtype=np.float64)
    systems = {
        name: np.asarray(samples.system == index, dtype=np.bool_)
        for index, name in enumerate(samples.systems)
    }
    each = tuple(
        _cell(
            (system, target, band, rain),
            np.asarray(in_system & in_band & is_rain, dtype=np.bool_),
            weight,
            labels[target],
            probability[target],
        )
        for target in labels
        for system, in_system in systems.items()
        for band, in_band in bands.items()
        for rain, is_rain in rains.items()
    )
    return (*each, *merged(each))


class MisalignedRainError(ValueError):
    """雨の列・答え・予測の行数が、検証の行と合わない。**別の行で測らない。**"""


def refuse_misaligned(
    samples: Samples,
    precip_mm: Float64,
    labels: Mapping[str, Int8],
    probability: Mapping[str, Mapping[str, Float64]],
) -> None:
    """**行数が 1 つでも違えば止める**（`harness.MisalignedPredictionError` と同じ作法）。"""
    rows = len(samples)
    lengths = {
        "降水": len(precip_mm),
        **{f"答え {target}": len(values) for target, values in labels.items()},
        **{
            f"予測 {target}/{model}": len(values)
            for target, by_model in probability.items()
            for model, values in by_model.items()
        },
    }
    wrong = {name: length for name, length in lengths.items() if length != rows}
    if wrong or set(labels) != set(probability):
        raise MisalignedRainError(f"検証の行（{rows}）と合いません: {wrong or sorted(probability)}")


def _cell(
    key: tuple[str, str, str, str],
    mask: Bools,
    weight: Float64,
    labels: Int8,
    by_model: Mapping[str, Float64],
) -> Cell:
    system, target, band, rain = key
    chosen_weight = weight[mask]
    answer = np.asarray(labels[mask], dtype=np.float64)
    sse = {
        model: float(np.sum(chosen_weight * np.square(values[mask] - answer)))
        for model, values in by_model.items()
    }
    return Cell(system, target, band, rain, int(mask.sum()), float(chosen_weight.sum()), sse)


def merged(each: Sequence[Cell]) -> tuple[Cell, ...]:
    """**系統とターゲットをまとめた**切り口（時間帯 × 雨）。和を取ってから割る。"""
    keys = tuple(dict.fromkeys((one.band, one.rain) for one in each))
    return tuple(
        _sum(ALL, ALL, band, rain, [one for one in each if (one.band, one.rain) == (band, rain)])
        for band, rain in keys
    )


def _sum(system: str, target: str, band: str, rain: str, parts: Sequence[Cell]) -> Cell:
    models = tuple(dict.fromkeys(model for one in parts for model in one.sse))
    return Cell(
        system,
        target,
        band,
        rain,
        sum(one.n for one in parts),
        sum(one.weight for one in parts),
        {model: sum(one.sse[model] for one in parts) for model in models},
    )


# ── 報告書 ────────────────────────────────────────────────────
def render(found: Sequence[Cell], models: Sequence[str], reference: str) -> list[str]:
    """§13 の Markdown。**改善は `reference`（ふつうは配信中の森）に対して**出す。"""
    others = [model for model in models if model != reference]
    return [
        "## 13. 雨と到着の時間帯（W12-06）",
        "",
        *_NOTE,
        "",
        "### 2 系統・2 ターゲットをまとめて",
        "",
        *_table(_overall(found), models, others, reference, with_system=False),
        "",
        f"### 系統 × ターゲット（{EVENING}）",
        "",
        *_table(_evening(found), models, others, reference, with_system=True),
        "",
    ]


_NOTE: Final[tuple[str, ...]] = (
    f"> **雨は到着の時刻の降水の予報**（`precip_mm_target`、発行時の値）が {RAIN_MM} mm 以上。"
    "値が無い行は「不明」。**時間帯は到着の時刻**（`t + h`。§10 と同じ切り方）。",
    ">",
    "> 数は `weight` で重み付けた Brier。**改善は 1 − Brier(モデル) / Brier(基準)** で、"
    "正なら基準より良い。W12-06 の合格の条件は、"
    f"**{EVENING} × {RAIN} の行で配信中の森を下回らないこと**。",
)


def _overall(found: Sequence[Cell]) -> list[Cell]:
    order = [EVENING, *(label for label, _ in slices.TIME_BANDS), ALL]
    chosen = [one for one in found if one.system == ALL and one.target == ALL]
    return [
        one
        for band in order
        for rain in RAIN_ORDER
        for one in chosen
        if (one.band, one.rain) == (band, rain) and (rain != UNKNOWN or one.n > 0)
    ]


def _evening(found: Sequence[Cell]) -> list[Cell]:
    return [
        one
        for one in found
        if one.system != ALL and one.band == EVENING and one.rain in (RAIN, DRY)
    ]


def _table(
    rows: Sequence[Cell],
    models: Sequence[str],
    others: Sequence[str],
    reference: str,
    *,
    with_system: bool,
) -> list[str]:
    head = (["系統", "ターゲット"] if with_system else ["時間帯"]) + ["雨", "件数"]
    columns = [*head, *models, *(f"{model} の改善" for model in others)]
    lines = [
        "| " + " | ".join(columns) + " |",
        "|" + "---|" * len(head) + "---:|" * (len(columns) - len(head)),
    ]
    return [
        *lines,
        *(_row(one, models, others, reference, with_system=with_system) for one in rows),
    ]


def _row(
    one: Cell, models: Sequence[str], others: Sequence[str], reference: str, *, with_system: bool
) -> str:
    where = [one.system, one.target] if with_system else [one.band]
    briers = [_brier_text(one.brier(model)) for model in models]
    gains = [_gain_text(one.brier(model), one.brier(reference)) for model in others]
    return "| " + " | ".join([*where, one.rain, f"{one.n:,}", *briers, *gains]) + " |"


def _brier_text(value: float) -> str:
    return "—" if np.isnan(value) else f"{value:.{BRIER_DIGITS}f}"


def improvement(model_brier: float, reference_brier: float) -> float:
    """1 − Brier(モデル) / Brier(基準) を % で。**基準が 0 か NaN なら NaN**。"""
    if np.isnan(model_brier) or np.isnan(reference_brier) or reference_brier == 0:
        return float("nan")
    return PERCENT * (1.0 - model_brier / reference_brier)


def _gain_text(model_brier: float, reference_brier: float) -> str:
    value = improvement(model_brier, reference_brier)
    return "—" if np.isnan(value) else f"{value:+.2f}%"
