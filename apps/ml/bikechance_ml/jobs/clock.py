"""待つ仕事の時計（W8 の PR G。前提の門と試し打ちが使う）。

**待つのも時計を通す**——試験では進め方を決められ、本物の時間を使わずに「3 時間待って来ない」を
確かめられる。
"""

import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """いまの時刻と、待つこと。"""

    def now(self) -> datetime: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """本物の時計（UTC）。"""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)
