"""抽出と逆抽出確率の重み（W3-14、W3 プラン §9.3。**2026-09-16 に一様へ変えた**）。

1 日の（ポート, 基準時刻, 水平）は **5,875 万行**（実測）で、全部は載らない。
**一様に 1% を引く**（`SAMPLE_RATE`）。

**層化はやめた**（W5 プラン §6.9 の PR I）。「稀で重要な場面を濃く取る」のが目的
だったが、**難所は稀ではない**——母集団の 84.3% が `bikes <= 2 または docks <= 2` で、
4% と 1% の層化が同じ行数の一様抽出より多く取れる難所は **+13.3%** しかなかった。
理由と閾値ごとの数字は `constants.SAMPLE_RATE` に書いてある。

**引くのは行ごとに 1 回。** `(ポート, 基準時刻, 水平)` の 1 行につき乱数を 1 つ作り、
`SAMPLE_RATE` と比べる。**2 回抽選しない**——「一様に引いてから何かを足す」と独立抽選に
なり、包含確率が掛け算になって重みが厳密でなくなる。1 回なら重みはちょうど
`1 / SAMPLE_RATE` で、**重みの総和が母集団のペア数に一致する**（§7.6 の検査）。

**乱数は決定的なハッシュで作る。** `random.seed` に頼ると、並列化やポートの並び順で
変わる。種はポート毎に `(日付, system_id, station_id)` から作り（blake2b）、行ごとの
値は splitmix64 で `(基準時刻の番号, 水平の番号)` を混ぜて得る。**同じ日を 2 回作れば
同じ行が出る**（§7.6 の再現性）。**層をやめても種と通し番号は変えていない**ので、
2026-09-15 までの日をこの種で作り直せば、当時の抽出をそのまま再現できる。

スカラ版と numpy 版の 2 つを持つ。速い側だけを持つと規則が読めなくなり、遅い側だけを
持つと 5,875 万回を回せない。**両者が一致することを `tests/test_features_sample.py` が
固定する。**
"""

import hashlib
from datetime import date
from typing import Final

import numpy as np

from bikechance_ml.features.arrays import Bools, Float64, UInt64
from bikechance_ml.features.constants import HORIZONS_MIN, SAMPLE_RATE

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


def accepted(drawn: Float64) -> Bools:
    """引いた値が抽出率の内側か。**層は見ない**（一様に引く）。

    **抽出の規則はこの 1 行だけ。** 呼ぶ側に `< SAMPLE_RATE` を書かせると、
    率を変えたときに直し漏れる場所ができる。
    """
    return np.asarray(drawn < SAMPLE_RATE, dtype=np.bool_)


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
