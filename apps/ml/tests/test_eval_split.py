"""時系列分割（`eval/split.py`、開発プラン §6.2）。

**この 1 ファイルの主題は「パージを飛ばさないこと」と「足りないなら止まること」。**
"""

from datetime import date

import pytest

from bikechance_ml.eval.dataset import to_samples
from bikechance_ml.eval.split import NotEnoughDaysError, mask_of, split_days
from tests import eval_fixture as fixture

DAYS = [date(2026, 9, d) for d in range(1, 11)]


def test_evaluation_is_the_newest_days() -> None:
    """**未来で当てるのだから、検証は最後の日**にする。"""
    split = split_days(DAYS, evaluate_days=2, purge_days=1)
    assert split.evaluate == (date(2026, 9, 9), date(2026, 9, 10))
    assert split.purge == (date(2026, 9, 8),)
    assert split.fit == tuple(DAYS[:7])


def test_the_three_sets_do_not_overlap() -> None:
    split = split_days(DAYS, evaluate_days=2, purge_days=1)
    assert set(split.fit) & set(split.purge) == set()
    assert set(split.purge) & set(split.evaluate) == set()
    assert set(split.fit) & set(split.evaluate) == set()
    assert len(split.fit) + len(split.purge) + len(split.evaluate) == len(DAYS)


def test_purge_can_be_zero_but_must_be_asked_for() -> None:
    """**既定は 1 日。** 0 にするなら明示的に書かせる（うっかり漏らさないため）。"""
    split = split_days(DAYS, evaluate_days=1, purge_days=0)
    assert split.purge == ()
    assert split.fit[-1] == date(2026, 9, 9)


def test_duplicate_and_unsorted_days_are_handled() -> None:
    split = split_days([DAYS[2], DAYS[0], DAYS[2], DAYS[1]], evaluate_days=1, purge_days=1)
    assert split.fit == (DAYS[0],)
    assert split.evaluate == (DAYS[2],)


def test_not_enough_days_raises() -> None:
    """**足りないまま進むと、何を測ったのか分からない表が出る。**"""
    with pytest.raises(NotEnoughDaysError, match="足りません"):
        split_days(DAYS[:2], evaluate_days=1, purge_days=1)


def test_zero_evaluation_days_is_refused() -> None:
    with pytest.raises(ValueError, match="指定できません"):
        split_days(DAYS, evaluate_days=0)


def test_mask_selects_only_the_named_days() -> None:
    """**日は JST で切る**（UTC で切ると 9 時間ずれる）。"""
    rows = [
        fixture.row(fixture.DAYS[0], "hellocycling", "a", 5, 3, 3, 1, 1, minute_of_day=5),
        fixture.row(fixture.DAYS[1], "hellocycling", "a", 5, 3, 3, 1, 1, minute_of_day=1435),
    ]
    samples = to_samples(fixture.to_table(rows))
    assert mask_of(samples, [fixture.DAYS[0]]).tolist() == [True, False]
    assert mask_of(samples, [fixture.DAYS[1]]).tolist() == [False, True]
