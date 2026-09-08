"""配列 → 長形式の変換（`bikechance_ml/jobs/snapshot_table.py`、W2 プラン §9.2）。

ここで固定したい契約は 5 つ。
  * 半開区間と UTC でパスが決まる（同じ時間帯は必ず同じパスに写像する）
  * 台帳の `idx` が密でなければ**止まる**（値が別のポートに付くくらいなら落ちる）
  * **配列長より外側の `idx` は行にしない**（未登録と「観測されなかった」を混ぜない）
  * `-1` はそのまま残る（0 と区別する）
  * **`fetched_at` が全行に入る**（学習の as-of はこの列で切る。W3 プラン §9.1）
"""

import io
from datetime import UTC, datetime, timedelta, timezone

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from bikechance_ml.jobs.snapshot_table import (
    MISSING,
    SCHEMA,
    InconsistentLedgerError,
    InconsistentSnapshotError,
    SchemaMismatchError,
    Snapshot,
    StationRow,
    count_missing,
    has_current_schema,
    hour_window,
    parquet_path,
    read_table,
    station_ids_by_idx,
    to_parquet_bytes,
    to_table,
)

JST = timezone(timedelta(hours=9))
SYSTEM = "hellocycling"


def pa_buffer(body: bytes) -> io.BytesIO:
    """バイト列を pyarrow が読める入力にする。"""
    return io.BytesIO(body)


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 9, 8, hour, minute, second, tzinfo=UTC)


def fetched(hour: int, minute: int, delay_s: int = 70) -> datetime:
    """観測から `delay_s` 秒あとの取り込み時刻。60 秒を超えるので `timedelta` で足す。"""
    return at(hour, minute) + timedelta(seconds=delay_s)


def snapshot(hour: int, minute: int, bikes: list[int], delay_s: int = 70) -> Snapshot:
    """4 本の配列を同じ長さで作る。値の意味は問わないテスト用。

    `fetched_at` は既定で観測の 70 秒あと。HELLO の公開遅延の実測中央値が 67 秒で、
    **`observed_at` と別の値であることがテストで見えるように**わざとずらしてある。
    """
    size = len(bikes)
    return Snapshot(
        observed_at=at(hour, minute),
        fetched_at=fetched(hour, minute, delay_s),
        bikes=bikes,
        docks=[10 - value for value in bikes],
        flags=[7] * size,
        reported_age_s=[30] * size,
    )


# ── 時間帯とパス ──────────────────────────────────────────────
def test_hour_window_is_the_previous_hour() -> None:
    start, end = hour_window(at(5, 7))
    assert (start, end) == (at(4), at(5))


def test_hour_window_at_the_exact_hour_takes_the_previous_one() -> None:
    """半開区間なので、05:00 ちょうどに動いても畳むのは 04:00〜05:00。"""
    assert hour_window(at(5)) == (at(4), at(5))


def test_hour_window_converts_to_utc() -> None:
    """JST の 14:30 は UTC の 05:30。畳むのは UTC の 04 時台。"""
    jst = datetime(2026, 9, 8, 14, 30, tzinfo=JST)
    assert hour_window(jst) == (at(4), at(5))


def test_parquet_path_is_utc_and_hive_style() -> None:
    assert parquet_path(SYSTEM, at(4)) == "hellocycling/date=2026-09-08/hour=04/part.parquet"


def test_parquet_path_converts_from_jst() -> None:
    """JST の 09/08 09:00 は UTC の 09/08 00:00。日付が変わる境界を固定する。"""
    jst = datetime(2026, 9, 8, 9, 0, tzinfo=JST)
    assert parquet_path(SYSTEM, jst) == "hellocycling/date=2026-09-08/hour=00/part.parquet"


# ── 台帳 ─────────────────────────────────────────────────────
def test_station_ids_are_ordered_by_idx() -> None:
    rows = [StationRow("c", 2), StationRow("a", 0), StationRow("b", 1)]
    assert station_ids_by_idx(rows) == ("a", "b", "c")


def test_station_ids_reject_a_gap() -> None:
    with pytest.raises(InconsistentLedgerError):
        station_ids_by_idx([StationRow("a", 0), StationRow("c", 2)])


def test_station_ids_reject_a_one_based_ledger() -> None:
    with pytest.raises(InconsistentLedgerError):
        station_ids_by_idx([StationRow("a", 1), StationRow("b", 2)])


def test_station_ids_of_an_empty_ledger() -> None:
    assert station_ids_by_idx([]) == ()


# ── 変換 ─────────────────────────────────────────────────────
def test_table_has_one_row_per_station_and_snapshot() -> None:
    table = to_table(SYSTEM, ["a", "b"], [snapshot(4, 0, [1, 2]), snapshot(4, 5, [3, 4])])
    assert table.num_rows == 4
    assert table.schema == SCHEMA


def test_table_skips_stations_beyond_the_array_length() -> None:
    """当時まだ台帳に無かったポートは行にしない。**未登録と欠損を混ぜない。**"""
    table = to_table(SYSTEM, ["a", "b", "c"], [snapshot(4, 0, [1, 2])])
    assert table.column("station_id").to_pylist() == ["a", "b"]


def test_table_keeps_missing_as_minus_one() -> None:
    table = to_table(SYSTEM, ["a", "b"], [snapshot(4, 0, [MISSING, 0])])
    assert table.column("bikes").to_pylist() == [MISSING, 0]
    assert count_missing(table) == 1


def test_table_is_sorted_by_station_then_time() -> None:
    table = to_table(SYSTEM, ["b", "a"], [snapshot(4, 30, [1, 2]), snapshot(4, 0, [3, 4])])
    assert table.column("station_id").to_pylist() == ["a", "a", "b", "b"]
    assert table.column("observed_at").to_pylist() == [at(4, 0), at(4, 30), at(4, 0), at(4, 30)]


