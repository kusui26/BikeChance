"""暦の特徴量（`features/calendar.py`、開発プラン §6.3・§6.4）。

**この 1 ファイルの主題は「TypeScript と同じ答えを出すこと」。**

`fixtures/calendar/day_type_golden.csv` は `packages/shared/src/calendar.ts` から
生成したもので、2026-01-01〜2027-12-31 の 730 日ぶんの答えが入っている。ここが落ちたら、
**学習（Python）と配信（TypeScript）で暦の解釈がずれている**ということ。

祝日そのものもゴールデンに入っているので、このテストは DB も CSV も要らない。
"""

import csv
from datetime import date
from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest

from bikechance_ml.features.calendar import (
    DAY_TYPES,
    DOW_TYPE_ORDER,
    DOW_TYPES,
    dates_between,
    day_type,
    dow_type,
    dow_type_name,
    is_day_before_holiday,
    is_last_business_day,
    is_new_year,
    is_obon,
    is_off,
)

GOLDEN = Path(__file__).resolve().parents[3] / "fixtures" / "calendar" / "day_type_golden.csv"


def load_golden() -> tuple[list[dict[str, str]], frozenset[date]]:
    """ゴールデンを読む。先頭の `#` 行は出典と注意書きなので飛ばす。"""
    lines = [
        line for line in GOLDEN.read_text(encoding="utf-8").splitlines() if not line.startswith("#")
    ]
    rows = list(csv.DictReader(lines))
    holidays = frozenset(
        date.fromisoformat(row["date"]) for row in rows if row["holiday_name"] != ""
    )
    return rows, holidays


ROWS, HOLIDAYS = load_golden()


# ── TypeScript との一致 ────────────────────────────────────────
def test_golden_covers_two_years() -> None:
    """範囲が縮んでいないこと。**フィクスチャが空でも通るテストにしない。**"""
    assert len(ROWS) == 730
    assert ROWS[0]["date"] == "2026-01-01"
    assert ROWS[-1]["date"] == "2027-12-31"


def test_day_type_matches_typescript() -> None:
    """730 日すべてで `day_type` が一致する。"""
    for row in ROWS:
        day = date.fromisoformat(row["date"])
        assert day_type(day, HOLIDAYS) == row["day_type"], row["date"]


def test_dow_type_matches_typescript() -> None:
    for row in ROWS:
        day = date.fromisoformat(row["date"])
        assert dow_type(day_type(day, HOLIDAYS)) == row["dow_type"], row["date"]


def test_day_before_holiday_matches_typescript() -> None:
    for row in ROWS:
        day = date.fromisoformat(row["date"])
        assert is_day_before_holiday(day, HOLIDAYS) == (row["is_day_before_holiday"] == "1"), row[
            "date"
        ]


def test_last_business_day_matches_typescript() -> None:
    for row in ROWS:
        day = date.fromisoformat(row["date"])
        assert is_last_business_day(day, HOLIDAYS) == (row["is_last_business_day"] == "1"), row[
            "date"
        ]


# ── 規則そのもの ──────────────────────────────────────────────
def test_new_year_is_a_calendar_rule() -> None:
    """**表に持たない。** 12/29〜1/3。"""
    assert [is_new_year(date(2026, 12, d)) for d in (28, 29, 31)] == [False, True, True]
    assert [is_new_year(date(2027, 1, d)) for d in (1, 3, 4)] == [True, True, False]


def test_obon_is_a_calendar_rule() -> None:
    assert [is_obon(date(2026, 8, d)) for d in (12, 13, 16, 17)] == [False, True, True, False]


def test_period_wins_over_holiday() -> None:
    """元日は法定の祝日でもあるが、年末年始の期間全体が休業として振る舞う。"""
    assert date(2026, 1, 1) in HOLIDAYS
    assert day_type(date(2026, 1, 1), HOLIDAYS) == "newyear"


def test_bridge_is_a_weekday_between_two_days_off() -> None:
    # 2026-08-10（月）は日曜 8/9 と山の日 8/11 に挟まれている
    assert day_type(date(2026, 8, 10), HOLIDAYS) == "bridge"
    # 2026-11-04（水）は文化の日の翌日だが、翌 11/5 は平日なので飛び石ではない
    assert day_type(date(2026, 11, 4), HOLIDAYS) == "weekday"


def test_bridge_is_never_a_day_off() -> None:
    for row in ROWS:
        day = date.fromisoformat(row["date"])
        if row["day_type"] == "bridge":
            assert not is_off(day, HOLIDAYS), row["date"]


