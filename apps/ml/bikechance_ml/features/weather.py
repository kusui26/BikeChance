"""天気の予報を「基準時刻から引ける形」にする（W4 プラン §6.4、開発プラン §6.2）。

**純粋。** DB は触らない（読み出しは `io/supabase.py`）。

規律は 1 つだけで、それが全部である。

    **基準時刻 `t` で使ってよいのは `available_at <= t` の発行だけ。**

`issued_hour`（＝パスに入っている「取得を始めた時刻を時に丸めた値」）で引いてはいけない。
実測では保存されるのは常にその **17.6 分あと**で、`issued_hour <= t` で引くと毎正時から
18 分のあいだ**まだ発行されていない予報**を使う（5 分格子点の 30%。W3-06）。

引き方は `Grid.shifted` と同じ考えで、**添字の算術に落としてある**。

  * どの発行を使うか … `available_at` の昇順の列に対する二分探索（基準時刻ごと）
  * どの格子か       … ポートの座標を整数添字に丸めた鍵（ポートごと）
  * どの時間帯か     … `(対象の毎正時 − issued_hour) / 1 時間`（行ごと）

**時間帯の取り方は変数で違う。** `precipitation` は「直前 1 時間の合計」なので `t` を
含む時間帯（`t` 以上で最小の毎正時）を引く。気温・風速は瞬時値なので `t` に**最も近い**
毎正時を引く。同じ添字で両方を引くと、気温だけが最大 1 時間先の値になる。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final

import numpy as np

from bikechance_ml.features.arrays import Bools, Float32, Float64, Int32, Int64
from bikechance_ml.features.constants import (
    HORIZONS_MIN,
    WEATHER_GRID_LAT_STEP,
    WEATHER_GRID_LON_STEP,
)
from bikechance_ml.features.grid import day_start, to_epoch_ms

#: 特徴量に載せる系列。**`weather_hourly` の列名と同じ**（読む列もこれで決まる）。
#: `weather_code` は読まない：降水量と重なり、独自なのは「降水量 0 のときの雲量」だけで、
#: いまはそれを使う根拠が無い（表には残してある。W4 プラン §6.4）。
SERIES: Final[tuple[str, ...]] = ("precip_mm", "temp_c", "wind_kmh")

#: 「無い」を表す添字。**負の添字は numpy では通ってしまう**ので、必ず判定してから引く。
ABSENT: Final[int] = -1

#: 読み出す `available_at` の窓（推論）。**規律ではなく読み込み量の都合**である
#: （どの発行を使うかを決めるのは `Weather.issue_at` の 1 か所）。
#: 3 時間あれば、取得が 2 回続けて落ちても 1 つは入る。
SERVING_LOOKBACK_HOURS: Final[int] = 3

_MS_PER_HOUR: Final[int] = 3_600_000


class WeatherLeakError(RuntimeError):
    """`available_at` より前の基準時刻に、その発行を使おうとした。**黙って進まない。**

    添字の算術（`searchsorted(..., side="right") - 1`）が正しければ起こらない。
    **起こったときに気づけるようにするために置いてある**（W4 プラン §6.4 の完了条件）。
    """


def cell_key(lat: float, lon: float) -> tuple[int, int]:
    """座標 → 気象格子の整数添字。**度のまま突き合わせない。**

    Open-Meteo の応答の座標は float32 で、26.2 が `26.199999` として返る
    （実測 2026-09-10。595 格子のうち 8 件）。等値比較では当たらない。

    **丸めは Postgres の `round(double precision)` と同じ**（偶数丸め）。
    `weather_grid_cells`（0015）が予報を取る座標を決めているので、ここがずれると
    「取ってある格子」と「引きにいく格子」が食い違う。実測でちょうど半端になる
    ポートが 1 件あり（`136.65625 / 0.0625 = 2186.5`）、両方とも 2186 に落ちる。
    """
    return (round(lat / WEATHER_GRID_LAT_STEP), round(lon / WEATHER_GRID_LON_STEP))


def required_lead_hours(missed_issues: int = 0) -> int:
    """1 行が持つべき時間数（先頭は発行時刻）。**`WEATHER_LEAD_HOURS` の根拠。**

    発行 `E` のファイルが「最新」でいられるのは、次の発行が入手できるまでである。
    毎時取得なので `1 時間`、取れなかった回数だけ延びる。入手そのものにも 1 時間まで
    かかりうる（実測 17.6 分）。そのあいだの `t` に対して引く最も先の時間帯は
    `t + 最長の水平` を含む毎正時なので、
    **`(2 + 落ちた回数) 時間 + 最長の水平` を時間に切り上げ、`lead 0` のぶんを足す。**
    """
    span_minutes = (2 + missed_issues) * 60 + max(HORIZONS_MIN)
    return -(-span_minutes // 60) + 1


@dataclass(frozen=True)
class WeatherRow:
    """`weather_hourly` の 1 行。**1 格子 × 1 発行で、時間帯は配列。**"""

    cell_lat_idx: int
    cell_lon_idx: int
    issued_hour: datetime
    available_at: datetime
    values: Mapping[str, Sequence[float | None]]


@dataclass(frozen=True)
class Weather:
    """発行 × 格子 × 時間帯の予報。**添字の算術だけで引ける形。**

    `available_ms` は昇順。`series` の各配列は `(発行, 格子, 時間帯)` で、
    時間帯の添字 `k` は `issued_ms + k 時間` を指す。欠けた値は NaN。
    """

    available_ms: Int64
    issued_ms: Int64
    cells: Mapping[tuple[int, int], int]
    series: Mapping[str, Float32]

    @property
    def n_issues(self) -> int:
        return int(self.available_ms.size)

    @property
    def n_leads(self) -> int:
        return int(self.series[SERIES[0]].shape[2])

    def issue_at(self, times_ms: Int64) -> Int32:
        """基準時刻ごとに、**使ってよい中でいちばん新しい発行**の添字（無ければ -1）。"""
        if self.n_issues == 0:
            return np.full(times_ms.shape, ABSENT, dtype=np.int32)
        found = np.searchsorted(self.available_ms, times_ms, side="right") - 1
        issue = np.asarray(found, dtype=np.int32)
        self._refuse_leak(issue, times_ms)
        return issue

    def _refuse_leak(self, issue: Int32, times_ms: Int64) -> None:
        """**入手より前の基準時刻に使っていないか。** 見張りであって、規律そのものではない。"""
        known: Bools = issue >= 0
        if not bool(np.any(known)):
            return
        used = self.available_ms[np.where(known, issue, 0)]
        late: Bools = known & (used > times_ms)
        if bool(np.any(late)):
            raise WeatherLeakError(f"入手より前の基準時刻で予報を引いた: {int(np.sum(late))} 点")

    def cell_at(self, lat: Float64, lon: Float64) -> Int32:
        """ポートごとの格子の添字（無ければ -1）。**座標が無いポートも -1。**"""
        missing: Bools = np.isnan(lat) | np.isnan(lon)
        lat_idx = np.rint(np.where(missing, 0.0, lat) / WEATHER_GRID_LAT_STEP).astype(np.int64)
        lon_idx = np.rint(np.where(missing, 0.0, lon) / WEATHER_GRID_LON_STEP).astype(np.int64)
        found = [
            ABSENT if absent else self.cells.get((int(one), int(other)), ABSENT)
            for absent, one, other in zip(missing, lat_idx, lon_idx, strict=True)
        ]
        return np.array(found, dtype=np.int32)

    def value(self, name: str, issue: Int32, cell: Int32, hour_ms: Int64) -> Float32:
        """1 行ぶんの値。**発行・格子・時間帯のどれかが無ければ NaN。**"""
        if self.n_issues == 0:
            return np.full(hour_ms.shape, np.nan, dtype=np.float32)
        known: Bools = (issue >= 0) & (cell >= 0)
        safe_issue = np.where(known, issue, 0)
        lead = (hour_ms - self.issued_ms[safe_issue]) // _MS_PER_HOUR
        usable: Bools = known & (lead >= 0) & (lead < self.n_leads)
        picked = self.series[name][safe_issue, np.where(known, cell, 0), np.where(usable, lead, 0)]
        return np.asarray(np.where(usable, picked, np.nan), dtype=np.float32)


def empty() -> Weather:
    """予報が 1 件も無いとき。**列は作るが値はすべて NULL になる。**"""
    return Weather(
        available_ms=np.zeros(0, dtype=np.int64),
        issued_ms=np.zeros(0, dtype=np.int64),
        cells={},
        series={name: np.zeros((0, 0, 0), dtype=np.float32) for name in SERIES},
    )


def to_weather(rows: Sequence[WeatherRow]) -> Weather:
    """DB の行を引ける形にする。**無い組み合わせは NaN**（0 で埋めない）。"""
    if not rows:
        return empty()
    issues = _issue_order(rows)
    cells = {key: index for index, key in enumerate(sorted({_cell_of(row) for row in rows}))}
    leads = max(len(row.values[SERIES[0]]) for row in rows)
    series = {
        name: np.full((len(issues), len(cells), leads), np.nan, dtype=np.float32) for name in SERIES
    }
    for row in rows:
        place = (issues[row.issued_hour][0], cells[_cell_of(row)])
        for name in SERIES:
            values = row.values[name]
            series[name][place][: len(values)] = [np.nan if one is None else one for one in values]
    order = sorted(issues.values())
    return Weather(
        available_ms=np.array([available for _, _, available in order], dtype=np.int64),
        issued_ms=np.array([issued for _, issued, _ in order], dtype=np.int64),
        cells=cells,
        series=series,
    )


def _issue_order(rows: Sequence[WeatherRow]) -> dict[datetime, tuple[int, int, int]]:
    """発行 → `(添字, issued_ms, available_ms)`。**`available_at` の昇順に番号を振る。**

    同じ発行に別々の `available_at` が来たら**遅いほうを採る**（安全側。1 発行の中で
    食い違うのは取り込みの誤りだが、早いほうを採ると使ってよい範囲が広がってしまう）。
    """
    latest: dict[datetime, int] = {}
    for row in rows:
        stamp = to_epoch_ms(row.available_at)
        latest[row.issued_hour] = max(latest.get(row.issued_hour, stamp), stamp)
    ordered = sorted(latest.items(), key=lambda pair: (pair[1], pair[0]))
    return {
        issued: (index, to_epoch_ms(issued), available)
        for index, (issued, available) in enumerate(ordered)
    }


def _cell_of(row: WeatherRow) -> tuple[int, int]:
    return (row.cell_lat_idx, row.cell_lon_idx)


# ── 時間帯の取り方 ────────────────────────────────────────────
def containing_hour_ms(times_ms: Int64) -> Int64:
    """`t` を含む 1 時間帯のラベル（**`t` 以上で最小の毎正時**）。

    `precipitation` は「直前 1 時間の合計」なので、ラベル `H` の値が覆うのは
    `(H − 1 時間, H]` である。`t` が入るのはこの時間帯。
    """
    return np.asarray(-((-times_ms) // _MS_PER_HOUR) * _MS_PER_HOUR, dtype=np.int64)


def nearest_hour_ms(times_ms: Int64) -> Int64:
    """`t` に最も近い毎正時（瞬時値の気温・風速用）。"""
    return np.asarray(
        ((times_ms + _MS_PER_HOUR // 2) // _MS_PER_HOUR) * _MS_PER_HOUR, dtype=np.int64
    )


# ── 読み出す窓 ────────────────────────────────────────────────
def training_window(day: date) -> tuple[datetime, datetime]:
    """1 日ぶんを組み立てるのに要る `available_at` の範囲（半開区間 `[start, end)`）。

    基準時刻はその JST 暦日の 288 点だけなので、**その日の 00:00 の直前に入手した
    発行から、その日の終わりまで**あればよい。前を 2 時間さかのぼるのは、取得が
    1 回落ちていても直前の発行に届くようにするため。
    """
    start = day_start(day)
    return (start - timedelta(hours=2), start + timedelta(days=1))


def serving_window(at: datetime) -> tuple[datetime, datetime]:
    """推論 1 点ぶんに要る範囲（半開区間 `[start, end)`）。**上限は基準時刻そのもの。**

    半開なので、`available_at` がちょうど `at` に一致する発行は読まれない。規律
    （`available_at <= t`）より**厳しい**側にずれるだけで、リークにはならない。
    `available_at` は `clock_timestamp()` 由来でマイクロ秒まで入るので、5 分格子の
    基準時刻とちょうど一致することは実際には起きない。
    """
    return (at - timedelta(hours=SERVING_LOOKBACK_HOURS), at)
