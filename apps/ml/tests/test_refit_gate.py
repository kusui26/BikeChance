"""当てはめ直しの前提の門（`jobs/refit_gate.py`。W8-01、W8 の PR G）。

**主題は「揃うまで待ち、揃わなければ 3 時間で諦めて理由を言うこと」**。日次の枠は毎朝 2〜3 時間
遅れる。来ないものを待ち続けても、待たずに古い日で当てはめても困る。

  * 3 つ（学習サンプル・プロファイル・日次評価）が**その日の日付で** `ok` になるまで待つ
  * 待つ間隔は 10 分、**最後に確かめるのはちょうど 3 時間**
  * 諦めたら、待った秒と揃わなかったものを持つ
"""

from datetime import UTC, date, datetime, timedelta
from typing import Final

import pytest

from bikechance_ml.jobs import refit_gate
from tests.test_dry_run import FakeClock

DAY: Final[date] = date(2026, 11, 1)
START: Final[datetime] = datetime(2026, 11, 2, 1, 30, tzinfo=UTC)


class FakeRuns:
    """`job_runs` の `ok` の行。**`arrive` に書いた時刻を過ぎたら見える。**"""

    def __init__(self, clock: FakeClock, arrive: dict[str, datetime]) -> None:
        self.clock = clock
        self.arrive = arrive
        self.asked: list[tuple[str, str, date]] = []

    def latest_ok_run(self, job_name: str, date_key: str, day: date) -> datetime | None:
        self.asked.append((job_name, date_key, day))
        at = self.arrive.get(job_name)
        return at if at is not None and at <= self.clock.now() and day == DAY else None


def _all_at(at: datetime) -> dict[str, datetime]:
    return {one.job_name: at for one in refit_gate.REQUIREMENTS}


def test_the_three_requirements_and_their_date_keys() -> None:
    """**日次評価の日付は `metric_date`**（ほかの 2 つは `date`。本番の `detail` の形）。"""
    assert {(one.job_name, one.date_key) for one in refit_gate.REQUIREMENTS} == {
        ("build_features", "date"),
        ("build_profiles", "date"),
        ("evaluate_daily", "metric_date"),
    }


def test_everything_ready_passes_without_waiting() -> None:
    clock = FakeClock(START)
    runs = FakeRuns(clock, _all_at(START - timedelta(hours=2)))
    passed = refit_gate.wait(runs, clock, DAY)
    assert passed.waited_s == 0
    assert clock.slept == []
    assert set(passed.arrived) == {one.job_name for one in refit_gate.REQUIREMENTS}
    assert {asked[2] for asked in runs.asked} == {DAY}


def test_a_late_job_is_waited_for_in_ten_minute_steps() -> None:
    clock = FakeClock(START)
    arrive = {**_all_at(START), "evaluate_daily": START + timedelta(minutes=25)}
    passed = refit_gate.wait(FakeRuns(clock, arrive), clock, DAY)
    assert clock.slept == [600, 600, 600]
    assert passed.waited_s == 1800


def test_it_gives_up_after_exactly_three_hours() -> None:
    """**最後に確かめるのは 3 時間ちょうど**（10 分 × 18 回待った後）。"""
    clock = FakeClock(START)
    arrive = {"build_features": START, "build_profiles": START}
    with pytest.raises(refit_gate.GateTimeoutError) as caught:
        refit_gate.wait(FakeRuns(clock, arrive), clock, DAY)
    assert caught.value.waited_s == refit_gate.WAIT_S
    assert caught.value.missing == ("evaluate_daily",)
    assert len(clock.slept) == refit_gate.WAIT_S // refit_gate.POLL_S


def test_a_job_arriving_at_the_last_check_still_passes() -> None:
    clock = FakeClock(START)
    arrive = {**_all_at(START), "build_features": START + timedelta(seconds=refit_gate.WAIT_S)}
    passed = refit_gate.wait(FakeRuns(clock, arrive), clock, DAY)
    assert passed.waited_s == refit_gate.WAIT_S


def test_ok_rows_for_another_day_do_not_count() -> None:
    """**日付で見る**（土曜の学習サンプルで日曜の回を始めない）。"""
    clock = FakeClock(START)
    runs = FakeRuns(clock, _all_at(START))
    with pytest.raises(refit_gate.GateTimeoutError) as caught:
        refit_gate.wait(runs, clock, DAY + timedelta(days=1), wait_s=600, poll_s=600)
    assert set(caught.value.missing) == {one.job_name for one in refit_gate.REQUIREMENTS}