# ── 値の範囲 ──────────────────────────────────────────────────
def test_every_day_gets_one_of_the_known_types() -> None:
    for day in dates_between(date(2026, 1, 1), date(2027, 12, 31)):
        assert day_type(day, HOLIDAYS) in DAY_TYPES
        assert dow_type(day_type(day, HOLIDAYS)) in DOW_TYPES


def test_dow_type_folds_holidays_into_sunday() -> None:
    """プロファイルと気候値のセルが埋まる粒度にする（3 値）。"""
    assert [dow_type(one) for one in ("sun", "holiday", "newyear", "obon")] == ["sun_holiday"] * 4
    assert dow_type("sat") == "sat"
    assert [dow_type(one) for one in ("weekday", "bridge")] == ["weekday"] * 2


def test_last_business_day_is_exactly_once_per_month() -> None:
    counted: dict[tuple[int, int], int] = {}
    for day in dates_between(date(2026, 1, 1), date(2027, 12, 31)):
        if is_last_business_day(day, HOLIDAYS):
            counted[(day.year, day.month)] = counted.get((day.year, day.month), 0) + 1
    assert len(counted) == 24, "24 か月すべてに 1 日ある"
    assert set(counted.values()) == {1}


def test_december_last_business_day_avoids_the_new_year_period() -> None:
    """12/29〜31 は年末年始なので、12 月の最後の営業日はそれより前になる。"""
    assert is_last_business_day(date(2026, 12, 28), HOLIDAYS)
    assert not is_last_business_day(date(2026, 12, 31), HOLIDAYS)


@pytest.mark.parametrize("day", [date(2028, 2, 29), date(2028, 2, 28)])
def test_leap_day_is_handled(day: date) -> None:
    """うるう日でも例外にならない（2028 年は祝日データの範囲外だが規則は効く）。"""
    assert day_type(day, HOLIDAYS) in DAY_TYPES


# ── 曜日種別の番号（W5 の PR C、§12 の 132）────────────────────
# **値を変えると、Storage に在る成果物が別のセルを指す。** 例外は出ず、確率だけが
# 静かに変わる。だから「直したくなったとき」に落ちる検査をここに置く。
def test_the_number_for_each_dow_type_is_pinned() -> None:
    """**この並びは契約である。** `DOW_TYPES` の並びではない。

    気候値の成果物は `(ポート, 曜日種別, 15 分枠)` を平たい配列で持ち、**曜日種別は
    この番号で引く**。`DOW_TYPES` の順に「直す」と、`weekday` の 66 万セルが
    `sun_holiday` として読まれる（W5 プラン §2.3f）。
    """
    assert DOW_TYPE_ORDER == ("sat", "sun_holiday", "weekday")
    assert DOW_TYPE_ORDER != DOW_TYPES, "**わざと違う。** 同じにすると過去の成果物が壊れる"


def test_the_name_comes_back_from_the_number() -> None:
    """**報告書はここを通す。** `DOW_TYPES[index]` と書くと 1 つずれる。"""
    assert [dow_type_name(index) for index in range(len(DOW_TYPE_ORDER))] == list(DOW_TYPE_ORDER)
    assert dow_type_name(2) == "weekday"
    # **これが「直したくなる」書き方。** 実際に W5 プランの最初の表がこうなっていた
    assert DOW_TYPES[2] == "sun_holiday", "素直に引くと日祝になってしまう"


def test_the_training_side_and_the_serving_side_agree() -> None:
    """**規約が 2 か所に無名で在った**（W5 プラン §12 の 132）。いまは同じ定数を読む。

    学習（`eval/dataset.py`）と配信（`models/predictor.py`）が別々に `sorted()` を
    呼んでいたので、片方だけ直すと成果物が別のセルを指す状態だった。
    """
    from bikechance_ml.eval.dataset import dow_type_indices
    from bikechance_ml.models.predictor import _dow_type_index

    values = [*DOW_TYPES, "weekday", "sat"]
    table = pa.table({"target_dow_type": pa.array(values, type=pa.string())})
    training = dow_type_indices(np.array(values, dtype=np.str_))
    serving = _dow_type_index(table)
    assert training.tolist() == serving.tolist()
    assert [dow_type_name(one) for one in training] == values


def test_an_unknown_dow_type_is_refused() -> None:
    """**知らない値で黙って番号を作らない**（`searchsorted` は必ず何かを返す）。"""
    from bikechance_ml.eval.dataset import dow_type_indices

    with pytest.raises(ValueError, match="dow_type"):
        dow_type_indices(np.array(["holiday"], dtype=np.str_))
