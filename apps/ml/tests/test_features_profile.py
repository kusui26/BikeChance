"""ポートプロファイル（`features/profile.py`、W5 プラン §6.2）。

**主題は「B2 と同じ量を、抽出せずに数えていること」。**

固定するのは 5 つ。

  * **1 枠 1 日あたり 3 点**（288 格子点 ÷ 96 枠）——抽出後の 1.62 行ではない
  * **除外は `features/exclude.py` と同じ**（休止だけは落とさずに数える）
  * **転がしと素直な和が一致する**（`profile(D-1) + daily(D) - daily(D-28)`）
  * **読むのは前日の版**（開発プラン §6.2 のリーク防止）
  * **割らない**——率にしてしまうと足せなくなり、分母の取り方も固定されてしまう
"""

from datetime import date, datetime, timedelta
from typing import Final

import numpy as np
import pyarrow as pa
import pytest

from bikechance_ml.features import asof, exclude, profile
from bikechance_ml.features.constants import GRID_POINTS_PER_DAY, MISSING
from bikechance_ml.features.grid import JST, build_grid, day_start, profile_path
from bikechance_ml.jobs.snapshot_table import SCHEMA as SNAPSHOT_SCHEMA

DAY: Final[date] = date(2026, 9, 7)  # 月曜
SATURDAY: Final[date] = date(2026, 9, 12)
OPEN, SUSPENDED = 7, 1

#: 収集の周期（秒）。**格子（5 分）より細かく**して、as-of が効くことを確かめる。
CADENCE_S: Final[int] = 150


def observations(
    rows: list[tuple[str, str, int, int, int]], *, day: date = DAY, minutes: int = 24 * 60
) -> pa.Table:
    """`(system_id, station_id, bikes, docks, flags)` を、その日いっぱい繰り返す。

    **形は `jobs/snapshot_table.py` の `SCHEMA` そのまま**（本番と同じ入口）。
    """
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
    return profile.build_day(profile.DayInputs(day=day, table=table, holidays=frozenset()))


def cell(table: pa.Table, station_id: str, slot15: int) -> dict[str, int]:
    """1 セルの数を取り出す。**無ければ失敗させる。** 鍵の列は返さない（数だけ見る）。"""
    rows = table.filter(
        pa.compute.and_(
            pa.compute.equal(table.column("station_id"), station_id),
            pa.compute.equal(table.column("slot15"), slot15),
        )
    )
    assert rows.num_rows == 1, f"{station_id} / {slot15} が {rows.num_rows} 件"
    return {name: int(rows.column(name)[0].as_py()) for name in profile.SUM_COLUMNS}


# ── 数える ────────────────────────────────────────────────────
def test_one_slot_gets_three_grid_points_a_day() -> None:
    """**1 枠 1 日あたり 3 点。** 抽出後の `features/` は 1.62 行しか無い（§2.3d）。"""
    built = daily(observations([("hellocycling", "a", 3, 4, OPEN)]))
    assert profile.GRID_POINTS_PER_SLOT == 3
    assert built.num_rows == profile.SLOTS_PER_DAY
    assert set(built.column("n").to_pylist()) == {3}


def test_every_slot_of_the_day_is_touched() -> None:
    """**被覆は 100%。** 抽出だと 64.1% のセルにしか触れない（§2.3d）。"""
    built = daily(observations([("hellocycling", "a", 3, 4, OPEN)]))
    assert sorted(built.column("slot15").to_pylist()) == list(range(profile.SLOTS_PER_DAY))


def test_the_grid_and_the_slots_line_up() -> None:
    """288 格子点 ÷ 96 枠 = 3。**どちらかを変えたら気づく。**"""
    assert GRID_POINTS_PER_DAY == profile.SLOTS_PER_DAY * profile.GRID_POINTS_PER_SLOT


def test_the_slot_matches_the_clock() -> None:
    """**枠は JST の時刻で決まる。** 0 時台の最初の枠が 0、23:45 の枠が 95。"""
    grid = build_grid(DAY, lookback_hours=profile.LOOKBACK_HOURS, lookahead_hours=0)
    times = [datetime.fromtimestamp(one / 1000, tz=JST) for one in grid.day_times_ms()]
    assert (times[0].hour, times[0].minute) == (0, 0)
    assert (times[-1].hour, times[-1].minute) == (23, 55)
    assert len(times) // profile.GRID_POINTS_PER_SLOT == profile.SLOTS_PER_DAY


