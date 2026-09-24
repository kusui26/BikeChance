"""ポートプロファイル（`features/profile.py`、W5 プラン §6.2）。

**主題は「B2 と同じ量を、抽出せずに数えていること」。**

固定するのは 5 つ。

  * **1 枠 1 日あたり 3 点**（288 格子点 ÷ 96 枠）——抽出後の 1.62 行ではない
  * **除外は `features/exclude.py` と同じ**（休止だけは落とさずに数える）
  * **転がしと素直な和が一致する**（`profile(D-1) + daily(D) - daily(D-28)`）
  * **読むのは前日の版**（開発プラン §6.2 のリーク防止）
  * **割らない**——率にしてしまうと足せなくなり、分母の取り方も固定されてしまう
"""

from datetime import date, datetime
from typing import Final

import numpy as np
import pyarrow as pa
import pytest

from bikechance_ml.baselines import climatology
from bikechance_ml.eval.dataset import Samples, dow_type_indices
from bikechance_ml.features import asof, exclude, profile
from bikechance_ml.features.calendar import DOW_TYPE_ORDER
from bikechance_ml.features.constants import GRID_POINTS_PER_DAY, HORIZONS_MIN, MISSING
from bikechance_ml.features.grid import JST, build_grid, profile_path
from bikechance_ml.features.schema import PROFILE_COLUMNS
from bikechance_ml.jobs.snapshot_table import SCHEMA as SNAPSHOT_SCHEMA
from tests import profile_fixture as pf

#: **観測の組み立ては `tests/profile_fixture.py` に置く**（PR D の検査と同じ入口を使う）。
DAY: Final[date] = pf.DAY
SATURDAY: Final[date] = pf.SATURDAY
OPEN, SUSPENDED = pf.OPEN, pf.SUSPENDED
CADENCE_S: Final[int] = pf.CADENCE_S
observations = pf.observations
daily = pf.daily


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
    """**28 日を待たない**（W5-03）。足りないことは `n_days` に出る。

    **使えるのは 3 日目から**（D-30。2 日ぶんの B2 は配らないほうが当たっていた）。
    下限は読む側のものを渡す——**プロファイルは切らない**。
    """
    one = daily(observations([("hellocycling", "a", 3, 4, OPEN)]))
    first = profile.roll(None, one, None)
    second = profile.roll(first, one, None)
    third = profile.roll(second, one, None)
    assert set(first.column("n_days").to_pylist()) == {1}
    assert set(second.column("n_days").to_pylist()) == {2}
    assert set(third.column("n_days").to_pylist()) == {3}
    floor = climatology.MIN_CELL_DAYS
    assert profile.summarize(first, min_days=floor)["usable_cells"] == 0
    assert profile.summarize(second, min_days=floor)["usable_cells"] == 0
    assert profile.summarize(third, min_days=floor)["usable_cells"] == third.num_rows


def test_a_new_dow_type_starts_at_zero_days() -> None:
    """**土曜は土曜の 3 日目まで効かない**（W5 プラン §7.2 の日付の算数、D-30）。"""
    weekday = daily(observations([("hellocycling", "a", 3, 4, OPEN)]))
    saturday = daily(observations([("hellocycling", "a", 3, 4, OPEN)], day=SATURDAY), day=SATURDAY)
    mixed = profile.roll(profile.roll(None, weekday, None), saturday, None)
    counted = profile.summarize(mixed, min_days=climatology.MIN_CELL_DAYS)["by_dow_type"]
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


def test_the_slots_match_the_climatology() -> None:
    """**B2 と同じ 15 分枠**（`baselines/climatology.py`）。別の数にすると別のセルを指す。

    下限（`MIN_CELL_DAYS`）はもう縛らない——**プロファイル側から消した**ので、
    持ち主は読む側の 1 か所だけである（D-30）。
    """
    assert profile.SLOTS_PER_DAY == climatology.SLOTS_PER_DAY


def test_the_profile_no_longer_holds_a_floor() -> None:
    """**同じ数を 2 か所に置かない**（§12 の 132・142）。

    プロファイルは切らないので、下限を持たない。持ち主は読む側（D-30）。
    """
    assert not hasattr(profile, "MIN_CELL_DAYS")


