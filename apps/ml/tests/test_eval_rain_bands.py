"""雨と到着の時間帯で割った Brier（`eval/rain_bands.py`。W12 プランの W12-06）。

**主題は 3 つ。**

1. **雨・雨なし・不明は重ならず、すべての行がどれかに入る**（値の無い行を「雨なし」に混ぜない）
2. **時間帯は到着の時刻で、`slices` と同じ切り方**（「夕方〜夜」は 16〜23 時）
3. **まとめた行は、二乗誤差の和を重みの和で割る**（平均の平均にしない）
"""

from datetime import date
from typing import Final

import numpy as np
import pytest

from bikechance_ml.eval import rain_bands, slices
from bikechance_ml.eval.dataset import TARGETS, Samples, to_samples
from bikechance_ml.features.arrays import Float64, Int8
from tests import eval_fixture as eval_rows

DAY: Final[date] = eval_rows.DAYS[0]

#: (到着の前の分, 水平, 重み, 答え) の 4 行。**到着の時は 16・10・0・23 時**
ROWS: Final[tuple[tuple[int, int, float, int], ...]] = (
    (15 * 60 + 50, 20, 1.0, 1),
    (9 * 60, 60, 1.0, 0),
    (23 * 60 + 50, 20, 2.0, 1),
    (23 * 60, 5, 2.0, 0),
)


def _samples(systems: tuple[str, ...] = ("hellocycling",)) -> Samples:
    rows = [
        eval_rows.row(
            DAY,
            system,
            f"s{index}",
            horizon,
            1,
            1,
            answer,
            answer,
            weight=weight,
            minute_of_day=minute,
        )
        for system in systems
        for index, (minute, horizon, weight, answer) in enumerate(ROWS)
    ]
    return to_samples(eval_rows.to_table(rows))


def _precip(*values: float) -> Float64:
    return np.asarray(values, dtype=np.float64)


# ── 1. 雨の分け方 ─────────────────────────────────────────────
def test_rain_dry_and_unknown_split_every_row_once() -> None:
    """**閾値は 0.5 mm を含む。値の無い行は「不明」**（0 と読まない）。"""
    classes = rain_bands.rain_classes(_precip(np.nan, 0.0, 0.49, 0.5, 3.0))
    assert classes[rain_bands.RAIN].tolist() == [False, False, False, True, True]
    assert classes[rain_bands.DRY].tolist() == [False, True, True, False, False]
    assert classes[rain_bands.UNKNOWN].tolist() == [True, False, False, False, False]
    total = sum(one.astype(np.int64) for one in classes.values())
    assert np.all(total == 1), "どれかに 2 度入る行か、どこにも入らない行がある"


# ── 2. 時間帯の切り方 ─────────────────────────────────────────
def test_the_bands_follow_the_arrival_hour_like_the_slices() -> None:
    """**到着の時刻（`t + h`）で切る**。15:50 に問い合わせて 20 分先なら「夕」。"""
    masks = rain_bands.band_masks(_samples())
    assert masks[rain_bands.EVENING].tolist() == [True, False, False, True]
    assert masks["夕 16-19"].tolist() == [True, False, False, False]
    assert masks["昼 10-15"].tolist() == [False, True, False, False]
    assert masks["深夜 0-5"].tolist() == [False, False, True, False]
    assert masks["夜 20-23"].tolist() == [False, False, False, True]
    assert masks[rain_bands.ALL].all()
    assert [label for label, _ in slices.TIME_BANDS] == [
        label for label in masks if label not in (rain_bands.EVENING, rain_bands.ALL)
    ], "帯の切り方が slices と違う"


# ── 3. Brier とまとめ方 ───────────────────────────────────────
BIKE, DOCK = TARGETS


def _labels(samples: Samples) -> dict[str, Int8]:
    return {BIKE.name: samples.y(BIKE)}


def test_a_cell_is_the_weighted_brier_of_its_rows() -> None:
    """**重み付きの平均二乗誤差**（`metrics.brier` と同じ）。夕方〜夜の雨は 1 行目と 4 行目。"""
    samples = _samples()
    probability = {"bike": {"A": _precip(0.8, 0.2, 0.5, 0.1), "B": _precip(0.6, 0.2, 0.5, 0.3)}}
    found = rain_bands.cells(samples, _precip(1.0, 0.0, 0.0, 2.0), _labels(samples), probability)
    one = next(
        cell
        for cell in found
        if (cell.system, cell.band, cell.rain) == ("hellocycling", rain_bands.EVENING, "雨")
    )
    assert (one.n, one.weight) == (2, 3.0)
    assert one.brier("A") == pytest.approx((1.0 * 0.2**2 + 2.0 * 0.1**2) / 3.0)
    assert one.brier("B") == pytest.approx((1.0 * 0.4**2 + 2.0 * 0.3**2) / 3.0)


