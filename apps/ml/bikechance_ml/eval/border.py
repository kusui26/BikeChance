"""合成器の境目の差（W6 プラン §5.10、PR H の完了条件 3）。

**セルの境で確率が段になる。** 同じポート・同じ時刻の曲線で、水平 30 分が B3、45 分が
LightGBM なら、45 分の点で B3 の曲線から LightGBM の値へ跳ぶ（地図は水平のあいだを線形補間
するので、段はそのまま曲線に出る）。台数のバケツの境でも、台数が 1 台動いただけで行き先が
変わる。

**段の大きさは、その行で LightGBM が B3 からどれだけ離れているか**（|P_LGBM − P_B3|）で測る。
隣のセルは B3 を配るので、隣の点の値はほぼ B3 の値である。ここでは**森を歩いた行ぜんぶ**と、
その中で**隣のセルが B3 の行**（水平の隣・バケツの隣）を分けて、分布を出す。**表の外**
（最初の水平の手前・最後のバケツの先）は境にしない。
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Final

import numpy as np
import pyarrow as pa

from bikechance_ml.eval.dataset import TARGETS, bucket_index
from bikechance_ml.eval.gates import horizon_index
from bikechance_ml.features.arrays import Bools, Float64, Int16, Int64
from bikechance_ml.models import composite
from bikechance_ml.models.predictor import BaselinePredictor

#: 分布で出す分位点（中央・90%・99%）。最大は別に出す。
QUANTILES: Final[tuple[float, ...]] = (0.5, 0.9, 0.99)


@dataclass(frozen=True)
class Spread:
    """|P_LGBM − P_B3| の分布。**行が無ければ数は 0、値は None**（0 と書かない）。"""

    rows: int
    p50: float | None
    p90: float | None
    p99: float | None
    largest: float | None


def spread_of(values: Float64) -> Spread:
    if values.size == 0:
        return Spread(rows=0, p50=None, p90=None, p99=None, largest=None)
    p50, p90, p99 = (float(np.quantile(values, one)) for one in QUANTILES)
    return Spread(rows=int(values.size), p50=p50, p90=p90, p99=p99, largest=float(values.max()))


@dataclass(frozen=True)
class Border:
    """境目の差。**森を歩いた行ぜんぶ**と、**水平の隣・バケツの隣が B3 の行**。"""

    walked: Spread
    horizon_edge: Spread
    bucket_edge: Spread

    def as_json(self) -> dict[str, dict[str, object]]:
        return {
            "walked": asdict(self.walked),
            "horizon_edge": asdict(self.horizon_edge),
            "bucket_edge": asdict(self.bucket_edge),
        }


@dataclass(frozen=True)
class Gaps:
    """1 つの系統・ターゲットぶん：行ごとの |P_LGBM − P_B3| と、森の行・境の行。"""

    difference: Float64
    walked: Bools
    horizon_edge: Bools
    bucket_edge: Bools


def edges(found: Bools, horizon: Int64, bucket: Int64) -> tuple[Bools, Bools, Bools]:
    """森を歩く行と、その中で**隣のセルが B3** の行（水平の隣・バケツの隣）。

    `found` は `水平 × バケツ` の表（`composite.lookup`）。外側を真で埋めて引くので、
    **表の外は境にならない**。
    """
    padded = np.pad(found, 1, constant_values=True)
    h, b = horizon + 1, bucket + 1
    walked = np.asarray(padded[h, b], dtype=np.bool_)
    horizon_edge = walked & ~(padded[h - 1, b] & padded[h + 1, b])
    bucket_edge = walked & ~(padded[h, b - 1] & padded[h, b + 1])
    return walked, np.asarray(horizon_edge, dtype=np.bool_), np.asarray(bucket_edge, dtype=np.bool_)


def gaps_of(
    made: composite.CompositeArtifact, system_id: str, at: datetime, table: pa.Table
) -> list[Gaps]:
    """1 系統の表で、ターゲットごとの |P_LGBM − P_B3| と境の行。**配る形と同じ関数で出す。**"""
    mixed = composite.to_predictor(made).predict(system_id, at, table)
    b3 = BaselinePredictor(made.b3).predict(system_id, at, table)
    horizon = horizon_index(_int16(table, "h_min"))
    cells = made.lightgbm_cells()
    found: list[Gaps] = []
    for target in TARGETS:
        bucket = bucket_index(_int16(table, target.counts)).astype(np.int64)
        walked, h_edge, b_edge = edges(composite.lookup(cells, system_id, target), horizon, bucket)
        difference = np.abs(mixed.probability[target.name] - b3.probability[target.name])
        found.append(Gaps(np.asarray(difference, dtype=np.float64), walked, h_edge, b_edge))
    return found


def border_of(pieces: Sequence[Gaps]) -> Border:
    """系統・ターゲットごとの差を 1 つの分布にまとめる。"""
    return Border(
        walked=spread_of(_joined(pieces, lambda one: one.walked)),
        horizon_edge=spread_of(_joined(pieces, lambda one: one.horizon_edge)),
        bucket_edge=spread_of(_joined(pieces, lambda one: one.bucket_edge)),
    )


def _joined(pieces: Sequence[Gaps], pick: Callable[[Gaps], Bools]) -> Float64:
    parts = [one.difference[pick(one)] for one in pieces]
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float64)


def measure(
    made: composite.CompositeArtifact, at: datetime, tables: Mapping[str, pa.Table]
) -> Border:
    """系統ごとの表（学習サンプル 1 日ぶんなど）で、境目の差を測る。"""
    pieces = [
        one for system_id, table in tables.items() for one in gaps_of(made, system_id, at, table)
    ]
    return border_of(pieces)


def _int16(table: pa.Table, name: str) -> Int16:
    column = table.column(name).combine_chunks()
    return np.asarray(column.to_numpy(zero_copy_only=False), dtype=np.int16)
