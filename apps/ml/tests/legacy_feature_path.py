"""特徴量の経路 B（W6 の PR G）の**前の実装**を、そのまま写したもの（契約 40）。

**物差しとして残す**：経路 B の 4 か所（①〜④）は、**どんな入力でも前の実装と 1 バイトも
違わない**ことが約束である（W6-20、契約 40）。性質検査（`test_feature_path_b.py`）と、
本番の過去の時刻で比べる道具（`scripts/compare-feature-paths.py`）が、ここを相手にする。

**ここは直さない。** 写した元は 2026-10-03 の main（3d7dfde）の
`json_shape.as_int_list`・`jobs/snapshot_table.to_table`・`features/asof.to_observations`・
`features/flow.compute_flow`。直すと物差しが動く。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import numpy as np
import pyarrow as pa

from bikechance_ml.features.arrays import Float32, Int16, Int32, Int64, Span
from bikechance_ml.features.asof import NO_ROW, Observations
from bikechance_ml.features.constants import CHANGE_CAP_MINUTES, FLOW_MINUTES, MISSING
from bikechance_ml.features.flow import Flow
from bikechance_ml.jobs.snapshot_table import (
    SCHEMA,
    InconsistentSnapshotError,
    Snapshot,
)
from bikechance_ml.json_shape import ShapeError, as_list

_MS_PER_MINUTE: Final[int] = 60_000


# ── ① json_shape.as_int_list ─────────────────────────────────
def as_int_list(value: object, where: str) -> list[int]:
    """整数の配列。`smallint[]` は要素に null を含まない（欠損は -1）。"""
    items = as_list(value, where)
    numbers: list[int] = []
    for item in items:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ShapeError(f"{where}: 要素が整数でない")
        numbers.append(item)
    return numbers


# ── ② jobs/snapshot_table.to_table ────────────────────────────
def _check(snapshot: Snapshot, n_stations: int) -> None:
    at = f"{snapshot.observed_at:%Y-%m-%dT%H:%M:%SZ}"
    length = snapshot.length()
    lengths = (length, len(snapshot.docks), len(snapshot.flags), len(snapshot.reported_age_s))
    if len(set(lengths)) != 1:
        raise InconsistentSnapshotError(f"{at}: 配列の長さが揃っていない")
    if length > n_stations:
        # 台帳から行は消えない（W1 プラン §11.1）。長ければ台帳の読み違い
        raise InconsistentSnapshotError(f"{at}: 配列長 {length} が台帳 {n_stations} を超えた")


def to_table(
    system_id: str,
    station_ids: Sequence[str],
    snapshots: Sequence[Snapshot],
) -> pa.Table:
    """スナップショット群を長形式の表にする。**`station_id, observed_at` 順に並べる。**

    **配列長より外側の `idx` は行にしない**（§9.2）。当時まだ台帳に無かったポートで、
    行を作ると「登録済みだが現れなかった」（`-1`）と区別できなくなる。
    """
    columns = _accumulate(station_ids, snapshots)
    table = pa.table(
        {
            "system_id": pa.array([system_id] * len(columns.station_id), type=pa.string()),
            "station_id": pa.array(columns.station_id, type=pa.string()),
            "observed_at": pa.array(columns.observed_at, type=pa.timestamp("ms", tz="UTC")),
            "fetched_at": pa.array(columns.fetched_at, type=pa.timestamp("ms", tz="UTC")),
            "bikes": pa.array(columns.bikes, type=pa.int16()),
            "docks": pa.array(columns.docks, type=pa.int16()),
            "flags": pa.array(columns.flags, type=pa.int16()),
            "reported_age_s": pa.array(columns.reported_age_s, type=pa.int16()),
        },
        schema=SCHEMA,
    )
    # 学習はポート単位の時系列を読む。ここで並べておくと必要な範囲だけを読める（§9.2）
    return table.sort_by([("station_id", "ascending"), ("observed_at", "ascending")])


@dataclass
class _Columns:
    """組み立て途中の列。表を作るまでの間だけ使う。"""

    station_id: list[str]
    observed_at: list[datetime]
    fetched_at: list[datetime]
    bikes: list[int]
    docks: list[int]
    flags: list[int]
    reported_age_s: list[int]


def _accumulate(station_ids: Sequence[str], snapshots: Sequence[Snapshot]) -> _Columns:
    columns = _Columns([], [], [], [], [], [], [])
    for snapshot in sorted(snapshots, key=lambda one: one.observed_at):
        _check(snapshot, len(station_ids))
        length = snapshot.length()
        columns.station_id.extend(station_ids[:length])
        columns.observed_at.extend([snapshot.observed_at] * length)
        columns.fetched_at.extend([snapshot.fetched_at] * length)
        columns.bikes.extend(snapshot.bikes)
        columns.docks.extend(snapshot.docks)
        columns.flags.extend(snapshot.flags)
        columns.reported_age_s.extend(snapshot.reported_age_s)
    return columns


# ── ③ features/asof.to_observations ────────────────────────────
def to_observations(table: pa.Table, keys: Sequence[tuple[str, str]]) -> Observations:
    """Parquet の表を、`keys` の順に並べ替えた区間の形にする。

    キーは `(system_id, station_id)`。**近傍はシステムを跨ぐ**ので、2 システムを
    1 つの台帳にまとめて位置を決める（W3 プラン §4.3 の 22）。

    **`keys` に無いポートの行は捨てる**（台帳が正）。逆に、行が 1 つも無いポートは
    空の区間になる。
    """
    position = {key: index for index, key in enumerate(keys)}
    pairs = zip(
        table.column("system_id").to_pylist(), table.column("station_id").to_pylist(), strict=True
    )
    mapped = np.fromiter(
        (position.get(pair, NO_ROW) for pair in pairs), dtype=np.int64, count=table.num_rows
    )
    keep = mapped >= 0
    order = np.lexsort((_int64(table, "observed_at"), mapped))
    order = order[keep[order]]
    sorted_station = mapped[order]
    starts = np.searchsorted(sorted_station, np.arange(len(keys) + 1), side="left")
    return Observations(
        starts=starts.astype(np.int64),
        observed_at_ms=_int64(table, "observed_at")[order],
        fetched_at_ms=_int64(table, "fetched_at")[order],
        bikes=_int16(table, "bikes")[order],
        docks=_int16(table, "docks")[order],
        flags=_int16(table, "flags")[order],
        reported_age_s=_int16(table, "reported_age_s")[order],
    )


def _int64(table: pa.Table, name: str) -> Int64:
    """時刻の列をエポックミリ秒の int64 にする。"""
    column = table.column(name).cast(pa.int64()).combine_chunks()
    return np.asarray(column.to_numpy(zero_copy_only=False), dtype=np.int64)


def _int16(table: pa.Table, name: str) -> Int16:
    column = table.column(name).combine_chunks()
    return np.asarray(column.to_numpy(zero_copy_only=False), dtype=np.int16)


# ── ④ features/flow.compute_flow ──────────────────────────────
def compute_flow(observations: Observations, window_minutes: int = FLOW_MINUTES) -> Flow:
    """全ポートぶんの流量。ポート毎に、そのポートの観測列だけを見る。"""
    n_rows = observations.n_rows
    flow = Flow(
        rentals=np.zeros(n_rows, dtype=np.int32),
        returns=np.zeros(n_rows, dtype=np.int32),
        n_changes=np.zeros(n_rows, dtype=np.int32),
        minutes_since_last_change=np.full(n_rows, np.nan, dtype=np.float32),
    )
    window_ms = window_minutes * _MS_PER_MINUTE
    for station in range(observations.n_stations):
        rows = observations.rows_of(station)
        if rows.stop == rows.start:
            continue
        _fill_station(observations, rows, window_ms, flow)
    return flow


def _fill_station(observations: Observations, rows: Span, window_ms: int, flow: Flow) -> None:
    """1 ポートぶんを書き込む。**観測された行だけで差分を取る。**"""
    times = observations.observed_at_ms[rows]
    bikes = observations.bikes[rows]
    seen = bikes != MISSING
    if int(seen.sum()) < 2:
        return
    change_times = times[seen][1:]
    delta = np.diff(bikes[seen].astype(np.int32)).astype(np.int32)

    at_from, at_to = _window_bounds(times, change_times, window_ms)
    flow.rentals[rows] = _windowed_sum(np.clip(-delta, 0, None).astype(np.int32), at_from, at_to)
    flow.returns[rows] = _windowed_sum(np.clip(delta, 0, None).astype(np.int32), at_from, at_to)
    flow.n_changes[rows] = _windowed_sum((delta != 0).astype(np.int32), at_from, at_to)
    flow.minutes_since_last_change[rows] = _since_last_change(times, change_times[delta != 0])


def _window_bounds(times: Int64, change_times: Int64, window_ms: int) -> tuple[Int64, Int64]:
    """各観測時刻について、窓に入る差分の範囲 `[from, to)` を返す。"""
    at_to = np.searchsorted(change_times, times, side="right")
    at_from = np.searchsorted(change_times, times - window_ms, side="right")
    return at_from, at_to


def _windowed_sum(values: Int32, at_from: Int64, at_to: Int64) -> Int32:
    """累積和の差で窓の合計を出す。**`int64` で足す**（`int16` のままだと桁があふれる）。"""
    cumulative = np.concatenate([np.zeros(1, dtype=np.int64), np.cumsum(values, dtype=np.int64)])
    return np.asarray(cumulative[at_to] - cumulative[at_from], dtype=np.int32)


def _since_last_change(times: Int64, change_times: Int64) -> Float32:
    """最後に台数が変わってからの分。**`CHANGE_CAP_MINUTES` で頭打ちにする。**"""
    cap = float(CHANGE_CAP_MINUTES)
    if len(change_times) == 0:
        return np.full(len(times), cap, dtype=np.float32)
    taken = np.searchsorted(change_times, times, side="right")
    elapsed = np.where(
        taken > 0, (times - change_times[np.maximum(taken - 1, 0)]) / _MS_PER_MINUTE, cap
    )
    return np.asarray(np.minimum(elapsed, cap), dtype=np.float32)
