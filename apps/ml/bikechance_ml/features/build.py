"""1 日ぶんの学習サンプルを組み立てる（W3 プラン §5.8・§9.3）。

**入力は Parquet と参照データだけ。** `status_snapshots` は読まない（学習と再現性の
ため、入力を 1 つに絞る）。副作用は持たない：読み書きは `jobs/build_features.py` にある。

組み立ての順序と、その理由：

  1. **拡張グリッド**（当日 288 点の前後に余白）に as-of を写す。余白はラグ・流量（前）と
     ラベル（後）のため
  2. **基準時刻 `t` 側の除外**を先に決める。母集団はここで確定し、抽出の重みはこの
     母集団に対して定義される
  3. **水平ごとに** `t + h` 側の除外・抽出・組み立てを行う。10 水平を同時に持つと
     `(ポート × 基準時刻 × 水平)` の行列が 10 枚生きて重くなる

**抽出は組み立てと同時に行う。** 5,731 万ペアを作ってから間引くのではない
（W3 プラン §5.8）。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

import numpy as np
import numpy.typing as npt
import pyarrow as pa

from bikechance_ml.features import asof, exclude, flow, labels, neighbors, sample, static
from bikechance_ml.features.arrays import (
    Bools,
    Float32,
    Int16,
    Int32,
    Int64,
    ScalarT,
    Span,
    Strings,
    UInt64,
)
from bikechance_ml.features.calendar import DayType, day_type, dow_type
from bikechance_ml.features.calendar import (
    is_day_before_holiday as calendar_is_day_before_holiday,
)
from bikechance_ml.features.calendar import is_last_business_day as calendar_is_last_business_day
from bikechance_ml.features.constants import (
    DELTA_MINUTES,
    FAR_RADIUS_M,
    FEATURE_SET,
    GRID_MINUTES,
    HORIZONS_MIN,
    LAG_MINUTES,
    MISSING,
    NEAR_RADIUS_M,
    ROLL_MINUTES,
    SAME_TIME_MINUTES,
    STRATUM_TIGHT,
    STRATUM_UNIFORM,
)
from bikechance_ml.features.grid import Grid, build_grid
from bikechance_ml.features.schema import SCHEMA, to_table

_MINUTES_PER_DAY: Final[int] = 24 * 60
_MS_PER_SECOND: Final[int] = 1000
_ONE_DAY: Final[timedelta] = timedelta(days=1)


@dataclass(frozen=True)
class Reference:
    """参照データ。**2 システムを 1 つの台帳にまとめたもの。**

    近傍がシステムを跨ぐので、位置の割り当ても跨いで 1 本にする必要がある
    （W3 プラン §4.3 の 22）。出力も 1 日 1 ファイルで、`system_id` は列になる。
    """

    facts: static.StationFacts
    links: neighbors.NeighborLinks
    holidays: frozenset[date]


@dataclass(frozen=True)
class DayInputs:
    """1 日ぶんの入力。`table` は全システムの Parquet を連結したもの。"""

    day: date
    reference: Reference
    table: pa.Table


@dataclass(frozen=True)
class DayStats:
    """その日の内訳（W3 プラン §5.8 の完了条件）。

    `pairs_total` は母集団（ポート × 288 点 × 10 水平）、`pairs_kept` は除外を通った数。
    `weight_sum` はその推定で、**両者が近いこと**が抽出の不偏性の確認になる（§7.6）。
    `excluded` は理由ごとの件数で、**合計が `pairs_total − pairs_kept` に一致する**。
    """

    date: str
    feature_set: str
    stations: int
    pairs_total: int
    pairs_kept: int
    rows: int
    weight_sum: float
    excluded: dict[str, int]

    def as_dict(self) -> dict[str, object]:
        """JSON に出す形。"""
        return {
            "date": self.date,
            "feature_set": self.feature_set,
            "stations": self.stations,
            "pairs_total": self.pairs_total,
            "pairs_kept": self.pairs_kept,
            "rows": self.rows,
            "weight_sum": self.weight_sum,
            "excluded": dict(sorted(self.excluded.items())),
        }


@dataclass(frozen=True)
class Built:
    """出力と、その日の内訳。"""

    table: pa.Table
    stats: DayStats


@dataclass(frozen=True)
class GridState:
    """拡張グリッド上の状態。行列の形はすべて `(ポート, グリッド点)`。"""

    feature_row: Int32
    label_row: Int32
    bikes: Int16
    docks: Int16
    flags: Int16
    reported_age_s: Int16
    observed_at_ms: Int64
    label_bikes: Int16
    label_docks: Int16
    label_flags: Int16
    label_observed_ms: Int64
    capacity: Int32
    valid: Bools
    rentals: Int32
    returns: Int32
    n_changes: Int32
    minutes_since_change: Float32


def build_day(inputs: DayInputs) -> Built:
    """1 日ぶんのサンプルを作る。"""
    grid = build_grid(inputs.day)
    state = _grid_state(inputs, grid)
    base = _base_exclusion(inputs, state, grid)
    pre = _precompute(inputs, grid, state)
    parts: list[pa.Table] = []
    # 基準時刻の側で落ちた行は**全水平で落ちる**ので、件数を水平の数だけ数える
    counts = {reason: number * len(HORIZONS_MIN) for reason, number in base.counts.items()}
    total = int(base.keep.size) * len(HORIZONS_MIN)
    for index, horizon in enumerate(HORIZONS_MIN):
        part, dropped = _one_horizon(inputs, grid, state, pre, base.keep, index, horizon)
        parts.append(part)
        for reason, number in dropped.items():
            counts[reason] = counts.get(reason, 0) + number
    table = _concat(parts)
    return Built(table=table, stats=_stats(inputs, table, counts, total))


# ── グリッドへの写像 ──────────────────────────────────────────
def _grid_state(inputs: DayInputs, grid: Grid) -> GridState:
    """観測を拡張グリッドへ写す。**特徴量とラベルで別々の as-of を使う。**"""
    observations = asof.to_observations(inputs.table, inputs.reference.facts.station_keys())
    feature_row = asof.as_of_feature(observations, grid.times_ms)
    label_row = asof.as_of_label(observations, grid.times_ms)
    flows = flow.compute_flow(observations)
    bikes = asof.gather(observations.bikes, feature_row, MISSING)
    observed = asof.gather(observations.observed_at_ms, feature_row, 0)
    times = np.asarray(grid.times_ms, dtype=np.int64)[None, :]
    return GridState(
        feature_row=feature_row,
        label_row=label_row,
        bikes=bikes,
        docks=asof.gather(observations.docks, feature_row, MISSING),
        flags=asof.gather(observations.flags, feature_row, MISSING),
        reported_age_s=asof.gather(observations.reported_age_s, feature_row, MISSING),
        observed_at_ms=observed,
        label_bikes=asof.gather(observations.bikes, label_row, MISSING),
        label_docks=asof.gather(observations.docks, label_row, MISSING),
        label_flags=asof.gather(observations.flags, label_row, MISSING),
        label_observed_ms=asof.gather(observations.observed_at_ms, label_row, 0),
        capacity=_capacity(inputs, observations, feature_row, len(grid)),
        valid=_valid(feature_row, bikes, observed, times),
        rentals=asof.gather(flows.rentals, feature_row, 0),
        returns=asof.gather(flows.returns, feature_row, 0),
        n_changes=asof.gather(flows.n_changes, feature_row, 0),
        minutes_since_change=asof.gather(flows.minutes_since_last_change, feature_row, np.nan),
    )


def _valid(feature_row: Int32, bikes: Int16, observed_ms: Int64, times: Int64) -> Bools:
    """その格子点の値を使ってよいか。**古すぎる観測とラグの穴をここで 1 度に潰す。**"""
    fresh = ~exclude.too_old(times, observed_ms, feature_row)
    return np.asarray((feature_row != asof.NO_ROW) & (bikes != MISSING) & fresh, dtype=np.bool_)


def _capacity(
    inputs: DayInputs, observations: asof.Observations, feature_row: Int32, n_grid: int
) -> Int32:
    """容量。**動的なシステムは累積最大の推定値、他は宣言値**（開発プラン §6.3）。"""
    facts = inputs.reference.facts
    estimated = asof.gather(static.running_capacity(observations), feature_row, static.NO_CODE)
    declared = static.declared_capacity_grid(facts, n_grid)
    return np.asarray(
        np.where(facts.has_dynamic_capacity[:, None], estimated, declared), dtype=np.int32
    )


def _base_exclusion(inputs: DayInputs, state: GridState, grid: Grid) -> exclude.Excluded:
    """基準時刻の側の除外。当日の 288 点だけを見る。"""
    day = grid.day_slice()
    return exclude.exclude_at_base(
        feature_row=state.feature_row[:, day],
        observed_at_ms=state.observed_at_ms[:, day],
        grid_ms=np.asarray(grid.day_times_ms(), dtype=np.int64)[None, :],
        bikes=state.bikes[:, day],
        docks=state.docks[:, day],
        flags=state.flags[:, day],
        phantom=exclude.phantom_mask(inputs.reference.facts.station_keys()),
    )


# ── グリッド上の派生量（水平に依らないもの）──────────────────
@dataclass(frozen=True)
class Precomputed:
    """当日の 288 点について、水平によらず 1 度だけ作れるもの。"""

    seeds: UInt64
    system_ids: Strings
    station_ids: Strings
    age_days: Float32
    #: 整数のラグ（`bikes_lag_*`・`delta_*`・`roll_min_60`・`roll_max_60`）
    lags: dict[str, Int16]
    lag_valid: dict[str, Bools]
    roll_mean: Float32
    roll_valid: Bools
    same_time_bikes: Int16
    same_time_valid: Bools
    #: 整数の近傍集約。平均だけ型が違うので分ける
    neighbor: dict[str, Int32]
    neighbor_fill_ratio: Float32
    urban_density: Int32
    tight: Bools


def _precompute(inputs: DayInputs, grid: Grid, state: GridState) -> Precomputed:
    """水平によらない材料をまとめて作る。"""
    day = grid.day_slice()
    facts = inputs.reference.facts
    lags, lag_valid = _lag_matrices(state, grid)
    same_time = grid.steps_per(SAME_TIME_MINUTES)
    past: Span = slice(day.start - same_time, day.stop - same_time, 1)
    mean, mean_valid = _rolling(state, grid)
    integer, ratio = _neighbor_matrices(inputs, state, grid)
    return Precomputed(
        seeds=_seeds(inputs),
        system_ids=np.array(facts.system_ids, dtype=np.str_),
        station_ids=np.array(facts.station_ids, dtype=np.str_),
        age_days=static.station_age_days(facts.first_seen_ms, grid.day_times_ms()),
        lags=lags,
        lag_valid=lag_valid,
        roll_mean=mean,
        roll_valid=mean_valid,
        same_time_bikes=state.bikes[:, past],
        same_time_valid=state.valid[:, past],
        neighbor=integer,
        neighbor_fill_ratio=ratio,
        urban_density=inputs.reference.links.within(FAR_RADIUS_M, same_system_only=False).counts(),
        tight=sample.is_tight(state.bikes[:, day], state.docks[:, day]),
    )


def _seeds(inputs: DayInputs) -> UInt64:
    """ポート毎の抽出の種。**日付とシステムと ID だけで決まる**（並び順に依らない）。"""
    return np.array(
        [
            sample.station_seed(inputs.day, system_id, station_id)
            for system_id, station_id in inputs.reference.facts.station_keys()
        ],
        dtype=np.uint64,
    )


def _lag_matrices(state: GridState, grid: Grid) -> tuple[dict[str, Int16], dict[str, Bools]]:
    """ラグ・差分と、移動窓の最小最大。**値が無い格子点は使わない**（補間しない）。"""
    day = grid.day_slice()
    values: dict[str, Int16] = {}
    valid: dict[str, Bools] = {}
    for minutes in LAG_MINUTES:
        past, past_valid = _shift_back(state, grid, minutes)
        values[f"bikes_lag_{minutes}"] = past
        valid[f"bikes_lag_{minutes}"] = past_valid
    for minutes in DELTA_MINUTES:
        past, past_valid = _shift_back(state, grid, minutes)
        values[f"delta_{minutes}"] = np.asarray(state.bikes[:, day] - past, dtype=np.int16)
        valid[f"delta_{minutes}"] = np.asarray(state.valid[:, day] & past_valid, dtype=np.bool_)
    lowest, highest, seen = _rolling_extremes(state, grid)
    values["roll_min_60"], valid["roll_min_60"] = lowest, seen
    values["roll_max_60"], valid["roll_max_60"] = highest, seen
    return values, valid


def _shift_back(state: GridState, grid: Grid, minutes: int) -> tuple[Int16, Bools]:
    """`minutes` 前の格子点の台数と、その有効性。"""
    steps = grid.steps_per(minutes)
    day = grid.day_slice()
    window: Span = slice(day.start - steps, day.stop - steps, 1)
    return state.bikes[:, window], state.valid[:, window]


def _rolling_offsets() -> range:
    """直近 60 分の格子点（**両端を含む 13 点**）。"""
    return range(0, ROLL_MINUTES + 1, GRID_MINUTES)


def _rolling(state: GridState, grid: Grid) -> tuple[Float32, Bools]:
    """直近 60 分の平均。**欠けた点は分母から外す**（0 で埋めない）。"""
    shape = state.bikes[:, grid.day_slice()].shape
    total = np.zeros(shape, dtype=np.float64)
    seen = np.zeros(shape, dtype=np.int32)
    for minutes in _rolling_offsets():
        bikes, ok = _shift_back(state, grid, minutes)
        total += np.where(ok, bikes, 0)
        seen += ok
    has = np.asarray(seen > 0, dtype=np.bool_)
    mean = np.where(has, total / np.maximum(seen, 1), np.nan)
    return np.asarray(mean, dtype=np.float32), has


def _rolling_extremes(state: GridState, grid: Grid) -> tuple[Int16, Int16, Bools]:
    """直近 60 分の最小・最大と、1 点でも有効だったか。"""
    shape = state.bikes[:, grid.day_slice()].shape
    lowest = np.full(shape, np.iinfo(np.int16).max, dtype=np.int16)
    highest = np.full(shape, np.iinfo(np.int16).min, dtype=np.int16)
    seen = np.zeros(shape, dtype=np.bool_)
    for minutes in _rolling_offsets():
        bikes, ok = _shift_back(state, grid, minutes)
        lowest = np.asarray(np.where(ok, np.minimum(lowest, bikes), lowest), dtype=np.int16)
        highest = np.asarray(np.where(ok, np.maximum(highest, bikes), highest), dtype=np.int16)
        seen = np.asarray(seen | ok, dtype=np.bool_)
    return (
        np.asarray(np.where(seen, lowest, 0), dtype=np.int16),
        np.asarray(np.where(seen, highest, 0), dtype=np.int16),
        seen,
    )


def _neighbor_matrices(
    inputs: DayInputs, state: GridState, grid: Grid
) -> tuple[dict[str, Int32], Float32]:
    """近傍の集約。**近傍 0 は合計 0・平均 NULL**（W3 プラン §9.5）。"""
    day = grid.day_slice()
    points = len(grid.day_times_ms())
    bikes = neighbors.observed_or_zero(state.bikes[:, day])
    docks = neighbors.observed_or_zero(state.docks[:, day])
    near = inputs.reference.links.within(NEAR_RADIUS_M, same_system_only=False)
    near_same = inputs.reference.links.within(NEAR_RADIUS_M, same_system_only=True)
    far = inputs.reference.links.within(FAR_RADIUS_M, same_system_only=False)
    empty = np.asarray(state.bikes[:, day] == 0, dtype=np.bool_)
    ratio = np.asarray(
        static.fill_ratio(neighbors.observed_nan(state.bikes[:, day]), state.capacity[:, day]),
        dtype=np.float64,
    )
    integer = {
        "nb_count_300m": _spread(near.counts(), points),
        "nb_count_500m": _spread(far.counts(), points),
        "nb_count_300m_same": _spread(near_same.counts(), points),
        "nb_bikes_sum_300m": neighbors.sum_over_neighbors(near, bikes),
        "nb_docks_sum_300m": neighbors.sum_over_neighbors(near, docks),
        "nb_bikes_sum_300m_same": neighbors.sum_over_neighbors(near_same, bikes),
        "nb_docks_sum_300m_same": neighbors.sum_over_neighbors(near_same, docks),
        "nb_n_empty_500m": neighbors.count_over_neighbors(far, empty),
    }
    return integer, neighbors.mean_over_neighbors(far, ratio)


def _spread(per_station: Int32, n_grid: int) -> Int32:
    """ポート毎の値を `(ポート, 基準時刻)` に広げる。"""
    return np.asarray(
        np.broadcast_to(per_station[:, None], (len(per_station), n_grid)), dtype=np.int32
    )


# ── 水平ごとの組み立て ────────────────────────────────────────
@dataclass(frozen=True)
class Picked:
    """抽出に当たった行。`(ポート, 基準時刻)` の位置と、水平の情報を持つ。"""

    stations: Int64
    points: Int64
    horizon_index: int
    horizon_min: int

    def __len__(self) -> int:
        return len(self.stations)


def _one_horizon(
    inputs: DayInputs,
    grid: Grid,
    state: GridState,
    pre: Precomputed,
    alive: Bools,
    index: int,
    horizon: int,
) -> tuple[pa.Table, dict[str, int]]:
    """1 つの水平について、除外・抽出・組み立てを行う。"""
    day = grid.day_slice()
    shift = grid.steps_per(horizon)
    target: Span = slice(day.start + shift, day.stop + shift, 1)
    dropped = exclude.exclude_at_target(
        alive=alive,
        feature_row=state.feature_row[:, day],
        label_row=state.label_row[:, target],
        observed_at_ms=state.label_observed_ms[:, target],
        target_ms=np.asarray(grid.times_ms[target], dtype=np.int64)[None, :],
        bikes=state.label_bikes[:, target],
        docks=state.label_docks[:, target],
    )
    accepted = dropped.keep & _accept(pre, index, len(grid.day_times_ms()))
    stations, points = np.nonzero(accepted)
    picked = Picked(stations, points, index, horizon)
    return _assemble(inputs, grid, state, pre, picked, target), dropped.counts


def _accept(pre: Precomputed, index: int, n_grid: int) -> Bools:
    """抽出の判定。**行ごとに 1 回だけ引く**（W3 プラン §9.3）。"""
    counters = (np.arange(n_grid, dtype=np.uint64) * np.uint64(len(HORIZONS_MIN))) + np.uint64(
        index
    )
    drawn = sample.uniform_array(pre.seeds[:, None], counters[None, :])
    return np.asarray(drawn < sample.rate_of(pre.tight), dtype=np.bool_)


# ── 列の組み立て ──────────────────────────────────────────────
def _assemble(
    inputs: DayInputs,
    grid: Grid,
    state: GridState,
    pre: Precomputed,
    picked: Picked,
    target: Span,
) -> pa.Table:
    """抽出された行を `SCHEMA` の列にする。"""
    day = grid.day_slice()
    columns: dict[str, pa.Array] = {}
    columns.update(_key_columns(grid, pre, picked))
    columns.update(_label_columns(state, picked, target))
    columns.update(_calendar_columns(inputs, picked))
    columns.update(_static_columns(inputs, state, pre, picked, day))
    columns.update(_state_columns(inputs, state, grid, picked, day))
    columns.update(_lag_columns(pre, picked))
    columns.update(_flow_columns(state, picked, day))
    columns.update(_history_columns(state, grid, pre, picked))
    columns.update(_neighbor_columns(pre, picked))
    return to_table(columns)


def _take(matrix: npt.NDArray[ScalarT], picked: Picked) -> npt.NDArray[ScalarT]:
    return matrix[picked.stations, picked.points]


def _col(values: object, name: str, null: Bools | None = None) -> pa.Array:
    """`SCHEMA` の型で列を作る。`null` が真の位置は NULL になる。"""
    return pa.array(values, type=SCHEMA.field(name).type, mask=null)


def _constant(value: object, name: str, count: int) -> pa.Array:
    return pa.array(np.full(count, value), type=SCHEMA.field(name).type)


def _key_columns(grid: Grid, pre: Precomputed, picked: Picked) -> dict[str, pa.Array]:
    times = np.asarray(grid.day_times_ms(), dtype=np.int64)[picked.points]
    tight = _take(pre.tight, picked)
    return {
        "system_id": pa.array(pre.system_ids[picked.stations], type=pa.string()),
        "station_id": pa.array(pre.station_ids[picked.stations], type=pa.string()),
        "t": pa.array(times, type=pa.int64()).cast(SCHEMA.field("t").type),
        "h_min": _constant(picked.horizon_min, "h_min", len(picked)),
        "weight": _col(sample.weight_of(tight), "weight"),
        "stratum": _col(np.where(tight, STRATUM_TIGHT, STRATUM_UNIFORM), "stratum"),
        "feature_set": _constant(FEATURE_SET, "feature_set", len(picked)),
    }


def _label_columns(state: GridState, picked: Picked, target: Span) -> dict[str, pa.Array]:
    bikes = _take(state.label_bikes[:, target], picked)
    docks = _take(state.label_docks[:, target], picked)
    flags = _take(state.label_flags[:, target], picked)
    return {
        "y_bike": _col(labels.y_bike(bikes, flags), "y_bike"),
        "y_dock": _col(labels.y_dock(docks, flags), "y_dock"),
    }


def _calendar_columns(inputs: DayInputs, picked: Picked) -> dict[str, pa.Array]:
    """暦。**基準日の属性はその日ひとつ**なので、行に広げるだけで済む。"""
    holidays = inputs.reference.holidays
    kind: DayType = day_type(inputs.day, holidays)
    minute = (picked.points * GRID_MINUTES).astype(np.int32)
    total = minute + picked.horizon_min
    count = len(picked)
    return {
        "minute_of_day": _col(minute.astype(np.int16), "minute_of_day"),
        "minute_of_day_sin": _col(_sin(minute), "minute_of_day_sin"),
        "minute_of_day_cos": _col(_cos(minute), "minute_of_day_cos"),
        "dow": _constant(inputs.day.weekday(), "dow", count),
        "day_type": _constant(kind, "day_type", count),
        "dow_type": _constant(dow_type(kind), "dow_type", count),
        "is_holiday": _constant(inputs.day in holidays, "is_holiday", count),
        "is_day_before_holiday": _constant(
            calendar_is_day_before_holiday(inputs.day, holidays), "is_day_before_holiday", count
        ),
        "is_last_business_day": _constant(
            calendar_is_last_business_day(inputs.day, holidays), "is_last_business_day", count
        ),
        "month": _constant(inputs.day.month, "month", count),
        "target_minute_of_day_sin": _col(_sin(total), "target_minute_of_day_sin"),
        "target_minute_of_day_cos": _col(_cos(total), "target_minute_of_day_cos"),
        "target_dow_type": _col(_target_dow_type(inputs, total), "target_dow_type"),
    }


def _sin(minute: Int32) -> Float32:
    turn = 2 * np.pi * (minute % _MINUTES_PER_DAY) / _MINUTES_PER_DAY
    return np.asarray(np.sin(turn), dtype=np.float32)


def _cos(minute: Int32) -> Float32:
    turn = 2 * np.pi * (minute % _MINUTES_PER_DAY) / _MINUTES_PER_DAY
    return np.asarray(np.cos(turn), dtype=np.float32)


def _target_dow_type(inputs: DayInputs, total_minute: Int32) -> Strings:
    """目標時刻の曜日種別。**水平は最長 180 分なので、跨ぐのは翌日まで。**"""
    holidays = inputs.reference.holidays
    today = dow_type(day_type(inputs.day, holidays))
    tomorrow = dow_type(day_type(inputs.day + _ONE_DAY, holidays))
    return np.asarray(np.where(total_minute >= _MINUTES_PER_DAY, tomorrow, today), dtype=np.str_)


def _static_columns(
    inputs: DayInputs, state: GridState, pre: Precomputed, picked: Picked, day: Span
) -> dict[str, pa.Array]:
    facts = inputs.reference.facts
    capacity = _take(state.capacity[:, day], picked)
    charging = facts.is_charging_station[picked.stations]
    return {
        "lat": _col(facts.lat[picked.stations], "lat"),
        "lon": _col(facts.lon[picked.stations], "lon"),
        **_code_column("pref_code", facts.pref_code[picked.stations]),
        **_code_column("muni_code", facts.muni_code[picked.stations]),
        **_code_column("region_id", facts.region_id[picked.stations]),
        "is_charging_station": _col(charging > 0, "is_charging_station", null=charging < 0),
        "station_age_days": _col(_take(pre.age_days, picked), "station_age_days"),
        "capacity": _col(
            np.where(capacity > 0, capacity, 0).astype(np.int16),
            "capacity",
            null=capacity <= 0,
        ),
        "urban_density_500m": _col(
            pre.urban_density[picked.stations].astype(np.int32), "urban_density_500m"
        ),
    }


def _code_column(name: str, values: Int32) -> dict[str, pa.Array]:
    """行政区画・地域の番号。**`-1` は「無い」なので NULL にする。**"""
    absent = values < 0
    return {name: _col(np.where(absent, 0, values), name, null=absent)}


def _state_columns(
    inputs: DayInputs, state: GridState, grid: Grid, picked: Picked, day: Span
) -> dict[str, pa.Array]:
    bikes = _take(state.bikes[:, day], picked).astype(np.int32)
    docks = _take(state.docks[:, day], picked).astype(np.int32)
    capacity = _take(state.capacity[:, day], picked)
    times = np.asarray(grid.day_times_ms(), dtype=np.int64)[picked.points]
    observed = _take(state.observed_at_ms[:, day], picked)
    age = _take(state.reported_age_s[:, day], picked).astype(np.int16)
    facts = inputs.reference.facts
    gap = np.asarray(
        np.where(facts.has_gap[picked.stations], static.gap(capacity, bikes, docks), np.nan),
        dtype=np.float32,
    )
    no_age = np.asarray(~facts.has_reported_age[picked.stations] | (age < 0), dtype=np.bool_)
    return {
        "bikes": _col(bikes.astype(np.int16), "bikes"),
        "docks": _col(docks.astype(np.int16), "docks"),
        "gap": _col(gap, "gap", null=np.isnan(gap)),
        **_float_column("fill_ratio", static.fill_ratio(bikes, capacity)),
        "is_over_capacity": _col(bikes > capacity, "is_over_capacity", null=capacity <= 0),
        "staleness_s": _col(age, "staleness_s", null=no_age),
        "feed_delay_s": _col(
            ((times - observed) // _MS_PER_SECOND).astype(np.int32), "feed_delay_s"
        ),
    }


def _float_column(name: str, values: Float32) -> dict[str, pa.Array]:
    """NaN を NULL として書く。**0 で埋めない。**"""
    return {name: _col(values, name, null=np.isnan(values))}


def _lag_columns(pre: Precomputed, picked: Picked) -> dict[str, pa.Array]:
    """ラグ・差分・移動窓。**有効でない格子点は NULL**（0 で埋めない）。"""
    columns: dict[str, pa.Array] = {}
    for name, matrix in pre.lags.items():
        absent = np.asarray(~_take(pre.lag_valid[name], picked), dtype=np.bool_)
        safe = np.asarray(np.where(absent, 0, _take(matrix, picked)), dtype=np.int16)
        columns[name] = _col(safe, name, null=absent)
    mean = _take(pre.roll_mean, picked)
    columns.update(_float_column("roll_mean_60", mean))
    return columns


def _flow_columns(state: GridState, picked: Picked, day: Span) -> dict[str, pa.Array]:
    """流量。**フィード本来の周期で計算済みの値を引くだけ**（W3-11）。"""
    minutes = _take(state.minutes_since_change[:, day], picked).astype(np.float32)
    return {
        "rentals_60": _col(_take(state.rentals[:, day], picked).astype(np.int32), "rentals_60"),
        "returns_60": _col(_take(state.returns[:, day], picked).astype(np.int32), "returns_60"),
        "n_changes_60": _col(
            _take(state.n_changes[:, day], picked).astype(np.int32), "n_changes_60"
        ),
        **_float_column("minutes_since_last_change", minutes),
    }


def _history_columns(
    state: GridState, grid: Grid, pre: Precomputed, picked: Picked
) -> dict[str, pa.Array]:
    """同時刻履歴（1 日前）。**`t` 時点で既知の過去だけを見る。**

    ラベル側の同時刻履歴は `(t + h) − 1 日` の実績で、これも `t` より前にある。
    引くのは**特徴量の as-of**（`fetched_at` で切ったもの）で、必要より厳しいが
    リークは起こさない。
    """
    day = grid.day_slice()
    back = grid.steps_per(SAME_TIME_MINUTES)
    shift = grid.steps_per(picked.horizon_min)
    past = slice(day.start + shift - back, day.stop + shift - back)
    bikes = _take(pre.same_time_bikes, picked).astype(np.int16)
    absent = ~_take(pre.same_time_valid, picked)
    label_absent = ~_take(state.valid[:, past], picked)
    past_bikes = _take(state.bikes[:, past], picked)
    past_docks = _take(state.docks[:, past], picked)
    past_flags = _take(state.flags[:, past], picked)
    return {
        "bikes_same_time_1d": _col(np.where(absent, 0, bikes), "bikes_same_time_1d", null=absent),
        "y_bike_same_time_1d": _col(
            labels.y_bike(past_bikes, past_flags), "y_bike_same_time_1d", null=label_absent
        ),
        "y_dock_same_time_1d": _col(
            labels.y_dock(past_docks, past_flags), "y_dock_same_time_1d", null=label_absent
        ),
    }


def _neighbor_columns(pre: Precomputed, picked: Picked) -> dict[str, pa.Array]:
    """近傍。**合計と個数は 0、平均は NULL**（W3 プラン §9.5）。"""
    columns = {name: _col(_take(matrix, picked), name) for name, matrix in pre.neighbor.items()}
    columns.update(_float_column("nb_fill_ratio_mean_500m", _take(pre.neighbor_fill_ratio, picked)))
    return columns


# ── まとめ ────────────────────────────────────────────────────
def _concat(parts: Sequence[pa.Table]) -> pa.Table:
    """水平ごとの表をつなぎ、**決定的な順**に並べる。

    並べ替えは読みやすさのためだけでなく、**2 回作ったときにバイト列が一致する**
    ことを、行の作り方に依らずに保証するためでもある（W3 プラン §7.6）。
    """
    if not parts:
        return SCHEMA.empty_table()
    joined = pa.concat_tables(parts)
    return joined.sort_by([("station_id", "ascending"), ("t", "ascending"), ("h_min", "ascending")])


def _stats(inputs: DayInputs, table: pa.Table, counts: dict[str, int], total: int) -> DayStats:
    """その日の内訳をまとめる。"""
    weights = np.asarray(table.column("weight").to_numpy(zero_copy_only=False), dtype=np.float64)
    return DayStats(
        date=inputs.day.isoformat(),
        feature_set=FEATURE_SET,
        stations=len(inputs.reference.facts),
        pairs_total=total,
        pairs_kept=total - sum(counts.values()),
        rows=int(table.num_rows),
        weight_sum=float(weights.sum()),
        excluded=counts,
    )
