"""as-of 結合：基準時刻から見て「そのとき使えた観測」を引く（W3 プラン §9.3・W3-13）。

**規則が 2 つある。取り違えると学習が本番より賢くなる。**

  * **特徴量（水準）**：`fetched_at <= t` のうち `observed_at` が最大のもの。
    本番の推論が使えるのは**取り込み済み**のものだけだから。
  * **ラベル**：`observed_at <= t + h` のうち最大のもの。未来の話なので
    「入手できたか」は問わない。

`observed_at` で特徴量を切ると、本番ではまだ届いていないスナップショットで学習する
ことになる（train/serve skew）。HELLO は公開遅延の中央値が 67 秒・最大 226 秒で、
5 分グリッド点の約 24% がこれに当たる（開発プラン §6.2）。

**古さの判定はここでしない。** 返すのは「どの観測か」だけで、`600 秒より古ければ欠損」
の判断は `exclude.py` が持つ。`feed_delay_s` を特徴量として出すためでもある。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np
import numpy.typing as npt
import pyarrow as pa

from bikechance_ml.features.arrays import Int16, Int32, Int64, ScalarT, Span

#: 「該当する観測が無い」。行番号として使えない値にする。
NO_ROW: Final[int] = -1

_INDEX_DTYPE: Final[np.dtype[np.int32]] = np.dtype(np.int32)


@dataclass(frozen=True)
class Observations:
    """1 システムぶんの観測を、**ポート毎に連続した区間**として持つ。

    `starts[s]` から `starts[s + 1]` が `s` 番目のポートの行で、その中は
    `observed_at_ms` の昇順に並ぶ。区間が空のポート（その日に 1 度も現れなかった）も
    在ってよい。
    """

    starts: Int64
    observed_at_ms: Int64
    fetched_at_ms: Int64
    bikes: Int16
    docks: Int16
    flags: Int16
    reported_age_s: Int16

    @property
    def n_stations(self) -> int:
        return len(self.starts) - 1

    @property
    def n_rows(self) -> int:
        return len(self.observed_at_ms)

    def rows_of(self, station: int) -> Span:
        return slice(int(self.starts[station]), int(self.starts[station + 1]))


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


def as_of_feature(observations: Observations, grid_ms: Sequence[int]) -> Int32:
    """特徴量の as-of。`fetched_at <= t` のうち `observed_at` が最大の行。

    返すのは `(ポート, 基準時刻)` の行番号の行列で、該当が無ければ `NO_ROW`。
    """
    grid = np.asarray(grid_ms, dtype=np.int64)
    result = np.full((observations.n_stations, len(grid)), NO_ROW, dtype=_INDEX_DTYPE)
    for station in range(observations.n_stations):
        rows = observations.rows_of(station)
        if rows.stop == rows.start:
            continue
        result[station] = _latest_by_fetched(observations, rows, grid)
    return result


def _latest_by_fetched(observations: Observations, rows: Span, grid: Int64) -> Int32:
    """1 ポートぶん。**`fetched_at` の順に並べ直してから累積最大を取る。**

    取り込みの順と観測の順は必ずしも一致しない（再構築やバックアップ収集器が
    後から古い観測を足し得る）。「`fetched_at` が t 以下の中で `observed_at` が最大」を
    素直に得るには、`fetched_at` で並べた上で `observed_at` の累積最大を持つ。
    """
    fetched = observations.fetched_at_ms[rows]
    observed = observations.observed_at_ms[rows]
    order = np.argsort(fetched, kind="stable")
    best = np.maximum.accumulate(observed[order])
    is_new_max = observed[order] == best
    within = np.maximum.accumulate(np.where(is_new_max, np.arange(len(order)), 0))
    winner = order[within] + rows.start
    taken = np.searchsorted(fetched[order], grid, side="right")
    return np.where(taken > 0, winner[taken - 1], NO_ROW).astype(_INDEX_DTYPE)


def as_of_label(observations: Observations, grid_ms: Sequence[int]) -> Int32:
    """ラベルの as-of。`observed_at <= t` のうち最大の行。**`fetched_at` では切らない。**"""
    grid = np.asarray(grid_ms, dtype=np.int64)
    result = np.full((observations.n_stations, len(grid)), NO_ROW, dtype=_INDEX_DTYPE)
    for station in range(observations.n_stations):
        rows = observations.rows_of(station)
        if rows.stop == rows.start:
            continue
        taken = np.searchsorted(observations.observed_at_ms[rows], grid, side="right")
        result[station] = np.where(taken > 0, rows.start + taken - 1, NO_ROW)
    return result


def gather(values: npt.NDArray[ScalarT], index: Int32, missing: float) -> npt.NDArray[ScalarT]:
    """行番号の行列で値を引く。`NO_ROW` の位置には `missing` を置く。

    番兵を末尾に足してから引く。`np.where` で後始末をすると、`index = -1` が
    「最後の行」を指してしまう事故（Python の負の添字）を一度は起こしてしまうため。
    """
    padded = np.concatenate([values, np.array([missing], dtype=values.dtype)])
    safe = np.where(index == NO_ROW, len(values), index)
    return padded[safe]
