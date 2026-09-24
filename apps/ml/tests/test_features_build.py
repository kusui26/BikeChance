"""1 日ぶんの組み立て（`features/build.py`、W3 プラン §5.8・§7.6）。

**この 1 ファイルの主題は 4 つ。**

  * **ゴールデン**：固定フィクスチャからの出力が `expected.csv` と一致する
  * **再現性**：2 回作って**バイト列まで同一**
  * **リーク**：`fetched_at > t` の観測が特徴量に入っていない（**素朴な再計算で検算する**）
  * **不偏性**：重みの総和が母集団に寄り、内訳の合計が落とした行数に一致する
"""

import csv
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import fields, replace
from datetime import UTC, date, datetime, timedelta
from typing import Final

import pyarrow as pa
import pytest

from bikechance_ml.features import build, coverage, profile, static
from bikechance_ml.features import neighbors as neighbors_module
from bikechance_ml.features import weather as weather_module
from bikechance_ml.features.constants import (
    FEATURE_SET,
    GRID_MINUTES,
    HORIZONS_MIN,
    MAX_STALENESS_S,
    SAMPLE_RATE,
    SAMPLE_WEIGHT,
    STRATUM_UNIFORM,
)
from bikechance_ml.features.grid import JST
from bikechance_ml.features.reference import StationAttributeRow, StationGeoRow, SystemReference
from bikechance_ml.features.sample import counter, station_seed, uniform
from bikechance_ml.features.schema import (
    PROFILE_COLUMNS,
    SCHEMA,
    MissingColumnError,
    feature_columns,
    to_table,
)
from bikechance_ml.features.weather import WeatherRow
from bikechance_ml.jobs.build_features import to_parquet_bytes
from bikechance_ml.jobs.snapshot_table import SCHEMA as SNAPSHOT_SCHEMA
from tests import features_fixture as fixture
from tests.gen_golden import to_csv

BUILT = build.build_day(fixture.build_inputs())
TABLE = BUILT.table
ROWS = TABLE.to_pylist()


# ── ゴールデン ────────────────────────────────────────────────
def test_output_matches_the_golden_file() -> None:
    """規則を変えたら `python -m tests.gen_golden` で作り直し、差分をレビューする。"""
    assert to_csv(TABLE) == fixture.EXPECTED.read_text(encoding="utf-8")


def test_golden_is_not_empty() -> None:
    """**空でも通るテストにしない。**"""
    assert TABLE.num_rows > 20
    assert len({row["station_id"] for row in ROWS}) >= 3


# ── 再現性 ────────────────────────────────────────────────────
def test_two_runs_produce_identical_bytes() -> None:
    """同じ日・同じ版で 2 回作れば、**行もバイト列も同じ**（W3 プラン §7.6）。"""
    again = build.build_day(fixture.build_inputs()).table
    assert again.equals(TABLE)
    assert to_parquet_bytes(again) == to_parquet_bytes(TABLE)


def test_rows_are_sorted_deterministically() -> None:
    keys = [(row["station_id"], row["t"], row["h_min"]) for row in ROWS]
    assert keys == sorted(keys)


# ── 契約 ──────────────────────────────────────────────────────
def test_schema_matches_the_contract() -> None:
    assert TABLE.schema == SCHEMA
    assert set(TABLE.column_names) == set(SCHEMA.names)


def test_to_table_refuses_missing_or_extra_columns() -> None:
    """**列を足したら必ずここで止まる。** 静かに落ちる列を作らない。"""
    columns = {name: TABLE.column(name).combine_chunks() for name in SCHEMA.names}
    del columns["bikes"]
    try:
        to_table(columns)
    except MissingColumnError as error:
        assert "bikes" in str(error)
    else:  # pragma: no cover - 失敗したときだけ通る
        raise AssertionError("列が足りなくても通ってしまった")


def test_non_feature_columns_are_not_offered_for_training() -> None:
    """キー・ラベル・重み・版は特徴量ではない（データ辞書 §10.4）。"""
    columns = feature_columns()
    assert "y_bike" not in columns
    assert "weight" not in columns
    assert "station_id" not in columns
    assert "bikes" in columns


def test_required_columns_have_no_nulls() -> None:
    for field in SCHEMA:
        if not field.nullable:
            assert TABLE.column(field.name).null_count == 0, field.name


