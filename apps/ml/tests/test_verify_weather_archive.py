"""生アーカイブからの組み直し（`jobs/verify_weather_archive.py`、W4 プラン §6.8 の PR L）。

**主題は「戻せると言い切れること」。** 学習サンプルは `weather_hourly`（30 日保持）から
作り直せるが、30 日を過ぎたら生アーカイブ（無期限）から戻すしかない——**その経路を
一度も動かしていなかった**（§8.5.2）。

**「たぶん戻せる」で持っている保険は、要るときに初めて壊れていることが分かる。**

ここで固定するのは 3 つ。

  * **一致を一致と言う**（縮約した実データで、読む → 解く → 突き合わせるが通る）
  * **食い違いを見落とさない**（値・系列・格子・長さのどれがずれても出る）
  * **書かない**（書く口を触ったら落ちる）
"""

import math
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from bikechance_ml.features.constants import WEATHER_LEAD_HOURS
from bikechance_ml.features.weather import WeatherRow
from bikechance_ml.jobs import verify_weather_archive as verifier
from bikechance_ml.jobs.weather_archive import (
    SERIES,
    CellForecast,
    issued_hour_of,
    read_batch,
    weather_object_path,
)

SAMPLE: Final[Path] = Path(__file__).resolve().parents[3] / "fixtures" / "weather"
BODY: Final[bytes] = (SAMPLE / "jma_msm_sample.json.gz").read_bytes()

#: 縮約元のファイルの発行時刻（2026/09/10/jma_msm_1789002000_00.json.gz）。
HOUR_EPOCH_S: Final[int] = 1_789_002_000
ISSUED: Final[datetime] = issued_hour_of(HOUR_EPOCH_S)
AVAILABLE: Final[datetime] = datetime(2026, 9, 10, 1, 17, 38, tzinfo=UTC)

ARCHIVE: Final[tuple[CellForecast, ...]] = read_batch(BODY, ISSUED)


def _row_of(one: CellForecast) -> WeatherRow:
    """アーカイブの 1 格子を、**そのまま表に入っている形**に直す（NaN → NULL）。"""
    return WeatherRow(
        cell_lat_idx=one.cell_lat_idx,
        cell_lon_idx=one.cell_lon_idx,
        issued_hour=ISSUED,
        available_at=AVAILABLE,
        values={
            name: [None if math.isnan(value) else value for value in values]
            for name, values in one.values.items()
        },
    )


TABLE: Final[tuple[WeatherRow, ...]] = tuple(_row_of(one) for one in ARCHIVE)


class FakePort:
    """`VerifyPort` を満たす代役。**書く口も持たせて、触られたら落とす。**"""

    def __init__(
        self,
        *,
        bodies: Sequence[bytes] = (BODY,),
        rows: Sequence[WeatherRow] = TABLE,
        oldest: datetime | None = ISSUED,
    ) -> None:
        self.bodies = tuple(bodies)
        self.rows = tuple(rows)
        self.oldest = oldest
        self.asked: list[str] = []

    def oldest_weather_issue(self) -> datetime | None:
        return self.oldest

    def list_weather_issue(self, issued_hour: datetime) -> tuple[WeatherRow, ...]:
        self.asked.append(issued_hour.isoformat())
        return tuple(one for one in self.rows if one.issued_hour == issued_hour)

    def download(self, bucket: str, path: str) -> bytes | None:
        for batch, body in enumerate(self.bodies):
            if path == weather_object_path(HOUR_EPOCH_S, batch):
                return body
        return None

    def upsert_weather_hourly(self, rows: Sequence[object]) -> int:
        raise AssertionError("この検査は書かない")

    def job_started(self, job_name: str) -> int:
        raise AssertionError("この検査は記録もしない")


def _changed(cell: int, series: str, lead: int, value: float | None) -> tuple[WeatherRow, ...]:
    """表の 1 値だけを差し替える。"""
    rows = list(TABLE)
    values = {name: list(one) for name, one in rows[cell].values.items()}
    values[series][lead] = value
    rows[cell] = replace(rows[cell], values=values)
    return tuple(rows)


# ── 一致 ──────────────────────────────────────────────────────
def test_a_faithful_archive_matches_the_table() -> None:
    """**縮約した実データで、読む → 解く → 突き合わせるが通る。**"""
    outcome = verifier.verify(FakePort())
    assert outcome.ok
    assert outcome.n_cells_archive == outcome.n_cells_table == len(ARCHIVE)
    assert outcome.n_batches == 1
    assert outcome.mismatches == ()


def test_every_series_is_compared() -> None:
    """**`weather_code` まで見る。** `list_weather` は 3 系列に絞るが、ここで絞ると
    「比べていない列がずれていても一致した」と言ってしまう。"""
    outcome = verifier.verify(FakePort())
    assert outcome.n_values == len(ARCHIVE) * len(SERIES) * WEATHER_LEAD_HOURS
    assert "weather_code" in SERIES


def test_nan_and_null_are_the_same_absence() -> None:
    """アーカイブの NaN と表の NULL は同じ「無い」。**0 で埋めない。**"""
    assert verifier.same_value(math.nan, None)
    assert not verifier.same_value(math.nan, 0.0)
    assert not verifier.same_value(0.0, None)
    assert verifier.same_value(1.5, 1.5)