def test_the_labels_come_from_labels_py() -> None:
    """**`n_bike_ok` は `y_bike` そのもの**（B2 が当てるラベルと同じ式。W5-01）。"""
    rows = [
        ("hellocycling", "empty", 0, 9, OPEN),
        ("hellocycling", "some", 3, 6, OPEN),
        ("hellocycling", "full", 9, 0, OPEN),
    ]
    built = daily(observations(rows))
    assert cell(built, "empty", 40)["n_bike_ok"] == 0
    assert cell(built, "empty", 40)["n_dock_ok"] == 3
    assert cell(built, "some", 40)["n_bike_ok"] == 3
    assert cell(built, "full", 40)["n_dock_ok"] == 0


def test_a_suspended_port_is_counted_not_dropped() -> None:
    """**休止は落とさずに数える。** 分母の取り方は読む側が決める（W5 プラン §6.2）。"""
    built = daily(observations([("hellocycling", "closed", 5, 5, SUSPENDED)]))
    one = cell(built, "closed", 40)
    assert one["n"] == 3, "行は残る"
    assert one["n_suspended"] == 3
    assert one["n_bike_ok"] == 0, "休止していれば借りられない（labels.py の定義）"
    assert one["n_dock_ok"] == 0


def test_the_denominator_can_be_chosen_afterwards() -> None:
    """**`n − n_suspended` で「開いていたときの割合」が出る。**

    これがあるので、**表を 2 つ作らずに**両方の推定量を比べられる（PR D で決める）。
    """
    built = daily(observations([("hellocycling", "closed", 5, 5, SUSPENDED)]))
    one = cell(built, "closed", 40)
    assert one["n"] - one["n_suspended"] == 0, "開いていた点が無いので条件付きは引けない"


def test_bikes_are_summed_for_the_mean_and_the_spread() -> None:
    """`prof_mean_bikes` / `prof_std_bikes` の材料。**割らずに和で持つ。**"""
    one = cell(daily(observations([("hellocycling", "a", 4, 5, OPEN)])), "a", 12)
    assert one["sum_bikes"] == 12  # 4 × 3 点
    assert one["sum_bikes_sq"] == 48  # 16 × 3 点


def test_an_unobserved_port_is_not_counted() -> None:
    """**`-1` を 0 と読まない**（データ辞書 §10.1）。行そのものが出ない。"""
    rows = [("hellocycling", "a", 3, 4, OPEN), ("hellocycling", "gone", MISSING, MISSING, OPEN)]
    built = daily(observations(rows))
    assert set(built.column("station_id").to_pylist()) == {"a"}


def test_half_an_observation_is_not_counted() -> None:
    """**片方だけ `-1` でも落とす**（`exclude_at_base` と同じ判定）。

    実データでは台数と返却枠は必ず一緒に `-1` になる（同じポートの同じ行から来る）が、
    **規則は片方でも落とす**と決まっている。**実データに出ないからと緩めない**
    ——緩めた瞬間に、この 2 つの判定が別のものになる。
    """
    rows = [("hellocycling", "a", 3, 4, OPEN), ("hellocycling", "half", 3, MISSING, OPEN)]
    built = daily(observations(rows))
    assert set(built.column("station_id").to_pylist()) == {"a"}


def test_the_phantom_station_is_not_counted() -> None:
    """実在しないポート（ドコモ `5753`）。**名前ではなく ID で弾く。**"""
    rows = [("docomo-cycle", "5753", 3, 4, OPEN), ("docomo-cycle", "real", 3, 4, OPEN)]
    built = daily(observations(rows))
    assert set(built.column("station_id").to_pylist()) == {"real"}
    # HELLO の同じ ID は落とさない（弾くのは 1 系統ぶん）
    hello = daily(observations([("hellocycling", "5753", 3, 4, OPEN)]))
    assert hello.num_rows == profile.SLOTS_PER_DAY


