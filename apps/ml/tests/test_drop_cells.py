"""合成器で削るセル（`jobs/drop_cells.py`。W6-12 の決まり、W8 の PR G）。

**この 1 ファイルの主題は「安全側にだけ動くこと」**。削り過ぎれば森の改善を捨て、削り損ねれば
B3 より悪い確率を配り続ける。守るのは 4 つ。

  * **比べる日は昇格の翌日から**（その日は前の版と時間帯が混ざる）、直近 7 日まで
  * **森が B3 より厳密に悪いセルだけ**を削る（引き分けは削らない）
  * **前に削ったセルは引き継ぐ**（削ったセルでは合成器も B3 を出すので、比べると「悪くない」に
    見える——引き継がなければ黙って森に戻る）
  * **両方の成績がそろったセルだけ**で決める
"""

from datetime import date, timedelta
from typing import Final

import pytest

from bikechance_ml.jobs import drop_cells
from bikechance_ml.jobs.drop_cells import DailyMetric
from bikechance_ml.models.gates import CellKey

COMPOSITE: Final[str] = "composite-v1-20261025"
B3: Final[str] = "baseline-b3-v0-20261025"
DAY: Final[date] = date(2026, 11, 1)

GOOD: Final[CellKey] = ("hellocycling", "bike", 30, "0")
BAD: Final[CellKey] = ("docomo-cycle", "dock", 120, "11+")
TIE: Final[CellKey] = ("hellocycling", "dock", 15, "2")
CARRIED: Final[CellKey] = ("docomo-cycle", "bike", 45, "1")


def _row(version: str, cell: CellKey, day: date, n: int, brier: float) -> DailyMetric:
    system_id, target, h_min, bucket = cell
    return DailyMetric(version, day, system_id, target, h_min, bucket, n, brier)


def _pair(cell: CellKey, day: date, mine: float, theirs: float, n: int = 100) -> list[DailyMetric]:
    return [_row(COMPOSITE, cell, day, n, mine), _row(B3, cell, day, n, theirs)]


# ── 比べる日 ──────────────────────────────────────────────────
def test_days_start_the_day_after_the_promotion() -> None:
    """**昇格の日は外す**（#5 で 10-20 を外したのと同じ）。月曜に上げたら火曜から日曜まで。"""
    days = drop_cells.window_days(date(2026, 10, 26), DAY)
    assert days == tuple(date(2026, 10, 27) + timedelta(days=step) for step in range(6))


def test_days_are_capped_at_seven() -> None:
    days = drop_cells.window_days(date(2026, 10, 1), DAY)
    assert len(days) == drop_cells.WINDOW_DAYS
    assert days[-1] == DAY


def test_without_a_promotion_time_the_last_seven_days_are_used() -> None:
    assert drop_cells.window_days(None, DAY) == drop_cells.window_days(date(2026, 1, 1), DAY)


@pytest.mark.parametrize("promoted", [DAY, DAY + timedelta(days=1)])
def test_a_promotion_on_or_after_the_last_day_leaves_nothing_to_compare(promoted: date) -> None:
    assert drop_cells.window_days(promoted, DAY) == ()


# ── 比べる ──────────────────────────────────────────────────────
def test_cells_are_weighted_by_count_across_days() -> None:
    """**件数で重み付ける**：n=300 の 0.10 と n=100 の 0.30 なら 0.15。"""
    rows = [
        *_pair(GOOD, DAY, 0.10, 0.20, n=300),
        *_pair(GOOD, DAY - timedelta(days=1), 0.30, 0.20, n=100),
    ]
    (one,) = drop_cells.compare_cells(rows, {GOOD}, COMPOSITE, B3)
    assert one.composite == pytest.approx(0.15)
    assert one.b3 == pytest.approx(0.20)
    assert (one.days, one.n) == (2, 400)
    assert not one.worse
    assert one.improvement == pytest.approx(0.25)


def test_only_cells_with_both_versions_are_compared() -> None:
    """**片方しか無いセルでは決めない**（日次評価が片側だけ落ちた日など）。"""
    rows = [*_pair(GOOD, DAY, 0.1, 0.2), _row(COMPOSITE, BAD, DAY, 100, 0.5)]
    compared = drop_cells.compare_cells(rows, {GOOD, BAD}, COMPOSITE, B3)
    assert [one.cell for one in compared] == [GOOD]


def test_cells_outside_the_forest_and_other_versions_are_ignored() -> None:
    rows = [
        *_pair(GOOD, DAY, 0.1, 0.2),
        *_pair(BAD, DAY, 0.5, 0.1),
        _row("baseline-b3-v0-20261019", GOOD, DAY, 100, 0.9),
    ]
    (one,) = drop_cells.compare_cells(rows, {GOOD}, COMPOSITE, B3)
    assert one.b3 == pytest.approx(0.2), "別の版の行は混ぜない"


# ── 決める ──────────────────────────────────────────────────────
def _decision(carried: tuple[CellKey, ...] = ()) -> drop_cells.DropDecision:
    rows = [*_pair(GOOD, DAY, 0.1, 0.2), *_pair(BAD, DAY, 0.3, 0.2), *_pair(TIE, DAY, 0.2, 0.2)]
    compared = drop_cells.compare_cells(rows, {GOOD, BAD, TIE}, COMPOSITE, B3)
    return drop_cells.decide(compared, carried, composite=COMPOSITE, b3=B3, days=(DAY,))


def test_only_strictly_worse_cells_are_dropped() -> None:
    """**引き分けは削らない**（同じ値なら森を残しても配る確率は悪くならない）。"""
    assert _decision().dropped == (BAD,)


def test_cells_dropped_before_are_carried_forward() -> None:
    """**足さない**（W6-12）：前の合成器で削ったセルは、比べられなくても削ったまま。"""
    decided = _decision(carried=(CARRIED,))
    assert decided.dropped == tuple(sorted((BAD, CARRIED)))
    assert decided.newly_dropped == (BAD,)
    assert decided.carried == (CARRIED,)


def test_nothing_to_compare_keeps_only_the_carried_cells() -> None:
    decided = drop_cells.decide((), (CARRIED,), composite=COMPOSITE, b3="", days=())
    assert decided.dropped == (CARRIED,)
    assert decided.summary()["improvement"] is None


def test_the_summary_counts_and_lists_the_cells() -> None:
    summary = _decision().summary()
    assert summary["compared"] == 3
    assert summary["worse"] == 1
    assert summary["dropped"] == [list(BAD)]
    # 件数が同じなので、改善は (0.1 + 0.3 + 0.2) / (0.2 + 0.2 + 0.2) から
    assert summary["improvement"] == pytest.approx(0.0)


def test_drop_lines_match_the_drop_file_format() -> None:
    """**`build_composite --drop-file` と同じ 1 行 1 セル**（`系統 ターゲット 水平 バケツ`）。"""
    assert drop_cells.drop_lines([BAD]) == ["docomo-cycle dock 120 11+"]
