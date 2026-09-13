"""評価の切り口（`eval/slices.py`、W5 プラン §6.3 の PR C）。

**主題は「同じ行を 2 度数えず、1 行も落とさないこと」。** 切り口が重なったり抜けたり
すると、表の合計が母数と合わなくなり、**どこかで嘘をついていることに気づけない**。

W5 で足した 2 つ（曜日種別・到着時刻の時間帯）を中心に固定する。

  * **曜日種別** … PR D の効果はここに出る。**名前は `dow_type_name` を通す**
  * **時間帯** … 切るのは **`t + h`**（利用者が着く時刻）。`t` ではない
"""

from datetime import date
from typing import Final

import numpy as np
import pytest

from bikechance_ml.eval import slices
from bikechance_ml.eval.dataset import TARGETS, Samples, to_samples
from bikechance_ml.features.calendar import DOW_TYPE_ORDER
from tests import eval_fixture as fixture

BIKE: Final = TARGETS[0]
DAY: Final[date] = fixture.DAYS[0]


def samples(rows: list[dict[str, object]]) -> Samples:
    return to_samples(fixture.to_table(rows))


def one_row(
    *,
    minute_of_day: int = 600,
    h_min: int = 30,
    dow_type: str = "weekday",
    system: str = "hellocycling",
) -> dict[str, object]:
    return fixture.row(
        DAY, system, "a", h_min, 3, 6, 1, 1, minute_of_day=minute_of_day, dow_type=dow_type
    )


def labels(parts: list[slices.Slice]) -> list[str | None]:
    return [one.cut for one in parts]


def covered(parts: list[slices.Slice], total: int) -> None:
    """**重ならず、抜けもない**ことを確かめる（どの切り口でも同じ条件）。"""
    stacked = np.vstack([one.mask for one in parts])
    assert int(stacked.sum()) == total, "行を落としている（または二重に数えている）"
    assert stacked.sum(axis=0).max() <= 1, "同じ行が 2 つの切り口に入っている"


# ── 曜日種別 ──────────────────────────────────────────────────
def test_the_dow_types_partition_the_rows() -> None:
    """3 種別で 1 行も落とさず、重ねもしない。"""
    rows = [one_row(dow_type=kind) for kind in ("weekday", "sat", "sun_holiday")]
    built = samples(rows)
    parts = list(slices.by_dow_type(built, BIKE))
    assert len(parts) == len(DOW_TYPE_ORDER), "1 system あたり 3 つ"
    covered(parts, len(built))


def test_the_label_is_the_name_not_the_number() -> None:
    """**`dow_type_name` を通す。** `DOW_TYPES[index]` と書くと 1 つずれる（§12 の 132）。"""
    rows = [one_row(dow_type=kind) for kind in ("weekday", "sat", "sun_holiday")]
    parts = list(slices.by_dow_type(samples(rows), BIKE))
    assert labels(parts) == list(DOW_TYPE_ORDER)


def test_each_dow_type_gets_its_own_rows() -> None:
    """札と中身が一致していること（番号がずれていれば、ここで入れ替わる）。"""
    rows = [
        one_row(dow_type="weekday", minute_of_day=60),
        one_row(dow_type="sat", minute_of_day=120),
        one_row(dow_type="sun_holiday", minute_of_day=180),
    ]
    built = samples(rows)
    by_label = {one.cut: one.mask for one in slices.by_dow_type(built, BIKE)}
    assert built.minute_of_day[by_label["weekday"]].tolist() == [60]
    assert built.minute_of_day[by_label["sat"]].tolist() == [120]
    assert built.minute_of_day[by_label["sun_holiday"]].tolist() == [180]


def test_horizons_are_mixed_on_purpose() -> None:
    """**水平はまとめる。** どの種別も同じ割合で 10 水平を持つので、種別どうしは比べられる。"""
    rows = [one_row(dow_type="sat", h_min=h) for h in (5, 30, 180)]
    parts = {one.cut: one for one in slices.by_dow_type(samples(rows), BIKE)}
    assert parts["sat"].horizon is None
    assert parts["sat"].n == 3


# ── 時間帯 ────────────────────────────────────────────────────
def test_the_bands_cover_the_whole_day() -> None:
    """**24 時間に穴も重なりも無い。** 端（0 時と 23 時）まで含む。"""
    rows = [one_row(minute_of_day=hour * 60, h_min=5) for hour in range(24)]
    built = samples(rows)
    parts = list(slices.by_time_of_day(built, BIKE))
    assert labels(parts) == [name for name, _ in slices.TIME_BANDS]
    covered(parts, len(built))


def test_the_band_follows_the_arrival_not_the_question() -> None:
    """**切るのは `t + h`。** 朝に問い合わせて昼に着く行は「昼」に入る。

    `t` で切ると、**利用者が知りたい時刻とは別の軸で表を割ってしまう**。
    """
    rows = [one_row(minute_of_day=9 * 60 + 50, h_min=30)]  # 09:50 発 → 10:20 着
    parts = {one.cut: one for one in slices.by_time_of_day(samples(rows), BIKE)}
    assert parts["昼 10-15"].n == 1
    assert parts["朝 6-9"].n == 0


def test_crossing_midnight_wraps_to_the_early_hours() -> None:
    """23:50 に 30 分先を聞けば **00:20 着**。日をまたいでも時は 0 に戻る。"""
    rows = [one_row(minute_of_day=23 * 60 + 50, h_min=30)]
    parts = {one.cut: one for one in slices.by_time_of_day(samples(rows), BIKE)}
    assert parts["深夜 0-5"].n == 1
    assert parts["夜 20-23"].n == 0


@pytest.mark.parametrize(
    ("hour", "band"),
    [
        (0, "深夜 0-5"),
        (5, "深夜 0-5"),
        (6, "朝 6-9"),
        (9, "朝 6-9"),
        (10, "昼 10-15"),
        (15, "昼 10-15"),
        (16, "夕 16-19"),
        (19, "夕 16-19"),
        (20, "夜 20-23"),
        (23, "夜 20-23"),
    ],
)
def test_the_band_boundaries_are_pinned(hour: int, band: str) -> None:
    """**境目を固定する。** 動かすと、前に測った表と比べられなくなる。"""
    rows = [one_row(minute_of_day=hour * 60, h_min=5)]
    parts = {one.cut: one for one in slices.by_time_of_day(samples(rows), BIKE)}
    assert parts[band].n == 1


def test_the_target_hour_is_shared_with_the_climatology() -> None:
    """**到着時刻の取り方は 1 つ。** 気候値の `slot15` と同じ式でなければならない。"""
    from bikechance_ml.baselines.climatology import slot15

    rows = [one_row(minute_of_day=minute, h_min=45) for minute in (0, 600, 1400)]
    built = samples(rows)
    assert (slices.target_hour(built) == slot15(built) // 4).all()


# ── 既存の軸を壊していないこと ────────────────────────────────
def test_the_older_axes_still_partition() -> None:
    """**足した軸が既存の軸を触っていない**ことを、同じ条件で確かめる。"""
    rows = [one_row(h_min=h) for h in (5, 30, 60)]
    built = samples(rows)
    covered([one for one in slices.by_horizon(built, BIKE) if one.n > 0], len(built))
    covered(list(slices.overall(built, BIKE)), len(built))


def test_a_slice_without_a_cut_says_so() -> None:
    """`cut` を持たない軸は `None` のまま（表では「全体」と書く）。"""
    parts = list(slices.overall(samples([one_row()]), BIKE))
    assert [one.cut for one in parts] == [None]
    assert slices.ALL == "全体"
