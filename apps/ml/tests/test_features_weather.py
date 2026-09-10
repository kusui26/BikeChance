"""天気の引き方（`features/weather.py`）。

**主題は 1 つ。** 基準時刻 `t` で使ってよいのは `available_at <= t` の発行だけ、という
規律が守られていること（W4 プラン §6.4 の完了条件）。

いちばん強い検査は `test_a_later_issue_does_not_change_the_past` で、
**「未来の発行を足しても過去の値が動かない」**を直接確かめる。リークがあれば必ず落ちる。
"""

import math
from datetime import UTC, date, datetime, timedelta
from typing import Final

import numpy as np
import pytest

from bikechance_ml.features import weather
from bikechance_ml.features.arrays import Int64
from bikechance_ml.features.constants import WEATHER_LEAD_HOURS
from bikechance_ml.features.grid import to_epoch_ms

CELL: Final[tuple[int, int]] = (714, 2236)
OTHER: Final[tuple[int, int]] = (713, 2235)


def _row(hour: int, cell: tuple[int, int] = CELL, offset: float = 0.0) -> weather.WeatherRow:
    """発行 `hour` 時（UTC）ぶんの 1 行。**値から発行と lead が読める。**"""
    issued = datetime(2026, 9, 10, hour, tzinfo=UTC)
    return weather.WeatherRow(
        cell_lat_idx=cell[0],
        cell_lon_idx=cell[1],
        issued_hour=issued,
        available_at=issued + timedelta(minutes=18),
        values={
            name: [round(hour + k / 10 + offset, 2) for k in range(WEATHER_LEAD_HOURS)]
            for name in weather.SERIES
        },
    )


def _at(hour: int, minute: int = 0) -> Int64:
    return np.array([to_epoch_ms(datetime(2026, 9, 10, hour, minute, tzinfo=UTC))], dtype=np.int64)


# ── 発行の選び方 ──────────────────────────────────────────────
def test_it_uses_the_newest_issue_that_was_already_available() -> None:
    forecast = weather.to_weather([_row(1), _row(2), _row(3)])
    assert forecast.issue_at(_at(2, 30)).tolist() == [1]  # 02:18 は入手済み、03:18 はまだ
    assert forecast.issue_at(_at(3, 30)).tolist() == [2]


def test_it_refuses_an_issue_that_was_not_yet_available() -> None:
    """**毎正時から 18 分のあいだが罠。** `issued_hour <= t` で引くと未発行を使う。"""
    forecast = weather.to_weather([_row(1), _row(2)])
    # 02:10 は「02:00 発行」の時刻を過ぎているが、入手は 02:18。使ってよいのは 01:00 発行
    assert forecast.issue_at(_at(2, 10)).tolist() == [0]


def test_there_is_no_issue_before_the_first_one() -> None:
    forecast = weather.to_weather([_row(5)])
    assert forecast.issue_at(_at(4)).tolist() == [weather.ABSENT]


def test_an_empty_forecast_gives_no_issue() -> None:
    assert weather.empty().issue_at(_at(12)).tolist() == [weather.ABSENT]


def test_a_later_issue_does_not_change_the_past() -> None:
    """**あとから入手する発行を足しても、それより前の基準時刻は動かない。**

    これが「リークが無い」の定義そのものである。基準時刻はすべて 02:18（足す発行の
    入手時刻）より前に置いてあるので、値が 1 つでも動いたら未来を覗いている。
    """
    times = np.array(
        [
            to_epoch_ms(datetime(2026, 9, 10, hour, minute, tzinfo=UTC))
            for hour, last in ((1, 60), (2, 20))
            for minute in range(0, last, 5)
        ],
        dtype=np.int64,
    )
    early = weather.to_weather([_row(0), _row(1)])
    late = weather.to_weather([_row(0), _row(1), _row(2), _row(3)])
    assert late.available_ms[2] > times[-1], "仕込みが甘い（足した発行が既に入手済み）"
    cell = np.zeros(len(times), dtype=np.int32)
    hours = weather.containing_hour_ms(times)
    before = early.value("precip_mm", early.issue_at(times), cell, hours)
    after = late.value("precip_mm", late.issue_at(times), cell, hours)
    assert np.array_equal(before, after, equal_nan=True)


def test_the_leak_guard_fires_when_the_index_is_wrong() -> None:
    """見張りが効いているか。**規律そのものではなく、壊れたときに気づくためのもの。**"""
    forecast = weather.to_weather([_row(1), _row(5)])
    with pytest.raises(weather.WeatherLeakError):
        forecast._refuse_leak(np.array([1], dtype=np.int32), _at(2))


# ── 格子の選び方 ──────────────────────────────────────────────
def test_a_port_finds_its_own_cell() -> None:
    forecast = weather.to_weather([_row(1, OTHER, 100.0), _row(1, CELL)])
    found = forecast.cell_at(np.array([35.68]), np.array([139.76]))
    assert found.tolist() == [forecast.cells[CELL]]


def test_a_port_outside_the_archived_cells_gets_nothing() -> None:
    """**当たらないポートがある**（実測 4 件）。借りずに NULL にする。"""
    forecast = weather.to_weather([_row(1)])
    assert forecast.cell_at(np.array([43.06]), np.array([141.35])).tolist() == [weather.ABSENT]


def test_a_port_without_coordinates_gets_nothing() -> None:
    forecast = weather.to_weather([_row(1)])
    assert forecast.cell_at(np.array([math.nan]), np.array([math.nan])).tolist() == [weather.ABSENT]