def test_an_empty_cell_has_no_brier() -> None:
    """**行が無い切り口は NaN**（0 と書くと、良いように読める）。"""
    samples = _samples()
    probability = {"bike": {"A": _precip(0.5, 0.5, 0.5, 0.5)}}
    found = rain_bands.cells(samples, _precip(0.0, 0.0, 0.0, 0.0), _labels(samples), probability)
    wet = [cell for cell in found if cell.rain == rain_bands.RAIN and cell.band == rain_bands.ALL]
    assert wet and all(cell.n == 0 and np.isnan(cell.brier("A")) for cell in wet)


def test_the_merged_cell_divides_the_sums_not_the_means() -> None:
    """**系統とターゲットをまとめた行は、和を取ってから割る。**

    2 系統で重みを変えると、平均の平均（系統ごとの Brier の平均）とは違う数になる。
    """
    samples = _samples(("hellocycling", "docomo-cycle"))
    p = _precip(0.9, 0.1, 0.4, 0.6, 0.5, 0.5, 0.5, 0.5)
    labels = {BIKE.name: samples.y(BIKE), DOCK.name: samples.y(DOCK)}
    probability = {"bike": {"A": p}, "dock": {"A": p[::-1].copy()}}
    found = rain_bands.cells(samples, _precip(*([0.0] * 8)), labels, probability)
    merged = next(
        cell
        for cell in found
        if (cell.system, cell.target, cell.band, cell.rain)
        == (rain_bands.ALL, rain_bands.ALL, rain_bands.ALL, rain_bands.ALL)
    )
    weight = np.asarray(samples.weight, dtype=np.float64)
    expected = (
        np.sum(weight * (p - labels["bike"]) ** 2)
        + np.sum(weight * (p[::-1] - labels["dock"]) ** 2)
    ) / (2 * weight.sum())
    assert merged.n == 16
    assert merged.brier("A") == pytest.approx(expected)


def test_misaligned_inputs_are_refused() -> None:
    """**行数が違えば止める**（別の行で測らない）。ターゲットが食い違っても止める。"""
    samples = _samples()
    good = {"bike": {"A": _precip(0.5, 0.5, 0.5, 0.5)}}
    with pytest.raises(rain_bands.MisalignedRainError):
        rain_bands.cells(samples, _precip(0.0, 0.0), _labels(samples), good)
    with pytest.raises(rain_bands.MisalignedRainError):
        rain_bands.cells(
            samples, _precip(0.0, 0.0, 0.0, 0.0), _labels(samples), {"bike": {"A": _precip(0.5)}}
        )
    with pytest.raises(rain_bands.MisalignedRainError):
        rain_bands.cells(
            samples, _precip(0.0, 0.0, 0.0, 0.0), _labels(samples), {"dock": good["bike"]}
        )


# ── 4. 報告書 ─────────────────────────────────────────────────
def _rendered(precip: Float64) -> list[str]:
    samples = _samples()
    probability = {
        "bike": {
            "B3": _precip(0.5, 0.5, 0.5, 0.5),
            "LGBM 70×0.15": _precip(0.9, 0.1, 0.9, 0.1),
            "森 lgbm-v1-old": _precip(0.7, 0.3, 0.7, 0.3),
        }
    }
    found = rain_bands.cells(samples, precip, _labels(samples), probability)
    return rain_bands.render(found, ["B3", "LGBM 70×0.15", "森 lgbm-v1-old"], "森 lgbm-v1-old")


def test_the_report_compares_every_model_with_the_reference() -> None:
    """**改善は基準（配信中の森）に対して**。基準そのものの「改善」の列は出さない。"""
    lines = _rendered(_precip(1.0, 0.0, 0.0, 2.0))
    header = next(line for line in lines if line.startswith("| 時間帯 |"))
    assert "LGBM 70×0.15 の改善" in header and "B3 の改善" in header
    assert "森 lgbm-v1-old の改善" not in header
    evening_wet = next(line for line in lines if line.startswith(f"| {rain_bands.EVENING} | 雨 |"))
    # 候補は 0.9 / 0.1 で答え 1 / 0 に近く、基準（0.7 / 0.3）より良い → 正の改善
    assert "+" in evening_wet.split("|")[-2]


def test_unknown_rows_appear_only_when_there_are_any() -> None:
    """**「不明」は行が在るときだけ出す**（天気の欠けた日が無ければ表が短い）。"""
    without = _rendered(_precip(1.0, 0.0, 0.0, 2.0))
    with_unknown = _rendered(_precip(np.nan, 0.0, 0.0, 2.0))
    assert not any("| 不明 |" in line for line in without)
    assert any("| 不明 |" in line for line in with_unknown)


def test_improvement_is_not_a_number_without_a_reference() -> None:
    assert np.isnan(rain_bands.improvement(0.1, 0.0))
    assert np.isnan(rain_bands.improvement(float("nan"), 0.1))
    assert rain_bands.improvement(0.09, 0.10) == pytest.approx(10.0)
