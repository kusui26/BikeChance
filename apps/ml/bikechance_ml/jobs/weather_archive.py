"""天気アーカイブの gzip JSON を読む（W4 プラン §6.4、W2 プラン §9.1）。

**純粋。** Storage も DB も触らない（読み書きは `jobs/load_weather.py`）。
`jobs/snapshot_table.py` と同じ役回りで、**保存されたバイト列と DB の行のあいだの
写像だけ**をここに置く。

読むのは **`jma_msm` の系列だけ**（W4-05）。`best_match` も同じファイルに入っているが、
混ぜると「どちらの値か」を記録する列が要る。**選べるうちは 1 本にする。**

時刻の扱いに 3 つの罠がある。どれも黙って 1 時間ずらす類の間違いなので、ここで閉じる。

  1. `hourly.time` は **JST の素の文字列**（`2026-09-10T00:00`）で、`utc_offset_seconds`
     は 32400。タイムゾーンを付けずに読むと 9 時間ずれる
  2. 先頭は**常に「取得した日の JST 00:00」**（59 発行すべてで確認）。取得時刻ではない
     ので、発行時刻に対応する添字は毎回数える
  3. `precipitation` は **直前 1 時間の合計**（Open-Meteo の "Preceding hour sum"）。
     `temperature_2m` / `wind_speed_10m` / `weather_code` は瞬時値。**同じ添字でも
     指している区間が違う**（引き方は `features/weather.py`）
"""

import gzip
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from bikechance_ml.features.constants import WEATHER_LEAD_HOURS
from bikechance_ml.features.grid import JST
from bikechance_ml.features.weather import cell_key
from bikechance_ml.json_shape import as_dict, as_float, as_list, as_str, field

#: 生アーカイブのバケット（0015）。`packages/shared` の `WEATHER_BUCKET` と同じ値。
WEATHER_BUCKET: Final[str] = "weather-raw"

#: 読むモデル（W4-05）。応答の列名は `{変数}_{モデル}` になる。
WEATHER_MODEL: Final[str] = "jma_msm"

#: 1 分割の格子数（`packages/shared` の `WEATHER_BATCH_SIZE`）。分割数の計算に使う。
WEATHER_BATCH_SIZE: Final[int] = 100

#: 列名 → Open-Meteo の変数名。**`weather_hourly` の列名が左**（単位を名前に持つ）。
#: `precipitation_probability` は入れない：JMA のモデルには無く、要求しても全件 null
#: （2026-09-10 に 210,630 値で確認。Open-Meteo の JMA API の変数一覧にも無い）。
SERIES: Final[Mapping[str, str]] = {
    "precip_mm": "precipitation",
    "temp_c": "temperature_2m",
    "wind_kmh": "wind_speed_10m",
    "weather_code": "weather_code",
}

#: 整数で持つ系列。JSON に載せるときだけ分ける（配列の型が `smallint[]`）。
_INTEGER_SERIES: Final[str] = "weather_code"

_SECONDS_PER_HOUR: Final[int] = 3600


class ArchiveShapeError(ValueError):
    """アーカイブが期待した形をしていない。**部分的に取り込まない。**"""


def issued_hour_of(hour_epoch_s: int) -> datetime:
    """`hour_epoch_s`（パスとログの鍵）を UTC の時刻に。"""
    return datetime.fromtimestamp(hour_epoch_s, tz=UTC)


def weather_object_path(hour_epoch_s: int, batch: int) -> str:
    """バケット内のパス。**`packages/shared/src/weather-path.ts` と同じ規約。**

        weather-raw/{YYYY}/{MM}/{DD}/jma_msm_{hour_epoch_s}_{NN}.json.gz

    日付は **UTC**（`gbfs-raw` と同じ。W1 プラン §11.5）。`NN` は分割の連番。
    """
    at = issued_hour_of(hour_epoch_s)
    return f"{at:%Y/%m/%d}/{WEATHER_MODEL}_{hour_epoch_s}_{batch:02d}.json.gz"


