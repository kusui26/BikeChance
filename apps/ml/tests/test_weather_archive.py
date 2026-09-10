"""天気アーカイブの読み方（`jobs/weather_archive.py`）。

**縮約した実データを読む**（`fixtures/weather/jma_msm_sample.json.gz`）。本番の
1 ファイルから 2 地点・24 時間ぶんを切り出したもので、`best_match` の列も
`precipitation_probability` の null も**そのまま**入っている。

ここで見るのは 3 つ。
  * **時刻を 9 時間ずらしていないか**（`hourly.time` は JST の素の文字列）
  * **`jma_msm` だけを読んでいるか**（`best_match` を混ぜない。W4-05）
  * **欠けた値を作っていないか**（null は NaN のまま JSON の null に戻る）
"""

import gzip
import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

import pytest

from bikechance_ml.features.constants import WEATHER_LEAD_HOURS
from bikechance_ml.features.grid import JST
from bikechance_ml.features.weather import cell_key
from bikechance_ml.jobs.weather_archive import (
    SERIES,
    ArchiveShapeError,
    batch_count,
    issued_hour_of,
    read_batch,
    to_rows,
    weather_object_path,
)

SAMPLE: Final[Path] = Path(__file__).resolve().parents[3] / "fixtures" / "weather"
BODY: Final[bytes] = (SAMPLE / "jma_msm_sample.json.gz").read_bytes()

#: 縮約元のファイルの発行時刻（2026/09/10/jma_msm_1789002000_00.json.gz）。
HOUR_EPOCH_S: Final[int] = 1_789_002_000
ISSUED: Final[datetime] = issued_hour_of(HOUR_EPOCH_S)


# ── パス ──────────────────────────────────────────────────────
def test_the_path_matches_the_archive() -> None:
    """**本番に置いてあるパスと同じ文字列**（`weather-path.ts` の規約）。"""
    assert weather_object_path(HOUR_EPOCH_S, 0) == "2026/09/10/jma_msm_1789002000_00.json.gz"
    assert weather_object_path(HOUR_EPOCH_S, 5) == "2026/09/10/jma_msm_1789002000_05.json.gz"


def test_the_path_date_is_utc() -> None:
    """**日付は UTC**（JST で切ると 1 日ずれる。W1 プラン §11.5）。"""
    # 2026-09-07 15:00 UTC ＝ 2026-09-08 00:00 JST。パスは 09/07 になる
    assert weather_object_path(1_788_793_200, 0).startswith("2026/09/07/")


def test_batches_follow_the_fetcher() -> None:
    """595 格子は 100 ずつで 6 分割（取得側の `batchCells` と同じ刻み）。"""
    assert batch_count(595) == 6
    assert batch_count(100) == 1
    assert batch_count(101) == 2


# ── 格子 ──────────────────────────────────────────────────────
def test_the_cell_key_is_an_integer_pair() -> None:
    assert cell_key(35.68, 139.76) == (714, 2236)


def test_the_cell_key_survives_float32() -> None:
    """**26.2 は `26.199999` として返る。** 度のまま突き合わせたら当たらない。"""
    assert cell_key(26.199999, 127.625) == cell_key(26.2, 127.625)


# ── 読み出し ──────────────────────────────────────────────────
def test_it_reads_every_place() -> None:
    forecasts = read_batch(BODY, ISSUED)
    assert len(forecasts) == 2
    assert {(one.cell_lat_idx, one.cell_lon_idx) for one in forecasts} == {(487, 1986), (487, 1987)}


def test_it_keeps_exactly_the_lead_hours() -> None:
    for one in read_batch(BODY, ISSUED):
        for name in SERIES:
            assert len(one.values[name]) == WEATHER_LEAD_HOURS


def test_it_reads_jma_msm_and_not_best_match() -> None:
    """**混ぜない**（W4-05）。同じファイルの `best_match` は別の値を持つ。"""
    document = json.loads(gzip.decompress(BODY))
    hourly = document[0]["hourly"]
    start = _lead_start(hourly["time"][0])
    forecast = read_batch(BODY, ISSUED)[0]
    for column, variable in SERIES.items():
        expected = hourly[f"{variable}_jma_msm"][start : start + WEATHER_LEAD_HOURS]
        assert list(forecast.values[column]) == expected