def test_a_port_seen_only_in_the_morning_has_only_morning_slots() -> None:
    """**観測が無い枠には行を作らない**（`n = 0` の行を残さない）。

    as-of は 10 分（`MAX_STALENESS_S`）まで前を見るので、途切れた直後の 2 枠ぶんは残る。
    """
    table = observations([("hellocycling", "a", 3, 4, OPEN)], minutes=6 * 60)
    built = daily(table)
    slots = built.column("slot15").to_pylist()
    assert min(slots) == 0
    assert 24 <= max(slots) <= 25, f"6 時（枠 24）の少し後で終わる: {max(slots)}"


# ── 除外が `exclude.py` と同じこと ────────────────────────────
def test_usable_points_match_exclude_at_base_apart_from_suspension() -> None:
    """**契約 6：除外規則は 1 実装。**

    `usable_points` は `exclude_at_base` から**休止だけを外した**もののはずである。
    ここがずれると、プロファイルと学習サンプルが別の母集団を見る。
    """
    rows = [
        ("hellocycling", "a", 3, 4, OPEN),
        ("hellocycling", "closed", 5, 5, SUSPENDED),
        ("hellocycling", "gone", MISSING, MISSING, OPEN),
        # **片方だけ欠けた行も混ぜる。** 両方欠けたものだけだと、`docks` の判定を
        # 落としても検査が通ってしまう（実際に落として確かめた）
        ("hellocycling", "half", 3, MISSING, OPEN),
        ("docomo-cycle", "5753", 3, 4, OPEN),
    ]
    table = observations(rows)
    keys = profile.station_keys(table)
    grid = build_grid(DAY, lookback_hours=profile.LOOKBACK_HOURS, lookahead_hours=0)
    seen = asof.to_observations(table, keys)
    row = asof.as_of_feature(seen, grid.times_ms)[:, grid.day_slice()]
    observed = asof.gather(seen.observed_at_ms, row, 0)
    grid_ms = np.asarray(grid.day_times_ms(), dtype=np.int64)[None, :]
    bikes = asof.gather(seen.bikes, row, MISSING)
    docks = asof.gather(seen.docks, row, MISSING)
    flags = asof.gather(seen.flags, row, MISSING)

    mine = profile.usable_points(
        keys=keys,
        feature_row=row,
        observed_at_ms=observed,
        grid_ms=grid_ms,
        bikes=bikes,
        docks=docks,
    )
    theirs = exclude.exclude_at_base(
        feature_row=row,
        observed_at_ms=observed,
        grid_ms=grid_ms,
        bikes=bikes,
        docks=docks,
        flags=flags,
        phantom=exclude.phantom_mask(keys),
    ).keep
    closed = exclude.suspended(flags)
    assert np.array_equal(mine & ~closed, theirs)
    assert int((mine & closed).sum()) > 0, "休止の点が実際に在ること"


# ── 転がす ────────────────────────────────────────────────────
def test_rolling_equals_a_plain_sum() -> None:
    """**`profile(D-1) + daily(D)` は、素直に足したものと一致する。**"""
    days = [
        daily(observations([("hellocycling", "a", index, 9 - index, OPEN)])) for index in range(3)
    ]
    rolled = None
    for one in days:
        rolled = profile.roll(rolled, one, None)
    assert rolled is not None
    assert rolled.equals(profile.sum_dailies(days))


def test_rolling_drops_the_expired_day() -> None:
    """**28 日前を引く。** 引かないと窓が伸びて「ふだん」が古いままになる。"""
    first = daily(observations([("hellocycling", "a", 1, 8, OPEN)]))
    second = daily(observations([("hellocycling", "a", 5, 4, OPEN)]))
    window = profile.roll(profile.roll(None, first, None), second, first)
    assert window.equals(profile.sum_dailies([second]))


def test_a_cell_that_only_the_expired_day_had_disappears() -> None:
    """**0 の行を残さない。** 残すと表が単調に太り、「未観測」と区別できなくなる。"""
    old = daily(observations([("hellocycling", "gone", 3, 4, OPEN)]))
    new = daily(observations([("hellocycling", "a", 3, 4, OPEN)]))
    window = profile.roll(profile.roll(None, old, None), new, old)
    assert set(window.column("station_id").to_pylist()) == {"a"}


