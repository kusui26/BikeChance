"""基準時刻のグリッドと Parquet の時間帯（`features/grid.py`、W3 プラン §4.3 の 23）。

**この 1 ファイルの主題は「JST と UTC を取り違えないこと」。** 基準時刻は JST 00:00
起点、Parquet のパスは UTC。ここがずれると 1 日の境界が静かに 9 時間ずれる。
"""

from datetime import UTC, date, datetime

import pytest

from bikechance_ml.features.constants import GRID_MINUTES, GRID_POINTS_PER_DAY
from bikechance_ml.features.grid import (
    JST,
    build_grid,
    day_start,
    features_path,
    from_epoch_ms,
    jst_date,
    jst_minute_of_day,
    parquet_hours,
    to_epoch_ms,
)

DAY = date(2026, 9, 7)


def test_day_starts_at_midnight_jst() -> None:
    """当日の先頭は **JST の 00:00**（＝前日 15:00 UTC）。"""
    assert day_start(DAY) == datetime(2026, 9, 6, 15, 0, tzinfo=UTC)


def test_grid_has_288_points_for_the_day() -> None:
    grid = build_grid(DAY)
    times = grid.day_times_ms()
    assert len(times) == GRID_POINTS_PER_DAY == 288
    assert from_epoch_ms(times[0]) == day_start(DAY)
    assert jst_minute_of_day(from_epoch_ms(times[0])) == 0
    assert jst_minute_of_day(from_epoch_ms(times[-1])) == 24 * 60 - GRID_MINUTES


def test_grid_has_margins_before_and_after() -> None:
    """余白はラグ・流量（前）とラベル（後）のためにある。"""
    grid = build_grid(DAY, lookback_hours=2, lookahead_hours=1)
    assert grid.base_offset == 2 * 60 // GRID_MINUTES
    assert grid.base_count == GRID_POINTS_PER_DAY
    assert len(grid) == grid.base_offset + GRID_POINTS_PER_DAY + 60 // GRID_MINUTES
    assert grid.times_ms[grid.day_slice()][0] == to_epoch_ms(day_start(DAY))


def test_steps_per_rejects_values_off_the_grid() -> None:
    """**グリッドの倍数でない分は例外にする。** 静かに丸めると水平がずれる。"""
    grid = build_grid(DAY)
    assert grid.steps_per(60) == 12
    with pytest.raises(ValueError, match="倍数"):
        grid.steps_per(7)


def test_timezone_is_required() -> None:
    with pytest.raises(ValueError, match="タイムゾーン"):
        to_epoch_ms(datetime(2026, 9, 7, 0, 0))


def test_jst_date_of_a_utc_instant() -> None:
    """**UTC の 15:00 は JST の翌日 00:00。** 出力のパスはこちらで決まる。"""
    assert jst_date(datetime(2026, 9, 6, 15, 0, tzinfo=UTC)) == DAY
    assert jst_date(datetime(2026, 9, 6, 14, 59, tzinfo=UTC)) == date(2026, 9, 6)


def test_parquet_hours_cover_the_grid_in_utc() -> None:
    """読む時間帯は **UTC の正時**で、グリッドの前後を覆う。"""
    hours = parquet_hours(DAY, lookback_hours=1, lookahead_hours=1)
    assert all(hour.tzinfo == UTC for hour in hours)
    assert all(hour.minute == 0 for hour in hours)
    grid = build_grid(DAY, lookback_hours=1, lookahead_hours=1)
    # 端の観測を as-of で拾えるよう、先頭は 1 時間多く取る
    assert hours[0] < from_epoch_ms(grid.times_ms[0])
    assert hours[-1] <= from_epoch_ms(grid.times_ms[-1])
    assert len(set(hours)) == len(hours)


def test_output_path_uses_the_jst_date() -> None:
    """**出力は JST の暦日**（入力は UTC）。ここを揃えると混ざる。"""
    assert features_path(DAY) == "features/date=2026-09-07/part.parquet"


def test_jst_is_a_fixed_offset() -> None:
    """日本は 1951 年以降に夏時間を採っていない。**固定オフセットでよい。**"""
    assert JST.utcoffset(None) is not None
    summer = datetime(2026, 8, 1, 12, tzinfo=JST)
    winter = datetime(2026, 1, 1, 12, tzinfo=JST)
    assert summer.utcoffset() == winter.utcoffset()
