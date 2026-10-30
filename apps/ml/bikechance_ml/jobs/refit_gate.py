"""当てはめ直しの前提の門（W8 の PR G、W8-01）。

**時刻ではなく `job_runs` の `ok` で始める**（W6 プランの所見 203）。日次の枠は毎朝 2〜3 時間
遅れる（10/27 は 06:00 の枠が 09:08 に始まった）。月曜の朝の回は、**最終学習日の 3 つ**が
揃うまで待つ。

| ジョブ | 何に要るか | 日付の欄 |
|---|---|---|
| `build_features` | B3 の学習サンプル・評価 | `detail.date` |
| `build_profiles` | B2（気候値）・点検 | `detail.date` |
| `evaluate_daily` | 削るセル（その日の成績まで読む。W6-12） | `detail.metric_date` |

`POLL_S` おきに `WAIT_S` まで待ち、揃わなければ `GateTimeoutError`（揃わなかったものを持つ）。
**門で待つ間は `job_runs` に行を作らない**——`running` のまま 1 時間を越えると「閉じられない記録」
として鳴り（0056）、3 時間待ってから失敗にすると「直近 3 時間の失敗」の見張り（0041）が
始まりの時刻で外してしまう。記録の始まりは門を通った後（`jobs/weekly_refit.py`）。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final, Protocol

from bikechance_ml.jobs.clock import Clock


@dataclass(frozen=True)
class Requirement:
    """門で待つ 1 つ。**どのジョブの、どの欄が最終学習日か。**"""

    job_name: str
    date_key: str


#: 待つもの（上の表）。
REQUIREMENTS: Final[tuple[Requirement, ...]] = (
    Requirement("build_features", "date"),
    Requirement("build_profiles", "date"),
    Requirement("evaluate_daily", "metric_date"),
)

#: 待つ上限（秒）。10:30 に始めて 13:30 まで（W8-01）。
WAIT_S: Final[int] = 3 * 60 * 60

#: 確かめる間隔（秒）。
POLL_S: Final[int] = 10 * 60


class FindsRuns(Protocol):
    """`job_runs` を引く口（`SupabaseIo`）。"""

    def latest_ok_run(self, job_name: str, date_key: str, day: date) -> datetime | None: ...


@dataclass(frozen=True)
class GateResult:
    """門を通った。**いつ揃ったか**と、待った秒。"""

    day: date
    waited_s: int
    arrived: Mapping[str, datetime]


class GateTimeoutError(RuntimeError):
    """待つ上限までに揃わなかった。"""

    def __init__(self, day: date, waited_s: int, missing: tuple[str, ...]) -> None:
        self.day = day
        self.waited_s = waited_s
        self.missing = missing
        super().__init__(f"{day} の {', '.join(missing)} が {waited_s} 秒待っても ok になりません")


def check(port: FindsRuns, day: date) -> tuple[tuple[str, ...], dict[str, datetime]]:
    """いま揃っていないもの（ジョブ名）と、揃ったものの始まりの時刻。"""
    found = {
        one.job_name: port.latest_ok_run(one.job_name, one.date_key, day) for one in REQUIREMENTS
    }
    missing = tuple(name for name, at in found.items() if at is None)
    return missing, {name: at for name, at in found.items() if at is not None}


def wait(
    port: FindsRuns, clock: Clock, day: date, *, wait_s: int = WAIT_S, poll_s: int = POLL_S
) -> GateResult:
    """揃うまで待つ。**最後に確かめるのは、ちょうど `wait_s` を待った時**。"""
    started = clock.now()
    while True:
        missing, arrived = check(port, day)
        waited_s = int((clock.now() - started).total_seconds())
        if not missing:
            return GateResult(day=day, waited_s=waited_s, arrived=arrived)
        if waited_s + poll_s > wait_s:
            raise GateTimeoutError(day, waited_s, missing)
        clock.sleep(poll_s)