def _lead_start(first: str) -> int:
    """`hourly.time[0]`（JST の素の文字列）から発行時刻までの時間数。"""
    start = datetime.fromisoformat(first).replace(tzinfo=JST)
    return int((ISSUED - start).total_seconds()) // 3600


def test_the_first_hour_is_read_as_jst() -> None:
    """**9 時間ずらさない。** UTC と決めつけると発行の添字が 9 つずれる。"""
    document = json.loads(gzip.decompress(BODY))
    # 2026-09-10T00:00 JST ＝ 2026-09-09T15:00 UTC。発行は 2026-09-10T01:00 UTC なので
    # 添字は 10 になる（UTC と読み違えると 1 になり、10 時間ぶん先の値が入る）
    assert _lead_start(document[0]["hourly"]["time"][0]) == 10
    assert ISSUED.isoformat() == "2026-09-10T01:00:00+00:00"


def test_precipitation_probability_is_not_read() -> None:
    """**JMA のモデルは降水確率を返さない**（要求しても全件 null）。列も作らない。"""
    document = json.loads(gzip.decompress(BODY))
    values = document[0]["hourly"]["precipitation_probability_jma_msm"]
    assert all(one is None for one in values), "前提が変わった（jma_msm が確率を返し始めた）"
    assert "precip_prob" not in SERIES


# ── 行にする ──────────────────────────────────────────────────
def test_rows_carry_both_timestamps() -> None:
    available = ISSUED + timedelta(minutes=17, seconds=38)
    rows = to_rows(read_batch(BODY, ISSUED), ISSUED, available)
    assert len(rows) == 2
    assert rows[0]["issued_hour"] == ISSUED.isoformat()
    assert rows[0]["available_at"] == available.isoformat()


def test_weather_code_is_sent_as_integers() -> None:
    """列が `smallint[]` なので、`51.0` ではなく `51` を送る。"""
    rows = to_rows(read_batch(BODY, ISSUED), ISSUED, ISSUED)
    codes = rows[0]["weather_code"]
    assert isinstance(codes, list)
    assert all(isinstance(one, int) for one in codes)


def test_null_becomes_null_and_not_zero() -> None:
    """**欠測を 0 にしない。** NaN で持ち、JSON には `null` として出す。"""
    body = _with_hole("temperature_2m_jma_msm", 10)
    forecast = read_batch(body, ISSUED)[0]
    assert math.isnan(forecast.values["temp_c"][0])
    row = to_rows((forecast,), ISSUED, ISSUED)[0]
    temperatures = row["temp_c"]
    assert isinstance(temperatures, list)
    assert temperatures[0] is None
    # **JSON にできる**（NaN のままだと相手が読めない）
    assert json.loads(json.dumps(row))["temp_c"][0] is None


def _with_hole(name: str, index: int) -> bytes:
    document = json.loads(gzip.decompress(BODY))
    document[0]["hourly"][name][index] = None
    return gzip.compress(json.dumps(document).encode())


# ── 足りないものは止める ──────────────────────────────────────
def test_it_refuses_a_file_that_does_not_reach_the_lead_hours() -> None:
    """**欠けたまま入れない。** 次の実行が拾えるように、その発行は未処理のまま残す。"""
    later = ISSUED + timedelta(hours=20)
    with pytest.raises(ArchiveShapeError):
        read_batch(BODY, later)


def test_it_refuses_a_file_that_starts_after_the_issue() -> None:
    earlier = ISSUED - timedelta(hours=20)
    with pytest.raises(ArchiveShapeError):
        read_batch(BODY, earlier)


def test_it_refuses_an_empty_time_axis() -> None:
    document = json.loads(gzip.decompress(BODY))
    document[0]["hourly"]["time"] = []
    with pytest.raises(ArchiveShapeError):
        read_batch(gzip.compress(json.dumps(document).encode()), ISSUED)


def test_a_single_place_comes_back_as_an_object() -> None:
    """**1 地点だけのときは配列でなくオブジェクト**（取得側の `countLocations` と同じ）。"""
    document = json.loads(gzip.decompress(BODY))
    body = gzip.compress(json.dumps(document[0]).encode())
    assert len(read_batch(body, ISSUED)) == 1
