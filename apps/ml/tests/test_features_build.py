"""1 日ぶんの組み立て（`features/build.py`、W3 プラン §5.8・§7.6）。

**この 1 ファイルの主題は 4 つ。**

  * **ゴールデン**：固定フィクスチャからの出力が `expected.csv` と一致する
  * **再現性**：2 回作って**バイト列まで同一**
  * **リーク**：`fetched_at > t` の観測が特徴量に入っていない（**素朴な再計算で検算する**）
  * **不偏性**：重みの総和が母集団に寄り、内訳の合計が落とした行数に一致する
"""

import csv
from collections import defaultdict
from datetime import datetime

import pyarrow as pa

from bikechance_ml.features import build
from bikechance_ml.features.constants import (
    FEATURE_SET,
    HORIZONS_MIN,
    MAX_STALENESS_S,
    STRATUM_TIGHT,
    STRATUM_UNIFORM,
    TIGHT_RATE,
    UNIFORM_RATE,
)
from bikechance_ml.features.schema import SCHEMA, MissingColumnError, feature_columns, to_table
from bikechance_ml.jobs.build_features import to_parquet_bytes
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


def test_weights_are_the_inverse_of_the_stratum_rate() -> None:
    for row in ROWS:
        expected = 1 / TIGHT_RATE if row["stratum"] == STRATUM_TIGHT else 1 / UNIFORM_RATE
        assert abs(float(str(row["weight"])) - expected) < 1e-6


def test_stratum_matches_the_base_state() -> None:
    """難所は `bikes <= 2` または `docks <= 2`。**水平によらない。**"""
    for row in ROWS:
        tight = int(str(row["bikes"])) <= 2 or int(str(row["docks"])) <= 2
        assert row["stratum"] == (STRATUM_TIGHT if tight else STRATUM_UNIFORM)


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