def test_the_cell_key_rounds_like_postgres() -> None:
    """**`weather_grid_cells` と同じ丸め**でなければ、取った格子と引く格子がずれる。

    Postgres の `round(double precision)` は偶数丸めで、ちょうど半端になる実データが
    1 件ある（`136.65625 / 0.0625 = 2186.5` → 2186）。
    """
    assert weather.cell_key(36.567493, 136.65625) == (731, 2186)


# ── 時間帯の選び方 ────────────────────────────────────────────
def test_precipitation_uses_the_hour_that_contains_t() -> None:
    """降水量は「直前 1 時間の合計」なので、**ラベルは `t` 以上で最小の毎正時**。"""
    assert weather.containing_hour_ms(_at(2, 10)).tolist() == _at(3).tolist()
    assert weather.containing_hour_ms(_at(2, 0)).tolist() == _at(2).tolist()
    assert weather.containing_hour_ms(_at(2, 59)).tolist() == _at(3).tolist()


def test_instant_values_use_the_nearest_hour() -> None:
    """気温と風速は瞬時値なので、**近いほう**を引く（1 時間先の値にしない）。"""
    assert weather.nearest_hour_ms(_at(2, 10)).tolist() == _at(2).tolist()
    assert weather.nearest_hour_ms(_at(2, 30)).tolist() == _at(3).tolist()
    assert weather.nearest_hour_ms(_at(2, 29)).tolist() == _at(2).tolist()


def test_the_two_rules_differ_in_the_first_half_of_an_hour() -> None:
    """**同じ添字で引くと気温が最大 1 時間ずれる。** ここが分かれ目。"""
    assert (
        weather.containing_hour_ms(_at(2, 5)).tolist()
        != weather.nearest_hour_ms(_at(2, 5)).tolist()
    )


# ── 値の引き方 ────────────────────────────────────────────────
def _value(forecast: weather.Weather, at: Int64, cell: tuple[int, int]) -> float:
    issue = forecast.issue_at(at)
    index = np.array([forecast.cells.get(cell, weather.ABSENT)], dtype=np.int32)
    return float(forecast.value("precip_mm", issue, index, weather.containing_hour_ms(at))[0])


def test_it_reads_the_lead_that_matches_the_hour() -> None:
    forecast = weather.to_weather([_row(1)])
    # 発行 01:00、基準 02:10 → 含む時間帯は 03:00 → lead 2 → 1 + 0.2
    assert _value(forecast, _at(2, 10), CELL) == pytest.approx(1.2)


def test_it_does_not_borrow_another_cell() -> None:
    """囮の格子は +100 してある。**取り違えたら一目で分かる。**"""
    forecast = weather.to_weather([_row(1, OTHER, 100.0), _row(1, CELL)])
    assert _value(forecast, _at(2, 10), CELL) == pytest.approx(1.2)
    assert _value(forecast, _at(2, 10), OTHER) == pytest.approx(101.2)


def test_a_missing_cell_gives_nan() -> None:
    forecast = weather.to_weather([_row(1)])
    assert math.isnan(_value(forecast, _at(2, 10), OTHER))


def test_an_hour_beyond_the_array_gives_nan() -> None:
    """**配列より先は NULL。** 端の値で埋めない。"""
    forecast = weather.to_weather([_row(1)])
    assert math.isnan(_value(forecast, _at(1, 30) + 9 * 3_600_000, CELL))


def test_a_null_in_the_archive_stays_null() -> None:
    row = _row(1)
    holed = weather.WeatherRow(
        cell_lat_idx=row.cell_lat_idx,
        cell_lon_idx=row.cell_lon_idx,
        issued_hour=row.issued_hour,
        available_at=row.available_at,
        values={
            name: [None if k == 2 else one for k, one in enumerate(values)]
            for name, values in row.values.items()
        },
    )
    assert math.isnan(_value(weather.to_weather([holed]), _at(2, 10), CELL))


def test_a_cell_missing_from_one_issue_gives_nan_only_there() -> None:
    """**発行ごとに格子が欠けうる**（6 分割のうち 1 つだけ取り込めた、など）。"""
    forecast = weather.to_weather([_row(1, CELL), _row(3, OTHER, 100.0)])
    assert _value(forecast, _at(2, 10), CELL) == pytest.approx(1.2)
    assert math.isnan(_value(forecast, _at(4, 10), CELL))


# ── 窓と定数 ──────────────────────────────────────────────────
def test_the_lead_hours_cover_two_missed_fetches() -> None:
    """`WEATHER_LEAD_HOURS` の根拠を機械で押さえる（**縮めたら落ちる**）。"""
    assert weather.required_lead_hours(0) == 6
    assert weather.required_lead_hours(missed_issues=2) == WEATHER_LEAD_HOURS


def test_the_serving_window_never_reaches_the_future() -> None:
    """**上限は基準時刻そのもの。** 未来の発行は読みにすら行かない。"""
    at = datetime(2026, 9, 10, 5, 35, tzinfo=UTC)
    start, end = weather.serving_window(at)
    assert end == at
    assert start < at


def test_the_training_window_covers_the_day_and_reaches_back() -> None:
    start, end = weather.training_window(date(2026, 9, 10))
    assert start < datetime(2026, 9, 9, 15, tzinfo=UTC)  # JST の 00:00 より前
    assert end == datetime(2026, 9, 10, 15, tzinfo=UTC)  # 翌日の JST 00:00