def test_feature_set_is_recorded_on_every_row() -> None:
    assert set(TABLE.column("feature_set").to_pylist()) == {FEATURE_SET}
    assert BUILT.stats.feature_set == FEATURE_SET


def test_every_horizon_can_appear() -> None:
    assert set(TABLE.column("h_min").to_pylist()) <= set(HORIZONS_MIN)


# ── リーク：素朴な再計算で検算する ────────────────────────────
def observations_by_station() -> dict[tuple[str, str], list[dict[str, object]]]:
    """フィクスチャの表を、ポート毎の観測の並びに直す（**numpy を使わない**）。"""
    grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in fixture.load_snapshots().to_pylist():
        grouped[(str(row["system_id"]), str(row["station_id"]))].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda one: _stamp(one["observed_at"]))
    return grouped


def _stamp(value: object) -> datetime:
    assert isinstance(value, datetime)
    return value


def test_features_only_use_observations_already_fetched() -> None:
    """**本番の推論が受け取っていない観測で学習しない**（W3-13）。

    出力の `bikes` を、素朴な走査で求めた「`fetched_at <= t` のうち `observed_at` が
    最大の観測」と突き合わせる。パイプラインとは別の道筋で計算するので、
    as-of の実装を変えたときにここが効く。
    """
    grouped = observations_by_station()
    for row in ROWS:
        base = _stamp(row["t"])
        usable = [
            one
            for one in grouped[(str(row["system_id"]), str(row["station_id"]))]
            if _stamp(one["fetched_at"]) <= base
        ]
        assert usable, row
        newest = max(usable, key=lambda one: _stamp(one["observed_at"]))
        assert row["bikes"] == newest["bikes"], row
        assert row["docks"] == newest["docks"], row


def test_feed_delay_is_never_negative_and_within_the_limit() -> None:
    for row in ROWS:
        delay = int(str(row["feed_delay_s"]))
        assert 0 <= delay <= MAX_STALENESS_S


def test_labels_come_from_the_target_time() -> None:
    """ラベルは `observed_at <= t + h` のうち最新。**`fetched_at` では切らない。**"""
    grouped = observations_by_station()
    for row in ROWS:
        target = _stamp(row["t"]).timestamp() + int(str(row["h_min"])) * 60
        seen = [
            one
            for one in grouped[(str(row["system_id"]), str(row["station_id"]))]
            if _stamp(one["observed_at"]).timestamp() <= target
        ]
        newest = max(seen, key=lambda one: _stamp(one["observed_at"]))
        bikes, flags = int(str(newest["bikes"])), int(str(newest["flags"]))
        assert row["y_bike"] == int(bikes >= 1 and (flags & 2) == 2), row


# ── 除外と抽出 ────────────────────────────────────────────────
def test_phantom_station_never_appears() -> None:
    """実在しないポート（ドコモ `5753`）は 1 行も出ない。"""
    assert all(row["station_id"] != "5753" for row in ROWS)
    assert BUILT.stats.excluded["phantom"] == 288 * len(HORIZONS_MIN)


def test_exclusion_counts_add_up_to_the_population() -> None:
    """**内訳の合計が落とした行数と一致する**（重複して数えない）。"""
    stats = BUILT.stats
    assert stats.pairs_kept == stats.pairs_total - sum(stats.excluded.values())


def test_collision_guard_is_recorded_even_when_it_is_rare() -> None:
    """実測では 0 件だが、**費用ゼロなので外さない**（W3-12）。

    フィクスチャは観測の終端を含むので、ここでは実際に発火する。
    """
    assert "collision" in BUILT.stats.excluded


def test_every_row_has_the_same_weight() -> None:
    """一様に引くので**重みは定数**（2026-09-16、W5 プラン §6.9 の PR I）。"""
    for row in ROWS:
        assert abs(float(str(row["weight"])) - SAMPLE_WEIGHT) < 1e-6


def test_every_row_is_in_the_uniform_stratum() -> None:
    """**層は 1 つしか出ない。** 列は残してあるが、値は 1 種類である。

    2026-09-15 までのファイルには `"tight"` も入っている——**混ぜて読むときに
    行ごとの重みが効く**ので、列そのものは落とさない（`constants.SAMPLE_WEIGHT`）。
    """
    assert {str(row["stratum"]) for row in ROWS} == {STRATUM_UNIFORM}


