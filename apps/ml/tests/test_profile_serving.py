"""推論が読むプロファイルの決め方（`features/profile.py`、W6 の PR D）。

**`serving_cells`・`wanted_groups`・`check_edition` の 3 つ**を固める。推論の入口を通した
「要るセルだけを読んでも、全部を読んだときと同じ特徴量が出る」は `test_infer.py` が見る。

  * **1 周期に要るセルは 7〜8 個**。曜日種別は目標時刻の日で決まり、翌日にまたぐのは 21:00 から
  * **契約 28 の揃えでない版は選ばずに止める**（前の並びの版を丸ごと落とさない）
  * **読んだ版は、組み立ての前に形を確かめる**（組み立ての途中で止まると B3 の配信まで止まる）
"""

from datetime import UTC, date, datetime, timedelta
from typing import Final

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from bikechance_ml.features import profile
from bikechance_ml.features.constants import GRID_MINUTES, GRID_POINTS_PER_DAY, HORIZONS_MIN
from bikechance_ml.features.grid import JST
from bikechance_ml.jobs.build_features import to_parquet_bytes
from bikechance_ml.jobs.build_profiles import DELTA_ENCODED, profile_bytes
from bikechance_ml.jobs.snapshot_table import COMPRESSION
from tests import range_fixture

#: 2026-09-23（水）は秋分の日。**前日の 22 日（火）の夜は、祝日にまたぐ。**
HOLIDAY: Final[date] = date(2026, 9, 23)
WEDNESDAY: Final[date] = date(2026, 9, 9)
FRIDAY: Final[date] = date(2026, 9, 11)

#: 1 日の分の数（0:00 に届いたら翌日）。
LAST_MINUTE_OF_DAY: Final[int] = 24 * 60

#: 翌日にまたぐ最初の周期（**180 分先が 0:00 に届く** 21:00。0:00 ちょうどは翌日の枠 0）。
FIRST_CROSSING_MINUTE: Final[int] = LAST_MINUTE_OF_DAY - max(HORIZONS_MIN)


def _at(day: date, minute: int) -> datetime:
    """JST のその日の `minute` 分（UTC で返す。推論の基準時刻と同じ持ち方）。"""
    return (
        datetime(day.year, day.month, day.day, tzinfo=JST) + timedelta(minutes=minute)
    ).astimezone(UTC)


def _minutes() -> list[int]:
    return [step * GRID_MINUTES for step in range(GRID_POINTS_PER_DAY)]


# ── serving_cells ─────────────────────────────────────────────
def test_every_cycle_needs_seven_or_eight_cells() -> None:
    """**1 日の 288 周期のどれも 7〜8 セル**（10 水平が 15 分枠に畳まれる。W5 の測り ①）。"""
    counts = {
        len(profile.serving_cells(_at(WEDNESDAY, minute), frozenset())) for minute in _minutes()
    }
    assert counts <= {7, 8}


def test_the_slots_are_the_target_slots() -> None:
    """**枠は `target_slot`**（B2 と `prof_*` が同じ規則で引く 1 か所。W5-01）。"""
    for minute in _minutes():
        slots = {slot for _, slot in profile.serving_cells(_at(WEDNESDAY, minute), frozenset())}
        expected = set(profile.target_slot(minute, np.asarray(HORIZONS_MIN)).tolist())
        assert slots == expected, minute


def test_only_the_last_three_hours_reach_into_tomorrow() -> None:
    """**翌日の曜日種別が入るのは 21:00 から**（水平は最長 180 分）。金曜の夜は土曜が入る。

    **23:55 はすべて翌日**——いちばん短い 5 分先でも 0:00 に届く。
    """
    for minute in _minutes():
        kinds = {kind for kind, _ in profile.serving_cells(_at(FRIDAY, minute), frozenset())}
        assert kinds == _expected_kinds(minute), minute


def _expected_kinds(minute: int) -> set[str]:
    if minute + min(HORIZONS_MIN) >= LAST_MINUTE_OF_DAY:
        return {"sat"}
    return {"weekday", "sat"} if minute >= FIRST_CROSSING_MINUTE else {"weekday"}


