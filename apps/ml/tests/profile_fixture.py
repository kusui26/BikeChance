"""ポートプロファイルの検査で使う、手で組み立てた観測。

**`features/profile.py` の検査（PR B）と、B2 をそこから作る検査（PR D）が同じ入口を
使う。** 片方だけが別の作り方をしていると、**接ぎ目の食い違いに気づけない**。

**形は `jobs/snapshot_table.py` の `SCHEMA` そのまま**（本番と同じ入口）。
"""

from collections.abc import Sequence
from datetime import date, timedelta
from typing import Final

import pyarrow as pa

from bikechance_ml.features import profile
from bikechance_ml.features.grid import day_start
from bikechance_ml.jobs.snapshot_table import SCHEMA as SNAPSHOT_SCHEMA

DAY: Final[date] = date(2026, 9, 7)  # 月曜
SATURDAY: Final[date] = date(2026, 9, 12)

#: `flags` の値。`features/exclude.py` の規則と合わせる。
OPEN: Final[int] = 7
SUSPENDED: Final[int] = 1

#: 収集の周期（秒）。**格子（5 分）より細かく**して、as-of が効くことを確かめる。
CADENCE_S: Final[int] = 150


def observations(
    rows: Sequence[tuple[str, str, int, int, int]], *, day: date = DAY, minutes: int = 24 * 60
) -> pa.Table:
    """`(system_id, station_id, bikes, docks, flags)` を、その日いっぱい繰り返す。"""
    start = day_start(day)
    stamps = [
        start + timedelta(seconds=step * CADENCE_S) for step in range(minutes * 60 // CADENCE_S + 1)
    ]
    return pa.table(
        {
            "system_id": [one[0] for one in rows for _ in stamps],
            "station_id": [one[1] for one in rows for _ in stamps],
            "observed_at": [at for _ in rows for at in stamps],
            "fetched_at": [at for _ in rows for at in stamps],
            "bikes": [one[2] for one in rows for _ in stamps],
            "docks": [one[3] for one in rows for _ in stamps],
            "flags": [one[4] for one in rows for _ in stamps],
            "reported_age_s": [0 for _ in rows for _ in stamps],
        },
        schema=SNAPSHOT_SCHEMA,
    )


def daily(table: pa.Table, *, day: date = DAY) -> pa.Table:
    """1 日ぶんの素の集計。**祝日表は空**（検査日は平日と土曜だけ）。"""
    return profile.build_day(profile.DayInputs(day=day, table=table, holidays=frozenset()))