def test_the_accepted_rows_are_exactly_those_under_the_rate() -> None:
    """**採った行を種から作り直して突き合わせる。**

    「抽出率どおりの割合が入っている」では、**台数で分岐する経路が戻ってきても
    気づけない**（割合は同じまま中身だけ変わる）。ここでは 1 行ずつ乱数を作り直し、
    `u < SAMPLE_RATE` が**そのまま採否になっている**ことを見る。
    """
    horizon_index = {horizon: index for index, horizon in enumerate(HORIZONS_MIN)}
    for row in ROWS:
        seed = station_seed(fixture.DAY, str(row["system_id"]), str(row["station_id"]))
        drawn = uniform(
            seed,
            counter(
                int(str(row["minute_of_day"])) // GRID_MINUTES,
                horizon_index[int(str(row["h_min"]))],
            ),
        )
        assert drawn < SAMPLE_RATE


def test_weight_sum_is_within_a_factor_of_the_population() -> None:
    """小さなフィクスチャなので誤差は大きいが、**桁は合う**。"""
    stats = BUILT.stats
    assert 0.5 < stats.weight_sum / stats.pairs_kept < 2.0


# ── システムごとの癖 ──────────────────────────────────────────
def rows_of(system_id: str) -> list[dict[str, object]]:
    return [row for row in ROWS if row["system_id"] == system_id]


def test_docomo_has_no_gap_and_no_staleness() -> None:
    """ドコモの `capacity` は動的値、`reported_age_s` は恒常的に 0（＝情報が無い）。"""
    for row in rows_of("docomo-cycle"):
        assert row["gap"] is None
        assert row["staleness_s"] is None


def test_hello_has_gap_and_staleness() -> None:
    for row in rows_of("hellocycling"):
        assert row["gap"] is not None
        assert row["staleness_s"] is not None


def test_administrative_codes_only_for_hello() -> None:
    """ドコモには住所が無いので `pref_code` / `muni_code` は NULL（W3 プラン §9.5）。"""
    assert all(row["pref_code"] is None for row in rows_of("docomo-cycle"))
    assert all(row["pref_code"] == 13 for row in rows_of("hellocycling"))


def test_region_id_only_for_docomo() -> None:
    assert all(row["region_id"] is not None for row in rows_of("docomo-cycle"))
    assert all(row["region_id"] is None for row in rows_of("hellocycling"))


def test_neighbours_cross_systems() -> None:
    """`p1` の 300 m 近傍は `p2` だけ、500 m には ドコモの `d1` も入る。"""
    p1 = next(row for row in ROWS if row["station_id"] == "p1")
    assert p1["nb_count_300m"] == 1
    assert p1["nb_count_300m_same"] == 1
    assert p1["nb_count_500m"] == 2
    assert p1["urban_density_500m"] == 2


def test_isolated_neighbour_aggregates_are_zero_not_null() -> None:
    """**近傍 0 は実データ。** 合計は 0、個数は 0（W3 プラン §9.5）。"""
    for row in ROWS:
        assert row["nb_count_300m"] is not None
        assert row["nb_bikes_sum_300m"] is not None


# ── 暦 ────────────────────────────────────────────────────────
def test_calendar_columns_describe_the_base_day() -> None:
    """2026-09-07 は月曜、祝日ではない。翌日も平日。"""
    for row in ROWS:
        assert row["dow"] == 0
        assert row["day_type"] == "weekday"
        assert row["dow_type"] == "weekday"
        assert row["is_holiday"] is False
        assert row["month"] == 9