# ── 特徴量として引く（W5 プラン §6.10 の PR J1）───────────────────
#: 引く検査で使う台帳。**ポートの番号はこの並び**（`StationFacts.station_keys()` の代わり）。
LEDGER: Final[tuple[tuple[str, str], ...]] = (("hellocycling", "a"), ("hellocycling", "b"))
WEEKDAY: Final[int] = DOW_TYPE_ORDER.index("weekday")
SAT: Final[int] = DOW_TYPE_ORDER.index("sat")


def profile_table(rows: list[dict[str, object]]) -> pa.Table:
    """`profile.parquet` と同じ形の表。**書いていない和は 0**（鍵と `n_days`・`n` は必ず書く）。"""
    filled = [{name: row.get(name, 0) for name in profile.PROFILE_SCHEMA.names} for row in rows]
    return pa.Table.from_pylist(filled, schema=profile.PROFILE_SCHEMA)


def one_cell(**values: object) -> dict[str, object]:
    """`a` の平日・枠 33（08:15〜08:30）。**値は引数で上書きする。**"""
    base: dict[str, object] = {
        "system_id": "hellocycling",
        "station_id": "a",
        "dow_type": "weekday",
        "slot15": 33,
        "n_days": 2,
        "n": 6,
    }
    return {**base, **values}


def take_one(table: pa.Table, port: int, dow: int, slot: int) -> profile.Cells:
    edition = profile.Edition(day=date(2026, 9, 6), table=table)
    found = profile.lookup(edition, LEDGER)
    return found.take(np.array([port]), np.array([dow]), np.array([slot]))


def test_the_target_slot_is_the_arrivals_quarter_hour() -> None:
    """**目標時刻 `t + h` の 15 分枠。** 基準時刻の枠ではない。"""
    assert int(profile.target_slot(8 * 60, 15)) == 33  # 08:15 着 → 08:15〜08:30
    assert int(profile.target_slot(8 * 60 + 10, 5)) == 33  # 08:15 着（端は含む）
    assert int(profile.target_slot(8 * 60 + 10, 4)) == 32  # 08:14 着はひとつ前


def test_the_target_slot_wraps_after_midnight() -> None:
    """**日をまたいだら 0 に戻る**（曜日種別は呼ぶ側が翌日に切り替える）。"""
    assert int(profile.target_slot(23 * 60 + 50, 20)) == 0  # 00:10
    assert int(profile.target_slot(23 * 60 + 55, 180)) == 11  # 02:55
    assert int(profile.target_slot(0, 5)) == 0