def test_the_night_before_a_holiday_reaches_the_holiday() -> None:
    """**祝日の前夜は `sun_holiday` の枠を引く**（曜日種別は目標時刻の日で決める）。"""
    at = _at(HOLIDAY - timedelta(days=1), 23 * 60)
    cells = profile.serving_cells(at, frozenset({HOLIDAY}))
    assert ("sun_holiday", 0) in cells
    assert ("weekday", 92) in cells
    assert {kind for kind, _ in cells} == {"weekday", "sun_holiday"}


def test_the_cells_are_sorted_and_unique() -> None:
    cells = profile.serving_cells(_at(FRIDAY, 22 * 60), frozenset())
    assert list(cells) == sorted(set(cells))


# ── wanted_groups ─────────────────────────────────────────────
PORTS: Final[tuple[tuple[str, str], ...]] = (("docomo-cycle", "d1"), ("hellocycling", "h1"))
TABLE: Final[pa.Table] = range_fixture.full_profile(PORTS)


def _metadata(body: bytes) -> pq.FileMetaData:
    return pq.ParquetFile(pa.BufferReader(body)).metadata


def test_the_groups_holding_the_cells_are_chosen() -> None:
    """**選ぶのは、そのセルを持つ行群だけ**（番号は昇順）。"""
    metadata = _metadata(profile_bytes(TABLE))
    cells = (("weekday", 40), ("sat", 0), ("weekday", 32))
    groups = profile.wanted_groups(metadata, cells)
    assert list(groups) == sorted(groups)
    keys = {
        (row["dow_type"], row["slot15"])
        for index in groups
        for row in pq.ParquetFile(pa.BufferReader(profile_bytes(TABLE)))
        .read_row_group(index)
        .to_pylist()
    }
    assert keys == set(cells)


def test_cells_that_the_file_does_not_have_choose_nothing() -> None:
    small = TABLE.slice(0, 3)
    assert profile.wanted_groups(_metadata(profile_bytes(small)), (("weekday", 40),)) == ()


def test_a_file_in_the_old_order_is_refused() -> None:
    """**前の並び（ポートが先）の版は選ばずに止める**——どの行群も全枠を含み、丸ごと落とす。"""
    old = TABLE.sort_by([(one, "ascending") for one in profile.KEY_COLUMNS])
    with pytest.raises(profile.LayoutError, match="契約 28"):
        profile.wanted_groups(_metadata(to_parquet_bytes(old)), (("weekday", 40),))


def test_a_file_without_key_statistics_is_refused() -> None:
    """**鍵の統計が無ければ選べない**——当て推量で全部を読まない。"""
    sink = pa.BufferOutputStream()
    with pq.ParquetWriter(
        sink, TABLE.schema, compression=COMPRESSION, write_statistics=False
    ) as writer:
        for start, stop in profile.row_groups(TABLE):
            writer.write_table(TABLE.slice(start, stop - start), row_group_size=stop - start)
    with pytest.raises(profile.LayoutError, match="統計"):
        profile.wanted_groups(_metadata(bytes(sink.getvalue())), (("weekday", 40),))


def test_the_real_writer_keeps_the_keys_readable() -> None:
    """**本番の書き手（差分で書く `station_id` を含む）でも選べる**（PR C の書き方の約束）。"""
    assert "station_id" in DELTA_ENCODED
    assert len(profile.wanted_groups(_metadata(profile_bytes(TABLE)), (("sun_holiday", 95),))) == 1


# ── check_edition ─────────────────────────────────────────────
def _edition(table: pa.Table) -> profile.Edition:
    return profile.Edition(day=date(2026, 9, 26), table=table)


def test_a_good_edition_passes() -> None:
    profile.check_edition(_edition(TABLE))


def test_a_cell_written_twice_is_caught() -> None:
    """**同じセルが 2 行ある版は引けない**（黙って片方を使わない）。"""
    doubled = pa.concat_tables([TABLE, TABLE.slice(0, 1)])
    with pytest.raises(profile.CorruptProfileError):
        profile.check_edition(_edition(doubled))


def test_an_edition_with_a_missing_column_is_caught() -> None:
    with pytest.raises(profile.SchemaMismatchError):
        profile.check_edition(_edition(TABLE.drop_columns(["n_days"])))
