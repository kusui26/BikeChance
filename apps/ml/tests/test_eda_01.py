"""EDA #1 の実行と出力（`analysis/eda_01.py`、`analysis/report.py`）。

読み込みは差し替え可能な口（`ParquetSource`）にしてあるので、Storage 無しで
「無い時間帯を欠落として扱う」「キャッシュを使う」まで確かめられる。
"""

import io as _io
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from bikechance_ml.analysis.eda_01 import (
    Loaded,
    SystemReport,
    analyse,
    hours_between,
    load_hours,
    parse_hour,
)
from bikechance_ml.analysis.report import findings, render_markdown
from bikechance_ml.jobs.snapshot_table import SCHEMA

HOUR = datetime(2026, 9, 7, 21, tzinfo=UTC)


def table_of(rows: Sequence[tuple[str, datetime, int, int]]) -> pa.Table:
    """`(station_id, observed_at, bikes, docks)` から SCHEMA の表を作る。

    **列を足したときに直す場所を 1 つにする。** この関数を書く前は同じ組み立てが
    3 か所にあり、`fetched_at` を足したとき（W3 の段 2）3 か所とも直すことになった。

    EDA は `fetched_at` を読まないが SCHEMA には要るので、観測の 70 秒あとで埋める
    （HELLO の公開遅延の実測中央値）。`flags` は 7（貸出・返却とも可）、
    `reported_age_s` は 0 で固定する。
    """
    return pa.table(
        {
            "system_id": pa.array(["t"] * len(rows), type=pa.string()),
            "station_id": pa.array([r[0] for r in rows], type=pa.string()),
            "observed_at": pa.array([r[1] for r in rows], type=pa.timestamp("ms", tz="UTC")),
            "fetched_at": pa.array(
                [r[1] + timedelta(seconds=70) for r in rows], type=pa.timestamp("ms", tz="UTC")
            ),
            "bikes": pa.array([r[2] for r in rows], type=pa.int16()),
            "docks": pa.array([r[3] for r in rows], type=pa.int16()),
            "flags": pa.array([7] * len(rows), type=pa.int16()),
            "reported_age_s": pa.array([0] * len(rows), type=pa.int16()),
        },
        schema=SCHEMA,
    )


def sample_table(station_ids: tuple[str, ...] = ("a", "b")) -> pa.Table:
    return table_of(
        [
            (station, HOUR + timedelta(minutes=minute), bikes, 5 - bikes)
            for station in station_ids
            for minute, bikes in ((0, 0), (5, 3), (10, 3))
        ]
    )


def to_bytes(table: pa.Table) -> bytes:
    sink = _io.BytesIO()
    pq.write_table(table, sink)
    return sink.getvalue()


class FakeSource:
    """指定した時間帯だけを返す口。それ以外は「無い」。"""

    def __init__(self, available: dict[str, bytes]) -> None:
        self.available = available
        self.asked: list[str] = []

    def download(self, bucket: str, path: str) -> bytes | None:
        self.asked.append(path)
        return self.available.get(path)


# ── 時間帯の並べ方 ───────────────────────────────────────────
def test_hours_between_is_half_open() -> None:
    hours = hours_between(HOUR, HOUR + timedelta(hours=3))
    assert hours == (HOUR, HOUR + timedelta(hours=1), HOUR + timedelta(hours=2))


def test_hours_between_of_an_empty_range() -> None:
    assert hours_between(HOUR, HOUR) == ()
    assert hours_between(HOUR + timedelta(hours=1), HOUR) == ()


def test_parse_hour_requires_a_timezone_and_the_hour_mark() -> None:
    assert parse_hour("2026-09-07T21:00:00Z") == HOUR
    with pytest.raises(ValueError, match="タイムゾーン"):
        parse_hour("2026-09-07T21:00:00")
    with pytest.raises(ValueError, match="正時"):
        parse_hour("2026-09-07T21:30:00Z")


def test_parse_hour_converts_to_utc() -> None:
    # JST 06:00 は UTC 前日 21:00
    assert parse_hour("2026-09-08T06:00:00+09:00") == HOUR


# ── 読み込み ─────────────────────────────────────────────────
def test_load_hours_records_missing_hours() -> None:
    path = "sys/date=2026-09-07/hour=21/part.parquet"
    source = FakeSource({path: to_bytes(sample_table())})
    loaded = load_hours(source, "sys", hours_between(HOUR, HOUR + timedelta(hours=2)), None)
    assert loaded.found_hours == (HOUR,)
    assert loaded.missing_hours == (HOUR + timedelta(hours=1),)
    assert loaded.table.num_rows == 6


