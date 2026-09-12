"""基準時刻のグリッドと、読み込む Parquet の時間帯（W3 プラン §4.3 の 23、§9.3）。

**グリッドを作る関数はこの 1 か所だけにする。** 基準時刻は **JST 00:00 起点**、
Parquet のパスは **UTC**。両方をあちこちで書くと、日付の境界がずれて静かに壊れる。

時刻は**エポックミリ秒の整数**で持ち回る。numpy の配列に載せたときに型が揺れず、
`datetime` の比較より速い。境界で `datetime` に戻すのはこのモジュールの仕事。
"""

from collections.abc import Mapping, Sequence
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
JST_OFFSET_MS: Final[int] = 9 * 60 * 60 * 1000
JST: Final[timezone] = timezone(timedelta(milliseconds=JST_OFFSET_MS), "JST")

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


def jst_yesterday(now: datetime) -> date:
    """**JST の昨日。** 日次ジョブが「前日ぶん」を作るときの既定の対象。

    参照スナップショット（05:00 JST）も学習サンプル（06:00 JST）も、**前日ぶん**を
    作る。どちらも JST の暦日で切るので、ここで 1 か所にまとめる。
    """
    return jst_date(now) - timedelta(days=1)


def jst_minute_of_day(at: datetime) -> int:
    """JST の 0 時からの経過分（0〜1439）。"""
    local = at.astimezone(JST)
    return local.hour * 60 + local.minute


def day_start(day: date) -> datetime:
    """その JST 暦日の 00:00（UTC の `datetime` として返す）。"""
    return datetime(day.year, day.month, day.day, tzinfo=JST).astimezone(UTC)


class MissingShiftError(KeyError):
    """疎な格子に、要求されたずらし量が無い。**黙って別の点を返さない。**

    推論の格子は「要る点だけ」を持つ（`build_point_grid`）。特徴量を足して新しい
    ずらし量が要るようになったのに格子を直し忘れると、ここで止まる。止めなければ
    **別の時刻の値が特徴量に入る**（W4 プラン §6.3）。
    """


@dataclass(frozen=True)
class Grid:
    """基準時刻の並びと、そこから `minutes` ずらした位置の引き方。

    **添字の算術をここに閉じ込める。** 学習は当日 288 点の連続格子（前後に余白）、
    推論は 1 点だけの疎な格子で、**どちらも同じ `shifted()` で引ける**
    （W4 プラン §6.3）。`features/build.py` は格子の作り方を知らない。

    `base_offset` から `base_offset + base_count` が基準時刻にあたる。
    """

    times_ms: tuple[int, ...]
    base_offset: int
    base_count: int
    #: ずらし量（分）→ `times_ms` の先頭の添字。**疎な格子のときだけ持つ。**
    #: 連続格子は歩数で引けるので None。
    shifts: Mapping[int, int] | None = None

    def __len__(self) -> int:
        return len(self.times_ms)

    def day_slice(self) -> Span:
        """基準時刻を指す（ずらし 0）。"""
        return self.shifted(0)

    def day_times_ms(self) -> tuple[int, ...]:
        return self.times_ms[self.day_slice()]

    def shifted(self, minutes: int) -> Span:
        """基準時刻の集合を `minutes` ずらした位置。**負は過去。**

        連続格子では歩数、疎な格子では持っている表で引く。**どちらも `Span` を返す**
        ので、呼ぶ側は numpy の view のまま扱える（複製が起きない）。
        """
        start = self._index_of(minutes)
        return slice(start, start + self.base_count, 1)

    def _index_of(self, minutes: int) -> int:
        if self.shifts is None:
            return self.base_offset + self.steps_per(minutes)
        found = self.shifts.get(minutes)
        if found is None:
            raise MissingShiftError(f"{minutes} 分ずらした格子点を持っていません")
        return found

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
    """当日の 288 点と前後の余白を並べる（学習）。"""
    start = day_start(day)
    before = lookback_hours * 60 // GRID_MINUTES
    after = lookahead_hours * 60 // GRID_MINUTES
    first = to_epoch_ms(start) - before * GRID_MINUTES * _MS_PER_MINUTE
    total = before + GRID_POINTS_PER_DAY + after
    step = GRID_MINUTES * _MS_PER_MINUTE
    return Grid(tuple(first + index * step for index in range(total)), before, GRID_POINTS_PER_DAY)


def build_point_grid(at: datetime, shifts: Sequence[int]) -> Grid:
    """基準時刻 1 点と、そこから要るぶんだけの格子（推論）。

    **要る点だけを持つ。** 連続した格子にすると 1,500 分ぶん（337 点）を並べることに
    なり、`(ポート × 格子点)` の行列が 10 倍以上になる。実際に引くのは 24 点である。

    `at` は **5 分格子の上**でなければならない。ずれた時刻を基準にすると、ラグや
    同時刻履歴が格子から外れて `MissingShiftError` になる前に、静かに別の点を指す。
    """
    if to_epoch_ms(at) % (GRID_MINUTES * _MS_PER_MINUTE) != 0:
        raise ValueError(f"基準時刻が {GRID_MINUTES} 分格子の上にありません: {at.isoformat()}")
    base_ms = to_epoch_ms(at)
    wanted = sorted({0, *shifts})
    times = tuple(base_ms + minutes * _MS_PER_MINUTE for minutes in wanted)
    index = {minutes: position for position, minutes in enumerate(wanted)}
    return Grid(times, index[0], 1, index)


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


def profile_path(day: date, name: str) -> str:
    """ポートプロファイルのパス（W5 プラン §6.2）。**JST の暦日**。

    `name` は `daily`（その日ぶんの素の集計）か `profile`（直近 28 日の累計）。
    参照スナップショットと同じく `gbfs-parquet` に置く（MIME は Parquet で通る）。

    **読む規則は「基準時刻の前日の版」**で、参照スナップショットと同じである
    （開発プラン §6.2 のリーク防止：プロファイルは前日 23:59 までのデータで作る）。
    """
    return f"profiles/date={day:%Y-%m-%d}/{name}.parquet"


def reference_path(day: date, name: str) -> str:
    """参照スナップショットのパス（W3 プラン §14.3）。**JST の暦日**。

    `name` は `stations` か `neighbors`。既存の `gbfs-parquet` バケットに置く
    （MIME の許可リストに収まるのでバケットを増やさない。§12 の 106）。
    """
    return f"reference/date={day:%Y-%m-%d}/{name}.parquet"


def day_hours(day: date) -> tuple[datetime, ...]:
    """その JST 暦日ちょうどを覆う **UTC の正時** 24 個。

    JST の 00:00 は UTC の 15:00 ちょうどなので、丸めずに 24 時間で過不足なく覆える。
    `parquet_hours` と違って**前後の余白を取らない**：ここが欲しいのは「その日の
    観測だけ」で、as-of のために前を覗く必要が無いため。
    """
    start = day_start(day)
    return tuple(start + timedelta(hours=offset) for offset in range(24))