def test_table_carries_the_system_id() -> None:
    table = to_table(SYSTEM, ["a"], [snapshot(4, 0, [1])])
    assert table.column("system_id").to_pylist() == [SYSTEM]


# ── fetched_at（W3 プラン §9.1・段 2） ─────────────────────────
def test_schema_has_fetched_at_next_to_observed_at() -> None:
    """列の順序も契約に含める。人が `parquet-tools` で覗いたときに並びで意味が分かる。"""
    assert SCHEMA.names == [
        "system_id",
        "station_id",
        "observed_at",
        "fetched_at",
        "bikes",
        "docks",
        "flags",
        "reported_age_s",
    ]
    assert SCHEMA.field("fetched_at").type == SCHEMA.field("observed_at").type
    assert not SCHEMA.field("fetched_at").nullable


def test_fetched_at_is_written_for_every_row() -> None:
    """スカラなので、そのスナップショットの全ポートに同じ値が入る。"""
    table = to_table(SYSTEM, ["a", "b"], [snapshot(4, 0, [1, 2])])
    assert table.column("fetched_at").to_pylist() == [fetched(4, 0)] * 2
    assert table.column("fetched_at").null_count == 0


def test_fetched_at_differs_from_observed_at() -> None:
    """**この 2 つを取り違えると train/serve skew になる**（開発プラン §6.2）。"""
    table = to_table(SYSTEM, ["a"], [snapshot(4, 0, [1], delay_s=226)])
    observed = table.column("observed_at").to_pylist()[0]
    fetched = table.column("fetched_at").to_pylist()[0]
    assert fetched > observed
    assert (fetched - observed).total_seconds() == 226


def test_fetched_at_follows_each_snapshot() -> None:
    """スナップショットごとに違う値になる（1 つの表に複数の取り込み時刻が混ざる）。"""
    table = to_table(
        SYSTEM, ["a"], [snapshot(4, 0, [1], delay_s=10), snapshot(4, 5, [2], delay_s=200)]
    )
    assert table.column("fetched_at").to_pylist() == [fetched(4, 0, 10), fetched(4, 5, 200)]


def test_fetched_at_survives_a_round_trip_through_parquet() -> None:
    """**書いて読んで消えないこと**を固定する。列を足しただけで満足しない。"""
    table = to_table(SYSTEM, ["a", "b"], [snapshot(4, 0, [1, 2])])
    read = pq.read_table(pa_buffer(to_parquet_bytes(table)))
    assert "fetched_at" in read.schema.names
    assert read.schema == SCHEMA
    assert read.column("fetched_at").null_count == 0
    assert read.column("fetched_at").to_pylist() == [fetched(4, 0)] * 2


def test_table_of_no_snapshots_is_empty_but_typed() -> None:
    table = to_table(SYSTEM, ["a"], [])
    assert table.num_rows == 0
    assert table.schema == SCHEMA


def test_table_rejects_ragged_arrays() -> None:
    ragged = Snapshot(at(4), fetched(4, 0), [1, 2], [1], [7, 7], [0, 0])
    with pytest.raises(InconsistentSnapshotError):
        to_table(SYSTEM, ["a", "b"], [ragged])


def test_table_rejects_arrays_longer_than_the_ledger() -> None:
    """台帳から行は消えない。配列のほうが長いなら台帳の読み違い。"""
    with pytest.raises(InconsistentSnapshotError):
        to_table(SYSTEM, ["a"], [snapshot(4, 0, [1, 2])])


# ── Parquet ──────────────────────────────────────────────────
def test_parquet_round_trip_keeps_rows_and_types() -> None:
    table = to_table(SYSTEM, ["a", "b"], [snapshot(4, 0, [1, MISSING])])
    restored = pq.read_table(pa_buffer(to_parquet_bytes(table)))
    assert restored.schema == SCHEMA
    assert restored.to_pylist() == table.to_pylist()


def test_parquet_of_the_same_input_has_the_same_rows() -> None:
    """同じ時間帯を 2 回処理したら同じ内容になる（PR D の完了条件）。"""
    snapshots = [snapshot(4, 0, [1, 2]), snapshot(4, 5, [3, MISSING])]
    first = pq.read_table(pa_buffer(to_parquet_bytes(to_table(SYSTEM, ["a", "b"], snapshots))))
    second = pq.read_table(pa_buffer(to_parquet_bytes(to_table(SYSTEM, ["a", "b"], snapshots))))
    assert first.to_pylist() == second.to_pylist()


# ── 読む側の契約（W3 プラン §12 の 96）────────────────────────
def test_read_table_rejects_a_file_with_missing_columns() -> None:
    """**明示スキーマは「無い列を作る」ためのものではない。**

    `pq.read_table(..., schema=SCHEMA)` は足りない列を黙って null で埋める。
    `fetched_at` の無い古い形のファイルを読むと、as-of が全滅しているのに例外も
    出ない。だから**ファイル自身の列**を先に確かめる。
    """
    old = pa.table(
        {
            name: pa.array([], type=SCHEMA.field(name).type)
            for name in SCHEMA.names
            if name != "fetched_at"
        }
    )
    sink = pa.BufferOutputStream()
    pq.write_table(old, sink)
    body = bytes(sink.getvalue())
    assert not has_current_schema(body)
    with pytest.raises(SchemaMismatchError, match="契約と違う"):
        read_table(body, "テスト")


def test_read_table_accepts_the_current_schema() -> None:
    body = to_parquet_bytes(SCHEMA.empty_table())
    assert has_current_schema(body)
    assert read_table(body, "テスト").schema == SCHEMA