def test_load_hours_of_nothing_returns_an_empty_typed_table() -> None:
    loaded = load_hours(FakeSource({}), "sys", (HOUR,), None)
    assert loaded.table.num_rows == 0
    assert loaded.table.schema == SCHEMA
    assert loaded.missing_hours == (HOUR,)


def test_load_hours_uses_the_cache_on_the_second_run(tmp_path: Path) -> None:
    path = "sys/date=2026-09-07/hour=21/part.parquet"
    source = FakeSource({path: to_bytes(sample_table())})
    load_hours(source, "sys", (HOUR,), tmp_path)
    load_hours(source, "sys", (HOUR,), tmp_path)
    assert len(source.asked) == 1  # 2 回目は取りに行かない
    assert (tmp_path / path).exists()


def test_load_hours_does_not_cache_missing_hours(tmp_path: Path) -> None:
    source = FakeSource({})
    load_hours(source, "sys", (HOUR,), tmp_path)
    load_hours(source, "sys", (HOUR,), tmp_path)
    assert len(source.asked) == 2  # 無いものは毎回確かめる


# ── 集計と出力 ───────────────────────────────────────────────
def report_of(table: pa.Table, missing: tuple[datetime, ...] = ()) -> SystemReport:
    return analyse(Loaded("t", table, (HOUR,), missing), n_requested=1 + len(missing))


def test_analyse_fills_every_section() -> None:
    report = report_of(sample_table())
    assert report.availability.n_rows == 6
    assert report.missingness.n_stations == 2
    assert report.hourly[0].hour_jst == 6  # UTC 21 時 ＝ JST 06 時
    assert report.spread.n_stations == 2
    assert report.flow.n_snapshots_native == 3
    assert report.rebalancing.n_events == 0  # +3 は閾値 4 に届かない


def test_analyse_of_an_empty_table_does_not_crash() -> None:
    report = report_of(SCHEMA.empty_table(), missing=(HOUR,))
    assert report.availability.n_rows == 0
    assert report.first_observed is None
    assert report.hourly == ()


def test_findings_reports_missing_hours() -> None:
    notes = findings([report_of(sample_table(), missing=(HOUR + timedelta(hours=1),))])
    assert any("Parquet が 1 時間ぶん無い" in note for note in notes)


def test_findings_reports_stations_that_never_move() -> None:
    still = table_of([("a", HOUR, 2, 2), ("a", HOUR + timedelta(minutes=5), 2, 2)])
    notes = findings([report_of(still)])
    assert any("一度も動かないポート" in note for note in notes)


def test_render_markdown_has_every_section() -> None:
    text = render_markdown([report_of(sample_table())], HOUR, HOUR + timedelta(hours=1))
    for heading in (
        "# EDA #1",
        "## 0. 気づいたこと",
        "## 1. 対象と欠損",
        "## 2. 在庫の分布",
        "## 3. 日内変動（JST）",
        "## 4. ポート差",
        "## 5. 5 分グリッドで失う流量",
        "## 6. 再配置とみられる変化",
    ):
        assert heading in text
    assert text.endswith("\n")


def test_render_markdown_is_pure() -> None:
    """同じ入力からは同じ本文が出る（時計を読んでいない）。"""
    args = ([report_of(sample_table())], HOUR, HOUR + timedelta(hours=1))
    assert render_markdown(*args) == render_markdown(*args)


def test_render_markdown_shows_jst_hours() -> None:
    text = render_markdown([report_of(sample_table())], HOUR, HOUR + timedelta(hours=1))
    assert "| 06 時 |" in text  # UTC 21 時 ＝ JST 06 時


def test_findings_reports_the_flow_loss() -> None:
    """5 分グリッドで失うものが無いシステムでも、その旨を書き出す。"""
    notes = findings([report_of(sample_table())])
    assert any("5 分グリッドで失うものが無い" in note for note in notes)


def test_findings_reports_the_diurnal_gap() -> None:
    """水準と変化を分けて言っていること。"""
    table = table_of(
        [
            ("a", HOUR + timedelta(minutes=minute), bikes, 5 - bikes)
            for minute, bikes in ((0, 5), (30, 5), (60, 0), (90, 5))  # 01 時台だけ動く
        ]
    )
    notes = findings([report_of(table)])
    assert any("水準はほぼ動かないのに" in note for note in notes)


def test_findings_reports_the_rebalancing_rate() -> None:
    notes = findings([report_of(sample_table())])
    assert any("再配置とみられる変化は 1 時間あたり" in note for note in notes)
