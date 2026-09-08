"""基準時刻のグリッドと、読み込む Parquet の時間帯（W3 プラン §4.3 の 23、§9.3）。

**グリッドを作る関数はこの 1 か所だけにする。** 基準時刻は **JST 00:00 起点**、
Parquet のパスは **UTC**。両方をあちこちで書くと、日付の境界がずれて静かに壊れる。

時刻は**エポックミリ秒の整数**で持ち回る。numpy の配列に載せたときに型が揺れず、
`datetime` の比較より速い。境界で `datetime` に戻すのはこのモジュールの仕事。
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Final

from bikechance_ml.features.arrays import Span
from bikechance_ml.features.constants import (
    GRID_MINUTES,
    GRID_POINTS_PER_DAY,
    LOOKAHEAD_HOURS,
    LOOKBACK_HOURS,
)

#: JST は **固定の +09:00**。日本は 1951 年以降に夏時間を採っていないので、
#: `zoneinfo`（tzdata の同梱が要る）を持ち込まずに固定オフセットで表す。
JST: Final[timezone] = timezone(timedelta(hours=9), "JST")

_MS_PER_MINUTE: Final[int] = 60_000


def to_epoch_ms(at: datetime) -> int:
    """`datetime` をエポックミリ秒に。**タイムゾーン必須。**"""
    if at.tzinfo is None:
        raise ValueError(f"タイムゾーンを付けてください: {at!r}")
    return int(at.timestamp() * 1000)


def from_epoch_ms(ms: int) -> datetime:
    """エポックミリ秒を UTC の `datetime` に。"""
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def jst_date(at: datetime) -> date:
    """その時刻が属する **JST の暦日**。出力のパスと暦の特徴量はこれで決まる。"""
    return at.astimezone(JST).date()


def jst_minute_of_day(at: datetime) -> int:
    """JST の 0 時からの経過分（0〜1439）。"""
    local = at.astimezone(JST)
    return local.hour * 60 + local.minute


def day_start(day: date) -> datetime:
    """その JST 暦日の 00:00（UTC の `datetime` として返す）。"""
    return datetime(day.year, day.month, day.day, tzinfo=JST).astimezone(UTC)


@dataclass(frozen=True)
class Grid:
    """基準時刻の並び。**当日の 288 点の前後に余白を持つ。**

    余白はラグ・流量（前）とラベル（後）のためで、`day_offset` から
    `day_offset + GRID_POINTS_PER_DAY` が当日の 288 点にあたる。
    """

    times_ms: tuple[int, ...]
    day_offset: int

    def __len__(self) -> int:
        return len(self.times_ms)

    def day_slice(self) -> Span:
        """当日の 288 点を指す。"""
        return slice(self.day_offset, self.day_offset + GRID_POINTS_PER_DAY, 1)

    def day_times_ms(self) -> tuple[int, ...]:
        return self.times_ms[self.day_slice()]

    def steps_per(self, minutes: int) -> int:
        """`minutes` がグリッドの何点ぶんか。**割り切れなければ例外にする。**"""
        if minutes % GRID_MINUTES != 0:
            raise ValueError(f"{minutes} 分はグリッド {GRID_MINUTES} 分の倍数ではありません")
        return minutes // GRID_MINUTES


def build_grid(
    day: date,
    lookback_hours: int = LOOKBACK_HOURS,
    lookahead_hours: int = LOOKAHEAD_HOURS,
) -> Grid:
    """当日の 288 点と前後の余白を並べる。"""
    start = day_start(day)
    before = lookback_hours * 60 // GRID_MINUTES
    after = lookahead_hours * 60 // GRID_MINUTES
    first = to_epoch_ms(start) - before * GRID_MINUTES * _MS_PER_MINUTE
    total = before + GRID_POINTS_PER_DAY + after
    step = GRID_MINUTES * _MS_PER_MINUTE
    return Grid(tuple(first + index * step for index in range(total)), before)


def parquet_hours(
    day: date,
    lookback_hours: int = LOOKBACK_HOURS,
    lookahead_hours: int = LOOKAHEAD_HOURS,
) -> tuple[datetime, ...]:
    """読み込む Parquet の時間帯（**UTC の正時**、半開区間）。

    グリッドの端の観測を as-of で拾えるように、前は 1 時間多く取る。前の余白の
    先頭ちょうどに観測が無い場合、その 1 つ前の時間帯に最新の観測がある。
    """
    grid = build_grid(day, lookback_hours, lookahead_hours)
    first = _floor_hour(from_epoch_ms(grid.times_ms[0])) - timedelta(hours=1)
    last = _floor_hour(from_epoch_ms(grid.times_ms[-1]))
    count = int((last - first).total_seconds()) // 3600 + 1
    return tuple(first + timedelta(hours=offset) for offset in range(count))


def _floor_hour(at: datetime) -> datetime:
    return at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def features_path(day: date) -> str:
    """出力のパス。**JST の暦日**（入力の Parquet は UTC）。"""
    return f"features/date={day:%Y-%m-%d}/part.parquet"