def test_the_day_count_says_how_much_is_behind_a_cell() -> None:
    """**28 日を待たない**（W5-03）。足りないことは `n_days` に出る。"""
    one = daily(observations([("hellocycling", "a", 3, 4, OPEN)]))
    first = profile.roll(None, one, None)
    second = profile.roll(first, one, None)
    assert set(first.column("n_days").to_pylist()) == {1}
    assert set(second.column("n_days").to_pylist()) == {2}
    assert profile.summarize(first)["usable_cells"] == 0
    assert profile.summarize(second)["usable_cells"] == second.num_rows


def test_a_new_dow_type_starts_at_zero_days() -> None:
    """**土曜は土曜の 2 日目まで効かない**（W5 プラン §7.2 の日付の算数）。"""
    weekday = daily(observations([("hellocycling", "a", 3, 4, OPEN)]))
    saturday = daily(observations([("hellocycling", "a", 3, 4, OPEN)], day=SATURDAY), day=SATURDAY)
    mixed = profile.roll(profile.roll(None, weekday, None), saturday, None)
    counted = profile.summarize(mixed)["by_dow_type"]
    assert isinstance(counted, dict)
    assert counted["weekday"]["days_max"] == 1
    assert counted["sat"]["days_max"] == 1
    assert counted["sat"]["usable"] == 0


def test_the_rows_are_sorted_by_the_key() -> None:
    """**鍵の昇順で書く。** 並びが圧縮に効き、読むときの結合も速い。"""
    rows = [("hellocycling", "b", 3, 4, OPEN), ("hellocycling", "a", 3, 4, OPEN)]
    built = profile.roll(None, daily(observations(rows)), None)
    keys = list(
        zip(
            built.column("system_id").to_pylist(),
            built.column("station_id").to_pylist(),
            built.column("dow_type").to_pylist(),
            built.column("slot15").to_pylist(),
            strict=True,
        )
    )
    assert keys == sorted(keys)


# ── 形 ────────────────────────────────────────────────────────
def test_the_schema_is_the_contract() -> None:
    built = daily(observations([("hellocycling", "a", 3, 4, OPEN)]))
    assert built.schema == profile.DAILY_SCHEMA
    assert profile.roll(None, built, None).schema == profile.PROFILE_SCHEMA
    # `profile` は `daily` に `n_days` を 1 つ足しただけ
    assert profile.PROFILE_SCHEMA.names == [
        *profile.KEY_COLUMNS,
        "n_days",
        *profile.SUM_COLUMNS,
    ]


def test_an_unexpected_schema_is_refused() -> None:
    """**足りない列を捏造しない**（W3 プラン §12 の 97 と同じ）。"""
    wrong = pa.table({"system_id": ["a"]})
    with pytest.raises(profile.SchemaMismatchError):
        profile.roll(None, wrong, None)


def test_an_empty_day_makes_an_empty_table() -> None:
    """観測が 1 つも無い日。**落ちずに空を返す。**"""
    built = daily(SNAPSHOT_SCHEMA.empty_table())
    assert built.num_rows == 0
    assert built.schema == profile.DAILY_SCHEMA
    assert profile.roll(None, built, None).num_rows == 0


# ── 読む規則 ──────────────────────────────────────────────────
def test_the_version_to_read_is_yesterdays() -> None:
    """**開発プラン §6.2 のリーク防止。** `t` の前日 23:59 までで作った値だけを使う。"""
    assert profile.source_day(date(2026, 9, 13)) == date(2026, 9, 12)


def test_the_path_carries_the_day_and_the_name() -> None:
    assert profile_path(DAY, profile.DAILY_NAME) == "profiles/date=2026-09-07/daily.parquet"
    assert profile_path(DAY, profile.PROFILE_NAME) == "profiles/date=2026-09-07/profile.parquet"


def test_the_window_is_twenty_eight_days() -> None:
    """開発プラン §6.3 の「過去 28 日（前日まで）」。**忘れるための数である。**"""
    assert profile.PROFILE_DAYS == 28


def test_the_floor_matches_the_climatology() -> None:
    """**B2 と同じ下限**（`baselines/climatology.py`）。別の数にすると答えが 2 つ出る。"""
    from bikechance_ml.baselines.climatology import MIN_CELL_DAYS, SLOTS_PER_DAY

    assert profile.MIN_CELL_DAYS == MIN_CELL_DAYS
    assert profile.SLOTS_PER_DAY == SLOTS_PER_DAY