def test_minute_of_day_is_jst() -> None:
    """基準時刻は **JST 00:00 起点**。UTC で数えると 9 時間ずれる。"""
    for row in ROWS:
        base = _stamp(row["t"])
        minute = int((base.timestamp() + 9 * 3600) % 86400 // 60)
        assert row["minute_of_day"] == minute


def test_target_day_type_can_cross_midnight() -> None:
    """`t + h` は翌日に入り得る（最長 180 分）。**目標時刻の属性を使う。**"""
    for row in ROWS:
        assert row["target_dow_type"] in {"weekday", "sat", "sun_holiday"}


# ── 空の入力 ──────────────────────────────────────────────────
def test_a_day_without_observations_produces_an_empty_table() -> None:
    """**例外にしない。** 収集が止まった日でもパイプラインは通る。"""
    inputs = fixture.build_inputs()
    empty = build.DayInputs(
        day=inputs.day,
        reference=inputs.reference,
        table=inputs.table.schema.empty_table(),
        weather=inputs.weather,
        profile=inputs.profile,
    )
    built = build.build_day(empty)
    assert built.table.num_rows == 0
    assert built.table.schema == SCHEMA
    assert built.stats.pairs_kept == 0


def test_csv_round_trip_keeps_the_column_order() -> None:
    """ゴールデンの読み方が壊れていないこと。"""
    lines = fixture.EXPECTED.read_text(encoding="utf-8").splitlines()
    assert next(iter(csv.reader(lines))) == list(SCHEMA.names)


def test_table_is_a_single_file_for_both_systems() -> None:
    """出力は **1 日 1 ファイル**。`system_id` は列（近傍がシステムを跨ぐため）。"""
    systems = set(TABLE.column("system_id").to_pylist())
    assert systems == {"hellocycling", "docomo-cycle"}
    assert isinstance(TABLE, pa.Table)


# ── 天気（W4 プラン §6.4）─────────────────────────────────────
def test_the_weather_columns_are_filled() -> None:
    """**完了条件**：天気アーカイブのある日は、4 列が NULL でない（§6.4）。"""
    for name in ("precip_mm_now", "precip_mm_target", "temp_c", "wind_kmh"):
        values = TABLE.column(name).to_pylist()
        assert all(one is not None for one in values), f"{name} に NULL がある"


#: ポートが居る気象格子（`weather.json` の `_comment`）。囮の格子は +100 してある。
_PORT_CELL: Final[tuple[int, int]] = (714, 2236)


def _port_issues() -> list[WeatherRow]:
    """ポートの格子ぶんの発行を、`available_at` の昇順で。"""
    rows = [
        one
        for one in fixture.load_weather_rows()
        if (one.cell_lat_idx, one.cell_lon_idx) == _PORT_CELL
    ]
    return sorted(rows, key=lambda one: one.available_at)


def _ceil_hour(at: datetime) -> datetime:
    top = at.replace(minute=0, second=0, microsecond=0)
    return top if top == at else top + timedelta(hours=1)


def _hours_between(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds()) // 3600


def test_the_weather_comes_from_the_issue_that_was_available() -> None:
    """**素朴な再計算で検算する**（リークの検査 `test_no_leak_*` と同じやり方）。

    「`available_at <= t` の最新の発行の、`t` を含む時間帯」を素朴に選び直して、
    出力の `precip_mm_now` と突き合わせる。`build.py` の添字の算術とは別経路である。
    """
    issues = _port_issues()
    for row in ROWS:
        at = row["t"].replace(tzinfo=UTC)
        usable = [one for one in issues if one.available_at <= at]
        assert usable, f"{at} に使える発行が無い（フィクスチャを疑う）"
        latest = usable[-1]
        lead = _hours_between(latest.issued_hour, _ceil_hour(at))
        expected = latest.values["precip_mm"][lead]
        assert row["precip_mm_now"] == pytest.approx(expected), f"{at} の行が合わない"


def _fraction_of(issue: WeatherRow) -> float:
    """その発行の値の小数部（＝発行時刻の目印）。"""
    first = issue.values["precip_mm"][0]
    assert first is not None, "フィクスチャの先頭が NULL（目印が取れない）"
    return round(first % 1, 1)


def test_no_row_uses_a_forecast_from_the_future() -> None:
    """**`available_at` より後の予報を 1 件も使っていない**（§6.4 の完了条件）。

    フィクスチャは `precip_mm[k] = 発行の時（JST）/ 10 + k` なので、**値の小数部が
    発行時刻を表す**（7 → .7、9 → .9、11 → .1、13 → .3）。出力の値だけから発行を
    復元して、それが `t` までに入手できていたことを確かめる。
    """
    by_fraction = {_fraction_of(one): one for one in _port_issues()}
    assert len(by_fraction) == len(_port_issues()), "小数部が重なっている（フィクスチャを疑う）"
    for row in ROWS:
        at = row["t"].replace(tzinfo=UTC)
        issue = by_fraction[round(float(row["precip_mm_now"]) % 1, 1)]
        assert issue.available_at <= at, f"{at} が未入手の {issue.available_at} を使っている"


def test_a_port_outside_the_archived_cells_is_counted() -> None:
    """**当たらないポートは数える**（黙って NULL にしない）。実在しない 5753 の格子は
    フィクスチャに入れていないので 1 件になる。"""
    assert BUILT.stats.stations_without_weather == 1
    assert BUILT.stats.weather_issues == 4


# ── 天気の被覆（W4 プラン §8.5.3、PR I）──────────────────────
def _only_the_last_issue() -> weather_module.Weather:
    """**最後の発行だけ**を残した予報。09-08（アーカイブが時の途中で始まった日）と同じ形。"""
    rows = fixture.load_weather_rows()
    latest = max(one.available_at for one in rows)
    return weather_module.to_weather([one for one in rows if one.available_at == latest])


def test_the_weather_coverage_is_measured_from_the_table() -> None:
    """**別経路で検算する。** 内訳の数ではなく、出力の表を数え直して突き合わせる。"""
    again = coverage.measure(TABLE)
    assert BUILT.stats.weather_coverage == again
    assert again.covered == TABLE.num_rows, "ゴールデンは 4 列とも埋まっている"


def test_a_day_without_any_forecast_has_no_coverage() -> None:
    """**09-07 と同じ形**（予報が 1 件も無い）。`feature_set` は `v3` のままである。"""
    without = build.build_day(replace(fixture.build_inputs(), weather=weather_module.empty()))
    assert without.stats.feature_set == FEATURE_SET
    assert without.stats.weather_issues == 0
    assert without.stats.weather_coverage.ratio == 0.0
    assert without.stats.weather_coverage.rows == TABLE.num_rows


def test_a_day_where_the_forecast_started_late_is_partly_covered() -> None:
    """**09-08 と同じ形**：早い基準時刻だけ天気が無い。**0% でも 100% でもない。**

    3 つの値（0%・途中・100%）が出ることで、**被覆が表について動いている**ことが分かる。
    どれか 1 つだけを見ていると、決め打ちの数と区別できない。
    """
    late = build.build_day(replace(fixture.build_inputs(), weather=_only_the_last_issue()))
    covered = late.stats.weather_coverage
    assert 0 < covered.covered < covered.rows
    assert covered == coverage.measure(late.table)


#: `as_dict` に出さないと決めた欄。**理由を書かせる**（うっかり落ちたのと区別するため）。
NOT_IN_THE_DICT: Final[Mapping[str, str]] = {}


def test_every_day_stat_reaches_the_dict() -> None:
    """**数えたものは全部出す**（W4 プラン §8.5.8 の教訓）。

    `NowStats` では 2 つが `inference_log` に届いていなかった。**欄を足して転送を
    忘れる道**は `DayStats` にも同じようにある（`build_features` の標準出力、のちに
    `job_runs`）ので、ここで数え上げる。
    """
    emitted = set(BUILT.stats.as_dict())
    counted = {one.name for one in fields(build.DayStats)}
    assert counted - emitted == set(NOT_IN_THE_DICT), "数えたのに出していない欄がある"
    assert emitted - counted == set(), "`DayStats` に無い鍵が出ている"


def test_the_target_weather_moves_with_the_horizon() -> None:
    """**水平で変わるのは `precip_mm_target` だけ。** 同じ基準時刻で値が動く。"""
    by_time: dict[datetime, set[float]] = defaultdict(set)
    for row in ROWS:
        by_time[row["t"]].add(float(row["precip_mm_target"]))
    assert any(len(values) > 1 for values in by_time.values()), "水平で動いていない"


# ── ポートプロファイル（W5 プラン §6.10 の PR J1）────────────────
def _naive_profile_values(row: dict[str, object]) -> tuple[dict[str, float], int] | None:
    """**素朴な再計算**：`profile.csv` を辞書で引き、Python の浮動小数で割る。

    組み立て（`profile.Lookup`）とは別の道で同じ値を出す。目標時刻は `t + h` を
    **JST に直してから**枠と曜日種別を決める（フィクスチャの日は月曜、翌日も平日）。
    """
    stamp = row["t"]
    assert isinstance(stamp, datetime)
    horizon = row["h_min"]
    assert isinstance(horizon, int)
    target = stamp.replace(tzinfo=UTC).astimezone(JST) + timedelta(minutes=horizon)
    slot = (target.hour * 60 + target.minute) // 15
    kind = "weekday" if target.weekday() < 5 else "sat"
    found = [
        one
        for one in fixture.load_profile().to_pylist()
        if (one["system_id"], one["station_id"], one["dow_type"], one["slot15"])
        == (row["system_id"], row["station_id"], kind, slot)
    ]
    if not found:
        return None
    cell = found[0]
    points = float(cell["n"])
    mean = cell["sum_bikes"] / points
    return (
        {
            "prof_p_bike": cell["n_bike_ok"] / points,
            "prof_p_dock": cell["n_dock_ok"] / points,
            "prof_mean_bikes": mean,
            "prof_std_bikes": max(cell["sum_bikes_sq"] / points - mean * mean, 0.0) ** 0.5,
            "prof_rentals_per_hour": cell["sum_rentals_60"] / points,
            "prof_returns_per_hour": cell["sum_returns_60"] / points,
        },
        int(cell["n_days"]),
    )


def test_the_profile_columns_match_a_naive_lookup() -> None:
    """**目標時刻のセルを、前日の版から引いている**（素朴な再計算で検算する）。"""
    checked = found = 0
    for row in ROWS:
        expected = _naive_profile_values(row)
        checked += 1
        if expected is None:
            assert row["prof_n_days"] == 0, row
            assert all(row[name] is None for name in profile.VALUE_COLUMNS), row
            continue
        values, days = expected
        found += 1
        assert row["prof_n_days"] == days, row
        for name, value in values.items():
            assert row[name] == pytest.approx(value, rel=1e-6), (name, row)
    # **空回りしていない**：引けた行も、引けなかった行も在る
    assert found > 0, "引けた行が 1 つも無い（仕込みを疑う）"
    assert found < checked, "引けなかった行が 1 つも無い（穴が効いていない）"


def test_the_weekday_rows_never_read_saturday_cells() -> None:
    """**曜日種別も鍵のうち。** `p1` には土曜のセル（日数 8）も在るが、月曜の行は拾わない。"""
    p1 = [row for row in ROWS if row["station_id"] == "p1" and row["prof_n_days"] != 0]
    assert p1, "p1 の行が無い（仕込みを疑う）"
    assert {row["prof_n_days"] for row in p1} == {5}


def test_a_day_without_a_profile_has_null_profile_columns() -> None:
    """**前日の版が無い日でも作れる**（J1 の完了条件 3）。値は NULL、日数は 0。

    2026-09-07 の本番がこれに当たる（`profile(09-06)` が無い）。**推論は J2 まで
    こちらを通る。** 0 で埋めると「日数 0 なのに率が 0」という嘘の行ができる。
    """
    built = build.build_day(fixture.build_inputs(with_profile=False))
    assert built.table.num_rows == TABLE.num_rows
    for name in profile.VALUE_COLUMNS:
        assert built.table.column(name).null_count == built.table.num_rows, name
    assert set(built.table.column(profile.DAYS_COLUMN).to_pylist()) == {0}
    assert built.stats.profile_date is None
    assert built.stats.profile_coverage.covered == 0


def test_the_profile_does_not_touch_any_other_column() -> None:
    """**プロファイルを渡しても、他の 69 列は 1 つも動かない**（行も抽出も同じ）。"""
    without = build.build_day(fixture.build_inputs(with_profile=False)).table
    others = [name for name in SCHEMA.names if name not in PROFILE_COLUMNS]
    assert TABLE.select(others).equals(without.select(others))


def test_the_profile_date_and_coverage_are_recorded() -> None:
    """**どの版を読み、何割の行に入ったか**を内訳に出す（版は「列が在る」しか語らない）。"""
    stats = BUILT.stats
    assert stats.profile_date == "2026-09-06"
    assert stats.profile_coverage.rows == TABLE.num_rows
    covered = sum(1 for row in ROWS if row["prof_n_days"] > 0)
    assert stats.profile_coverage.covered == covered
    assert 0 < stats.profile_coverage.ratio < 1, "穴の行と引けた行の両方が在るはず"
    # **日数と値は同じ行で揃う**（揃っていなければ引き方が壊れている）
    assert stats.profile_coverage.is_uniform
    recorded = stats.as_dict()
    assert recorded["profile_date"] == "2026-09-06"
    assert recorded["profile_coverage"] == stats.profile_coverage.as_dict()


# ── 日をまたぐ行は翌日の曜日種別で引く ──────────────────────────
#: 金曜（翌日は土曜）。**目標時刻が 0 時を越えた行だけ土曜のセルを引く。**
_FRIDAY: Final[date] = date(2026, 9, 11)
_NIGHT_AT: Final[datetime] = datetime(2026, 9, 11, 23, 0, tzinfo=JST).astimezone(UTC)


def _night_inputs() -> build.NowInputs:
    """金曜 23:00 JST に 1 ポートだけ動いている入力。**プロファイルは平日と土曜で日数を変える。**"""
    start = datetime(2026, 9, 11, 19, 0, tzinfo=JST).astimezone(UTC)
    rows = [
        {
            "system_id": "hellocycling",
            "station_id": "night",
            "observed_at": start + timedelta(minutes=5 * step),
            "fetched_at": start + timedelta(minutes=5 * step, seconds=40),
            "bikes": 3 + step % 4,
            "docks": 6,
            "flags": 7,
            "reported_age_s": 30,
        }
        for step in range(4 * 12)
    ]
    systems = (
        SystemReference(
            system_id="hellocycling",
            geo=(
                StationGeoRow(
                    station_id="night",
                    first_seen_at=datetime(2026, 8, 1, tzinfo=UTC),
                    pref_code=13,
                    muni_code=13101,
                ),
            ),
            attributes=(
                StationAttributeRow(
                    station_id="night",
                    lat=35.68,
                    lon=139.76,
                    capacity=12,
                    is_charging_station=False,
                    region_id=None,
                ),
            ),
            neighbors=(),
        ),
    )
    facts = static.to_facts(systems, {})
    cells = [
        {
            "system_id": "hellocycling",
            "station_id": "night",
            "dow_type": kind,
            "slot15": slot,
            "n_days": days,
            "n": 3 * days,
            "n_bike_ok": 3 * days,
            "n_dock_ok": 3 * days,
        }
        for kind, days in (("weekday", 7), ("sat", 2))
        for slot in range(profile.SLOTS_PER_DAY)
    ]
    table = pa.Table.from_pylist(
        [{name: one.get(name, 0) for name in profile.PROFILE_SCHEMA.names} for one in cells],
        schema=profile.PROFILE_SCHEMA,
    )
    return build.NowInputs(
        at=_NIGHT_AT,
        system_id="hellocycling",
        reference=build.Reference(
            facts=facts,
            links=neighbors_module.to_links(systems, facts.station_keys()),
            holidays=frozenset(),
        ),
        table=pa.Table.from_pylist(rows, schema=SNAPSHOT_SCHEMA),
        weather=weather_module.empty(),
        profile=profile.Edition(day=_FRIDAY - timedelta(days=1), table=table),
    )


def test_a_row_that_crosses_midnight_reads_the_next_days_cells() -> None:
    """**目標時刻が 0 時を越えたら、翌日（土曜）のセルを引く**（`target_dow_type` と同じ規則）。

    金曜 23:00 から 60 分以上先は土曜に入る。平日のセルは 7 日、土曜のセルは 2 日にしてあるので、
    **日数で「どちらを引いたか」が読める。**
    """
    table = build.build_now(_night_inputs()).table
    days = dict(
        zip(table.column("h_min").to_pylist(), table.column("prof_n_days").to_pylist(), strict=True)
    )
    kinds = dict(
        zip(
            table.column("h_min").to_pylist(),
            table.column("target_dow_type").to_pylist(),
            strict=True,
        )
    )
    assert sorted(days) == list(HORIZONS_MIN), "10 水平すべての行が在るはず"
    for horizon in HORIZONS_MIN:
        crosses = horizon >= 60
        assert kinds[horizon] == ("sat" if crosses else "weekday"), horizon
        assert days[horizon] == (2 if crosses else 7), horizon