def batch_count(n_cells: int) -> int:
    """格子数から分割数を出す（取得側の `batchCells` と同じ刻み）。"""
    return -(-n_cells // WEATHER_BATCH_SIZE)


@dataclass(frozen=True)
class PendingIssue:
    """まだ取り込んでいない発行（`v_weather_pending` の 1 行）。

    `n_cells` は取得側が要求した格子数、`n_loaded` は取り込み済みの行数。
    **等しくなるまで「未処理」**なので、途中で落ちた発行も次の実行が拾う。
    """

    hour_epoch_s: int
    issued_hour: datetime
    available_at: datetime
    n_cells: int
    n_loaded: int


@dataclass(frozen=True)
class CellForecast:
    """1 格子 1 発行ぶん。`weather_hourly` の 1 行そのもの。

    欠けた値は **NaN** で持つ（補間しない）。JSON に載せるときだけ `null` に直す
    ——JSON に NaN は書けず、書けば相手が読めない。
    """

    cell_lat_idx: int
    cell_lon_idx: int
    values: Mapping[str, tuple[float, ...]]

    def as_row(self, issued_hour: datetime, available_at: datetime) -> dict[str, object]:
        """`upsert_weather_hourly` に渡す 1 行。"""
        return {
            "cell_lat_idx": self.cell_lat_idx,
            "cell_lon_idx": self.cell_lon_idx,
            "issued_hour": issued_hour.isoformat(),
            "available_at": available_at.isoformat(),
            **{name: _as_json(name, values) for name, values in self.values.items()},
        }


def _as_json(name: str, values: tuple[float, ...]) -> list[float | int | None]:
    """NaN を `null` に直す。天気コードは整数で送る（列が `smallint[]`）。"""
    if name == _INTEGER_SERIES:
        return [None if math.isnan(one) else int(one) for one in values]
    return [None if math.isnan(one) else one for one in values]


def read_batch(body: bytes, issued_hour: datetime) -> tuple[CellForecast, ...]:
    """1 分割の gzip JSON を読む。**1 地点だけならオブジェクトで返る**ので両方受ける。"""
    document = json.loads(gzip.decompress(body))
    places = document if isinstance(document, list) else [document]
    return tuple(_one_place(place, issued_hour) for place in as_list(places, "weather"))


def _one_place(place: object, issued_hour: datetime) -> CellForecast:
    fields = as_dict(place, "weather.place")
    hourly = as_dict(field(fields, "hourly", "weather.place"), "hourly")
    start = _lead_offset(hourly, issued_hour)
    lat = as_float(field(fields, "latitude", "weather.place"), "latitude")
    lon = as_float(field(fields, "longitude", "weather.place"), "longitude")
    lat_idx, lon_idx = cell_key(lat, lon)
    return CellForecast(
        cell_lat_idx=lat_idx,
        cell_lon_idx=lon_idx,
        values={name: _window(hourly, variable, start) for name, variable in SERIES.items()},
    )


def _lead_offset(hourly: Mapping[str, object], issued_hour: datetime) -> int:
    """発行時刻が `hourly.time` の何番目か。**足りなければ止める**（欠けたまま入れない）。"""
    times = as_list(field(hourly, "time", "hourly"), "hourly.time")
    if not times:
        raise ArchiveShapeError("hourly.time が空だった")
    first = _jst_hour(as_str(times[0], "hourly.time[0]"))
    start = int((issued_hour - first).total_seconds()) // _SECONDS_PER_HOUR
    if start < 0 or start + WEATHER_LEAD_HOURS > len(times):
        raise ArchiveShapeError(
            f"発行 {issued_hour.isoformat()} から {WEATHER_LEAD_HOURS} 時間ぶんが無い"
            f"（先頭 {first.isoformat()}・{len(times)} 点）"
        )
    return start


def _jst_hour(text: str) -> datetime:
    """`2026-09-10T00:00` を **JST として**読む（素の文字列を UTC と決めつけない）。"""
    return datetime.fromisoformat(text).replace(tzinfo=JST)


def _window(hourly: Mapping[str, object], variable: str, start: int) -> tuple[float, ...]:
    """`{変数}_jma_msm` を発行時刻から `WEATHER_LEAD_HOURS` 個。**null は NaN。**"""
    name = f"{variable}_{WEATHER_MODEL}"
    values = as_list(field(hourly, name, "hourly"), f"hourly.{name}")
    window = values[start : start + WEATHER_LEAD_HOURS]
    return tuple(_number(one, f"hourly.{name}") for one in window)


def _number(value: object, where: str) -> float:
    """数か `null`。**`null` は NaN**（欠測を 0 にしない）。"""
    return math.nan if value is None else as_float(value, where)


def to_rows(
    forecasts: Sequence[CellForecast], issued_hour: datetime, available_at: datetime
) -> list[dict[str, object]]:
    """`upsert_weather_hourly` に渡す行の並び。"""
    return [one.as_row(issued_hour, available_at) for one in forecasts]