# ── 食い違い ──────────────────────────────────────────────────
def test_a_changed_value_is_found_and_named() -> None:
    """**どの格子の、どの系列の、何時間先か**まで出す。"""
    outcome = verifier.verify(FakePort(rows=_changed(0, "temp_c", 3, 99.0)))
    assert not outcome.ok
    assert len(outcome.mismatches) == 1
    found = outcome.mismatches[0]
    assert (found.series, found.lead, found.in_table) == ("temp_c", 3, 99.0)
    assert found.cell == (ARCHIVE[0].cell_lat_idx, ARCHIVE[0].cell_lon_idx)


def test_a_value_that_became_null_is_found() -> None:
    """**「入っていたはずの値が NULL になった」を見落とさない。**"""
    outcome = verifier.verify(FakePort(rows=_changed(0, "precip_mm", 0, None)))
    assert len(outcome.mismatches) == 1
    assert outcome.mismatches[0].in_table is None


def test_the_weather_code_column_is_checked_too() -> None:
    """3 系列だけ見ていれば、ここが素通りする。"""
    outcome = verifier.verify(FakePort(rows=_changed(1, "weather_code", 2, 61.0)))
    assert [one.series for one in outcome.mismatches] == ["weather_code"]


def test_a_short_array_in_the_table_is_found() -> None:
    """**長さが足りない**（取り込みが途中で切れた形）も食い違いにする。"""
    rows = list(TABLE)
    values = {name: list(one)[:2] for name, one in rows[0].values.items()}
    rows[0] = replace(rows[0], values=values)
    outcome = verifier.verify(FakePort(rows=tuple(rows)))
    assert len(outcome.mismatches) == len(SERIES) * (WEATHER_LEAD_HOURS - 2)


def test_a_cell_only_in_the_archive_is_reported() -> None:
    """**取り込み漏れ。** アーカイブに在って表に無い格子。"""
    outcome = verifier.verify(FakePort(rows=TABLE[:1]))
    assert not outcome.ok
    assert outcome.only_in_archive == ((ARCHIVE[1].cell_lat_idx, ARCHIVE[1].cell_lon_idx),)
    assert outcome.only_in_table == ()


def test_a_cell_only_in_the_table_is_reported() -> None:
    """**アーカイブが欠けている。** 表に在ってアーカイブに無い格子。"""
    extra = replace(TABLE[0], cell_lat_idx=TABLE[0].cell_lat_idx + 1000)
    outcome = verifier.verify(FakePort(rows=(*TABLE, extra)))
    assert not outcome.ok
    assert outcome.only_in_table == ((extra.cell_lat_idx, extra.cell_lon_idx),)


def test_the_listed_mismatches_are_capped() -> None:
    """**全部は出さない**（595 格子 × 4 系列 × 8 時間ある）が、件数は正しく数える。"""
    rows = tuple(
        replace(one, values={name: [None] * WEATHER_LEAD_HOURS for name in SERIES}) for one in TABLE
    )
    outcome = verifier.verify(FakePort(rows=rows))
    listed = outcome.as_dict()["mismatches"]
    assert isinstance(listed, list)
    assert len(listed) == verifier.MAX_REPORTED
    assert outcome.as_dict()["n_mismatches"] == len(outcome.mismatches) > verifier.MAX_REPORTED


# ── 分割 ──────────────────────────────────────────────────────
def test_batches_are_read_until_one_is_missing() -> None:
    """**分割数を DB から貰わない。** 在るものを数える。"""
    forecasts, batches = verifier.read_archive(FakePort(bodies=(BODY, BODY, BODY)), ISSUED)
    assert batches == 3
    assert len(forecasts) == len(ARCHIVE) * 3


def test_an_empty_archive_is_not_a_match() -> None:
    """**1 分割も無ければ「一致」にしない。** 0 件どうしを一致と言わない。"""
    outcome = verifier.verify(FakePort(bodies=()))
    assert not outcome.ok
    assert (outcome.n_batches, outcome.n_cells_archive) == (0, 0)
    assert len(outcome.only_in_table) == len(ARCHIVE)


# ── 対象の選び方 ──────────────────────────────────────────────
def test_the_default_target_is_the_oldest_issue() -> None:
    """**次に消えるものを見る。** 指定しなければいちばん古い発行。"""
    port = FakePort()
    verifier.verify(port)
    assert port.asked == [ISSUED.isoformat()]


def test_no_issue_at_all_is_loud() -> None:
    """**「1 つも無い」と「その発行が無い」を言い分ける。** 直し方が違う。"""
    with pytest.raises(verifier.NoIssueError, match="1 つもありません"):
        verifier.verify(FakePort(oldest=None))


def test_an_issue_missing_from_the_table_is_loud() -> None:
    """**無いものを「一致した」と言わない。**"""
    with pytest.raises(verifier.NoIssueError, match="2026-01-01"):
        verifier.verify(FakePort(), datetime(2026, 1, 1, tzinfo=UTC))


# ── 書かない ──────────────────────────────────────────────────
def test_nothing_is_written() -> None:
    """**書く口を触ったら落ちる代役**を渡している。通れば触っていない。"""
    assert verifier.verify(FakePort()).ok


def test_the_command_line_takes_an_issued_hour() -> None:
    assert verifier._arguments([]).issued_hour is None
    assert verifier._arguments(["--issued-hour", "2026-09-08T00:00:00Z"]).issued_hour == (
        "2026-09-08T00:00:00Z"
    )
