"""近傍の集約（開発プラン §6.3 の「近傍」、W3 プラン §9.5）。

半径 500 m の組は `station_neighbors` にあり（片方向の行が両向き入っている）、
300 m は `distance_m` で絞る。**表を 2 つ持たない。**

**近傍 0 は欠損ではなく実データ。** 300 m で 27.8%、500 m で 10.2% のポートに
近傍が 1 つも無い（W3 プラン §10.2）。だから

  * 合計（`nb_bikes_sum` / `nb_docks_sum`）と個数は **0**
  * 平均（`nb_fill_ratio_mean`）は分母が 0 なので **NULL**

とする。ここを 0 で埋めると「周りが満杯」と「周りに何も無い」が同じ値になる。

**事業者を跨ぐ組も入っている。** HELLO とドコモは別事業者で利用者はふつう乗り換え
られないので、「全部」と「同一システムのみ」の 2 系統を作る（W3 プラン §4.3 の 22）。

観測されていない近傍（`bikes = -1`）は合計に足さない。**補間しない。**
"""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from bikechance_ml.features.arrays import Bools, Float32, Float64, Int16, Int32, Int64
from bikechance_ml.features.constants import MISSING
from bikechance_ml.features.reference import SystemReference

#: 一度に畳む基準時刻の数。近傍の組 × 基準時刻の中間行列を作るので、ここで頭を押さえる。
#: 106,438 組 × 48 点 × 4 バイト = 20 MB。
_GRID_CHUNK = 48


@dataclass(frozen=True)
class NeighborLinks:
    """近傍の組。**`source` の昇順に並んでいること**を前提にする。"""

    source: Int32
    target: Int32
    distance_m: Int16
    same_system: Bools
    n_stations: int

    def within(self, radius_m: int, *, same_system_only: bool) -> "NeighborLinks":
        """帯とシステムで絞る。並び順は保たれる。"""
        keep = np.asarray(self.distance_m <= radius_m, dtype=np.bool_)
        if same_system_only:
            keep = np.asarray(keep & self.same_system, dtype=np.bool_)
        return NeighborLinks(
            source=self.source[keep],
            target=self.target[keep],
            distance_m=self.distance_m[keep],
            same_system=self.same_system[keep],
            n_stations=self.n_stations,
        )

    def group_starts(self) -> Int64:
        """`source` ごとの区間の境界（長さ `n_stations + 1`）。"""
        return np.asarray(
            np.searchsorted(self.source, np.arange(self.n_stations + 1), side="left"),
            dtype=np.int64,
        )

    def counts(self) -> Int32:
        """ポート毎の近傍の数。**近傍 0 は 0 のまま。**"""
        starts = self.group_starts()
        return np.asarray(starts[1:] - starts[:-1], dtype=np.int32)


def to_links(systems: Sequence[SystemReference], keys: Sequence[tuple[str, str]]) -> NeighborLinks:
    """近傍の行を、ポートの位置の組に直す。**`source` の昇順に並べる。**

    相手が台帳に無い組は落とす（台帳が正）。**落とした件数は呼ぶ側では見えない**ので、
    ここで落ちるのは「日次ジョブが作った近傍表と台帳が食い違っている」ときだけである
    ことに注意する。どちらも同じ `rebuild_geo()` が同じトランザクションで作る。
    """
    position = {key: index for index, key in enumerate(keys)}
    found = [
        (position[(system.system_id, row.station_id)], target, row)
        for system in systems
        for row in system.neighbors
        for target in [position.get((row.nb_system_id, row.nb_station_id))]
        if target is not None and (system.system_id, row.station_id) in position
    ]
    found.sort(key=lambda one: (one[0], one[1]))
    return NeighborLinks(
        source=np.array([source for source, _, _ in found], dtype=np.int32),
        target=np.array([target for _, target, _ in found], dtype=np.int32),
        distance_m=np.array([row.distance_m for _, _, row in found], dtype=np.int16),
        same_system=np.array([row.same_system for _, _, row in found], dtype=np.bool_),
        n_stations=len(keys),
    )


def sum_over_neighbors(links: NeighborLinks, values: Int32) -> Int32:
    """近傍の値を足す。`values` は `(ポート, 基準時刻)`。近傍が無ければ 0。"""
    return _grouped(links, values, np.add)


def count_over_neighbors(links: NeighborLinks, hit: Bools) -> Int32:
    """条件に当たる近傍の数。`hit` は `(ポート, 基準時刻)` の真偽。"""
    return _grouped(links, hit.astype(np.int32), np.add)


def mean_over_neighbors(links: NeighborLinks, values: Float64) -> Float32:
    """近傍の平均。**値のある近傍が 1 つも無ければ NaN**（0 で埋めない）。

    `values` の NaN は「その近傍については分からない」を表し、分母から外す。
    """
    usable = np.asarray(~np.isnan(values), dtype=np.bool_)
    total = _grouped_float(links, np.where(usable, values, 0.0).astype(np.float64))
    denominator = _grouped(links, usable.astype(np.int32), np.add)
    mean = np.where(denominator > 0, total / np.maximum(denominator, 1), np.nan)
    return np.asarray(mean, dtype=np.float32)


def observed_or_zero(values: Int16) -> Int32:
    """観測されていない近傍を 0 として足せる形にする（`-1` を持ち込まない）。"""
    return np.asarray(np.where(values == MISSING, 0, values), dtype=np.int32)


def observed_nan(values: Int16) -> Float64:
    """観測されていない近傍を NaN にする。平均の分母から外すため。"""
    return np.asarray(np.where(values == MISSING, np.nan, values), dtype=np.float64)


def _grouped(links: NeighborLinks, values: Int32, operation: np.ufunc) -> Int32:
    """`source` ごとに畳む。**基準時刻を刻んで**中間行列を小さく保つ。"""
    starts = links.group_starts()
    n_grid = values.shape[1]
    result = np.zeros((links.n_stations, n_grid), dtype=np.int32)
    if len(links.source) == 0:
        return result
    for begin in range(0, n_grid, _GRID_CHUNK):
        end = min(begin + _GRID_CHUNK, n_grid)
        result[:, begin:end] = _reduce_groups(values[links.target, begin:end], starts, operation)
    return result


def _grouped_float(links: NeighborLinks, values: Float64) -> Float64:
    """浮動小数版。平均の分子に使う。"""
    starts = links.group_starts()
    n_grid = values.shape[1]
    result = np.zeros((links.n_stations, n_grid), dtype=np.float64)
    if len(links.source) == 0:
        return result
    for begin in range(0, n_grid, _GRID_CHUNK):
        end = min(begin + _GRID_CHUNK, n_grid)
        result[:, begin:end] = _reduce_groups(values[links.target, begin:end], starts, np.add)
    return result


def _reduce_groups(
    gathered: Int32 | Float64, starts: Int64, operation: np.ufunc
) -> Int32 | Float64:
    """区間ごとの畳み込み。**空の区間を 0 に直す**（`reduceat` の仕様の穴）。

    `np.add.reduceat` は `starts[i] >= starts[i+1]` のとき合計ではなく
    `a[starts[i]]` をそのまま返す。近傍 0 のポートは全体の 1〜3 割あるので、
    ここを直さないと「隣のポートの値」が紛れ込む。
    """
    n_pairs = gathered.shape[0]
    heads = np.minimum(starts[:-1], n_pairs - 1)
    folded = operation.reduceat(gathered, heads, axis=0)
    folded[starts[:-1] >= starts[1:]] = 0
    return folded
