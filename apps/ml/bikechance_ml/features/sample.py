"""層化抽出と逆抽出確率の重み（W3-14、W3 プラン §9.3）。

1 日の（ポート, 基準時刻, 水平）は **5,731 万ペア**（実測）で、全部は載らない。
一様 1% に加えて、**難所**（`bikes <= 2` または `docks <= 2`。実測 45.4%）を 4% で引く。

**2 回抽選しない。** 行ごとに一様乱数を 1 つ作り、難所なら閾値 4%、それ以外は 1% で
判定する。「一様に 1% 引いてから難所を 3% 足す」と独立抽選になり、包含確率が
`1 − 0.99 × 0.97 = 3.97%` になって重みが厳密でなくなる。1 回の判定なら重みは
ちょうど 25 と 100 になり、**重みの総和が母集団のペア数に一致する**（§7.6 の検査）。

**乱数は決定的なハッシュで作る。** `random.seed` に頼ると、並列化やポートの並び順で
変わる。種はポート毎に `(日付, system_id, station_id)` から作り（blake2b）、行ごとの
値は splitmix64 で `(基準時刻の番号, 水平の番号)` を混ぜて得る。**同じ日を 2 回作れば
同じ行が出る**（§7.6 の再現性）。

スカラ版と numpy 版の 2 つを持つ。速い側だけを持つと規則が読めなくなり、遅い側だけを
持つと 5,731 万回を回せない。**両者が一致することを `tests/test_features_sample.py` が
固定する。**
"""

import hashlib
from datetime import date
from typing import Final

import numpy as np

from bikechance_ml.features.arrays import Bools, Float32, Float64, Int16, UInt64
from bikechance_ml.features.constants import (
    HORIZONS_MIN,
    STRATUM_TIGHT,
    STRATUM_UNIFORM,
    TIGHT_RATE,
    TIGHT_THRESHOLD,
    UNIFORM_RATE,
)

_MASK: Final[int] = (1 << 64) - 1

#: splitmix64 の定数（Steele らの SplittableRandom）。**値を変えると抽出が変わる。**
_GAMMA: Final[int] = 0x9E3779B97F4A7C15
_MUL1: Final[int] = 0xBF58476D1CE4E5B9
_MUL2: Final[int] = 0x94D049BB133111EB

#: 上位 53 ビットだけを使って [0, 1) の倍精度にする。2^53 は倍精度で厳密に表せる。
_DROP_BITS: Final[int] = 11
_SCALE: Final[float] = float(1 << 53)


def station_seed(day: date, system_id: str, station_id: str) -> int:
    """ポート 1 つぶんの 64 ビットの種。**並び順にも並列化にも依らない。**"""
    material = f"{day.isoformat()}|{system_id}|{station_id}".encode()
    return int.from_bytes(hashlib.blake2b(material, digest_size=8).digest(), "big")


def counter(grid_index: int, horizon_index: int) -> int:
    """行を一意に決める通し番号。`(基準時刻, 水平)` の格子を 1 本に伸ばす。"""
    return grid_index * len(HORIZONS_MIN) + horizon_index


def uniform(seed: int, row_counter: int) -> float:
    """[0, 1) の一様乱数（スカラ版）。"""
    return float(_mix64(seed + row_counter) >> _DROP_BITS) / _SCALE


def uniform_array(seeds: UInt64, counters: UInt64) -> Float64:
    """[0, 1) の一様乱数（numpy 版）。`seeds` と `counters` は uint64 で放送できる形。"""
    mixed = _mix64_array(np.asarray(seeds + counters, dtype=np.uint64))
    return np.asarray((mixed >> np.uint64(_DROP_BITS)).astype(np.float64) / _SCALE)


def _mix64(state: int) -> int:
    z = (state + _GAMMA) & _MASK
    z = ((z ^ (z >> 30)) * _MUL1) & _MASK
    z = ((z ^ (z >> 27)) * _MUL2) & _MASK
    return (z ^ (z >> 31)) & _MASK


def _mix64_array(state: UInt64) -> UInt64:
    """numpy の uint64 は 2^64 で自然に巻き戻るので、マスクは要らない。

    シフト量まで `np.uint64` にするのは、Python の `int` を混ぜると numpy が
    float64 に昇格させて**静かに値が変わる**ため。
    """
    z = state + np.uint64(_GAMMA)
    z = (z ^ (z >> np.uint64(30))) * np.uint64(_MUL1)
    z = (z ^ (z >> np.uint64(27))) * np.uint64(_MUL2)
    return np.asarray(z ^ (z >> np.uint64(31)), dtype=np.uint64)


def is_tight(bikes: Int16, docks: Int16) -> Bools:
    """難所か。**`t` 時点の水準で決まる**ので、水平によらず同じ値になる。"""
    return np.asarray((bikes <= TIGHT_THRESHOLD) | (docks <= TIGHT_THRESHOLD), dtype=np.bool_)


def rate_of(tight: Bools) -> Float64:
    """層ごとの抽出率。"""
    return np.asarray(np.where(tight, TIGHT_RATE, UNIFORM_RATE), dtype=np.float64)


def weight_of(tight: Bools) -> Float32:
    """逆抽出確率の重み。`1 / 抽出率` なので 25 と 100 になる。"""
    return np.asarray(1.0 / rate_of(tight), dtype=np.float32)


def stratum_name(tight: bool) -> str:
    return STRATUM_TIGHT if tight else STRATUM_UNIFORM