def test_b2_and_prof_share_the_slot_rule() -> None:
    """**B2（`climatology.slot15`）と `prof_*` は同じ関数で枠を決める**（W5-01）。

    全格子点 × 全水平で突き合わせる。**2 か所に書いて片方を直すと、B2 と `prof_*` が
    別のセルを指す**——例外は出ず、値だけがずれる。
    """
    minutes = np.repeat(np.arange(0, 24 * 60, 5, dtype=np.int16), len(HORIZONS_MIN))
    horizons = np.tile(np.asarray(HORIZONS_MIN, dtype=np.int16), 24 * 60 // 5)
    rows = len(minutes)
    samples = Samples(
        systems=("hellocycling",),
        ports=("hellocycling/a",),
        system=np.zeros(rows, dtype=np.int8),
        port=np.zeros(rows, dtype=np.int32),
        day=np.zeros(rows, dtype=np.int32),
        h_min=horizons,
        minute_of_day=minutes,
        dow_type=np.zeros(rows, dtype=np.int8),
        weight=np.ones(rows, dtype=np.float32),
        labels={},
        counts={},
    )
    assert np.array_equal(climatology.slot15(samples), profile.target_slot(minutes, horizons))


def test_the_values_are_derived_from_the_sums() -> None:
    """**割るのは読むときだけ。** `prof_p_bike` は B2 の率そのもの（`n_bike_ok / n`）。"""
    table = profile_table(
        [
            one_cell(
                n_bike_ok=3,
                n_dock_ok=6,
                sum_bikes=12,
                sum_bikes_sq=30,
                sum_rentals_60=18,
                sum_returns_60=6,
            )
        ]
    )
    cells = take_one(table, 0, WEEKDAY, 33)
    assert cells.values["prof_p_bike"][0] == pytest.approx(0.5)
    assert cells.values["prof_p_dock"][0] == pytest.approx(1.0)
    assert cells.values["prof_mean_bikes"][0] == pytest.approx(2.0)
    # 分散は 30/6 − 2² ＝ 1
    assert cells.values["prof_std_bikes"][0] == pytest.approx(1.0)
    # **60 分の窓の和を点の数で割る**ので「1 時間あたり」（18 / 6 ＝ 3 台/時）
    assert cells.values["prof_rentals_per_hour"][0] == pytest.approx(3.0)
    assert cells.values["prof_returns_per_hour"][0] == pytest.approx(1.0)
    assert int(cells.n_days[0]) == 2


def test_the_suspended_points_stay_in_the_denominator() -> None:
    """**休止していた点も分母に入る**（B2 と同じ定義。`n_suspended` は読まない）。"""
    table = profile_table([one_cell(n_suspended=3, n_bike_ok=3)])
    cells = take_one(table, 0, WEEKDAY, 33)
    assert cells.values["prof_p_bike"][0] == pytest.approx(0.5)


def test_a_missing_cell_is_nan_and_zero_days() -> None:
    """**無いセルは NaN と 0**（値を 0 で埋めない。日数の 0 は「履歴が無い」）。"""
    table = profile_table([one_cell()])
    cells = take_one(table, 0, WEEKDAY, 34)
    for name in profile.VALUE_COLUMNS:
        assert np.isnan(cells.values[name][0]), name
    assert int(cells.n_days[0]) == 0


def test_the_dow_type_is_part_of_the_key() -> None:
    """**同じポート・同じ枠でも、曜日種別が違えば別のセル。** 平日の行が土曜の値を拾わない。"""
    table = profile_table([one_cell(n_days=2), one_cell(dow_type="sat", n_days=7, n=21)])
    assert int(take_one(table, 0, WEEKDAY, 33).n_days[0]) == 2
    assert int(take_one(table, 0, SAT, 33).n_days[0]) == 7
    assert int(take_one(table, 0, DOW_TYPE_ORDER.index("sun_holiday"), 33).n_days[0]) == 0


def test_the_dow_numbers_follow_dow_type_order() -> None:
    """**番号は `DOW_TYPE_ORDER` の位置**（B2 の `dow_type_indices` と同じ）。3 つとも確かめる。"""
    rows = [
        one_cell(dow_type=name, n_days=days, n=3 * days)
        for name, days in (("sat", 1), ("sun_holiday", 2), ("weekday", 3))
    ]
    table = profile_table(rows)
    names = np.array(DOW_TYPE_ORDER, dtype=np.str_)
    for name, index in zip(DOW_TYPE_ORDER, dow_type_indices(names), strict=True):
        expected = {"sat": 1, "sun_holiday": 2, "weekday": 3}[name]
        assert int(take_one(table, 0, int(index), 33).n_days[0]) == expected


def test_the_port_number_is_the_ledgers() -> None:
    """**ポートの番号は台帳の並び。** プロファイルの行の並びではない。"""
    table = profile_table([one_cell(station_id="b", n_days=4, n=12), one_cell(n_days=2)])
    assert int(take_one(table, 0, WEEKDAY, 33).n_days[0]) == 2
    assert int(take_one(table, 1, WEEKDAY, 33).n_days[0]) == 4


def test_a_port_the_ledger_does_not_know_is_dropped() -> None:
    """**台帳に無いポートは捨てる**（引かれることが無い）。**台帳の別のポートに化けない。**"""
    table = profile_table([one_cell(station_id="ghost", n_days=9, n=27)])
    edition = profile.Edition(day=date(2026, 9, 6), table=table)
    found = profile.lookup(edition, LEDGER)
    assert found.keys.size == 0
    assert int(take_one(table, 0, WEEKDAY, 33).n_days[0]) == 0


def test_the_station_id_alone_does_not_match_across_systems() -> None:
    """**鍵は `(system_id, station_id)` の組。** 系統を跨いで同じ番号が 2,608 件ある。"""
    table = profile_table([one_cell(system_id="docomo-cycle", station_id="a")])
    assert int(take_one(table, 0, WEEKDAY, 33).n_days[0]) == 0


def test_no_edition_gives_nothing_but_nan_and_zero() -> None:
    """**渡されなければ空**（J1 の推論はこちら）。

    版の日付も None で、記録に「読んでいない」と出る。
    """
    found = profile.lookup(None, LEDGER)
    assert found.day is None
    cells = found.take(np.array([0, 1]), np.array([WEEKDAY, SAT]), np.array([0, 95]))
    assert all(bool(np.all(np.isnan(values))) for values in cells.values.values())
    assert cells.n_days.tolist() == [0, 0]


def test_a_partial_profile_gives_the_same_values() -> None:
    """**要る枠だけを読んだ表でも、引ける値は同じ**（J2 は 1 周期 8 枠だけを読む。§6.10 ①）。

    全ポート × 3 × 96 の格子を確保する作りだと、部分の表を渡したときに**無い枠が
    「0 日」に化ける**ことがある。在るセルだけを持つので、それが起きないことを固定する。
    """
    rows = [
        one_cell(slot15=slot, n_days=1 + slot % 3, n=3 + 3 * (slot % 3), n_bike_ok=slot % 3)
        for slot in range(profile.SLOTS_PER_DAY)
    ]
    full = profile_table(rows)
    part = profile_table([row for row in rows if 30 <= int(str(row["slot15"])) < 38])
    for slot in range(30, 38):
        whole, piece = take_one(full, 0, WEEKDAY, slot), take_one(part, 0, WEEKDAY, slot)
        assert whole.n_days.tolist() == piece.n_days.tolist()
        for name in profile.VALUE_COLUMNS:
            assert whole.values[name].tolist() == piece.values[name].tolist(), (slot, name)


def test_the_standard_deviation_never_goes_negative() -> None:
    """**分散を 0 で止める。** 全点が同じ台数のセルは、丸めで負の小さな値が出る。"""
    derived = profile.derive(
        {
            "n": np.array([3.0]),
            "n_bike_ok": np.array([0.0]),
            "n_dock_ok": np.array([0.0]),
            "sum_bikes": np.array([1.0]),
            "sum_bikes_sq": np.array([1.0 / 3.0 - 1e-12]),
            "sum_rentals_60": np.array([0.0]),
            "sum_returns_60": np.array([0.0]),
        }
    )
    assert derived["prof_std_bikes"].tolist() == [0.0]


def test_the_derived_columns_are_the_schemas() -> None:
    """**作る列と契約の列が同じ**（`features/schema.py` の `PROFILE_COLUMNS`）。"""
    ones = {name: np.ones(1) for name in ("n", "n_bike_ok", "n_dock_ok", "sum_bikes")}
    zeros = {name: np.zeros(1) for name in ("sum_bikes_sq", "sum_rentals_60", "sum_returns_60")}
    derived = profile.derive({**ones, **zeros})
    assert tuple(derived) == profile.VALUE_COLUMNS
    assert (*profile.VALUE_COLUMNS, profile.DAYS_COLUMN) == PROFILE_COLUMNS


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ([one_cell(), one_cell()], "同じセルが 2 行"),
        ([one_cell(n=0)], "格子点が 0"),
        ([one_cell(dow_type="holiday")], "知らない曜日種別"),
    ],
)
def test_a_corrupt_profile_is_refused(rows: list[dict[str, object]], reason: str) -> None:
    """**黙って片方を使わない。** 正しく作った表では起きないので、起きたら止める。"""
    with pytest.raises(profile.CorruptProfileError):
        take_one(profile_table(rows), 0, WEEKDAY, 33)


def test_a_table_of_another_shape_is_refused() -> None:
    """**`daily` を渡し間違えたら止める**（`n_days` が無い）。"""
    table = pa.Table.from_pylist(
        [{name: one_cell().get(name, 0) for name in profile.DAILY_SCHEMA.names}],
        schema=profile.DAILY_SCHEMA,
    )
    with pytest.raises(profile.SchemaMismatchError):
        profile.lookup(profile.Edition(day=date(2026, 9, 6), table=table), LEDGER)
