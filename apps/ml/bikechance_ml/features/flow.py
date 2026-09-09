"""流量（貸出・返却・活動量）を**フィード本来の周期**で計算する（W3-11）。

**水準と同じ関数でやってはいけない。** 5 分グリッドに畳んでから差分を取ると、
ドコモ（81 秒周期）では**流量の 13% が消える**（開発プラン §6.7 の実測。22,704 →
19,741 台）。毎分収集がデータ量を増やしているのはドコモだけで、その増分はここでしか
効かない。

だから計算は観測 1 つ 1 つの上で行い、**結果をグリッドに写す**（`asof.py` が返す
行番号でそのまま引ける）。窓は「その観測時刻までの 60 分」で、グリッド点ではなく
**as-of した観測の時刻**が終端になる。

`-1`（観測されなかった）の行は差分の計算から外す。前後の観測どうしで差を取るので、
穴が空いた区間の差分は「その穴をまたいだ変化量」になる。**補間はしない**
（データ辞書 §10.1）。

**`minutes_since_last_change` には上限がある**（`CHANGE_CAP_MINUTES`）。この列だけが
読んだ窓の長さで値が変わるので、学習（25 時間）と推論（3 時間）で必ず食い違う。
上限を決めて、どちらも同じ値を出せるようにしてある（W4 プラン §4 の W4-10）。
"""

from dataclasses import dataclass
from typing import Final

import numpy as np

from bikechance_ml.features.arrays import Float32, Int32, Int64, Span
from bikechance_ml.features.asof import Observations
from bikechance_ml.features.constants import CHANGE_CAP_MINUTES, FLOW_MINUTES, MISSING

_MS_PER_MINUTE: Final[int] = 60_000


@dataclass(frozen=True)
class Flow:
    """観測 1 行ごとの流量。`Observations` の行と 1 対 1 に並ぶ。

    `minutes_since_last_change` は **`CHANGE_CAP_MINUTES`（180 分）で頭打ち**にする。
    変化が見えなければ上限そのもの、観測が 2 つ未満なら NaN（**分からない**）。
    **0 で埋めない**（「たった今変わった」と区別がつかなくなる）。
    """

    rentals: Int32
    returns: Int32
    n_changes: Int32
    minutes_since_last_change: Float32


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
    """最後に台数が変わってからの分。**`CHANGE_CAP_MINUTES` で頭打ちにする。**

    上限を入れるのは、**この列だけが読んだ窓の長さで値が変わる**ためである。学習は
    25 時間さかのぼって「420 分前に動いた」と言えるが、5 分毎に走る推論は同じ深さを
    読めない。上限を決めておけば、**窓が上限より長いかぎり両方が同じ値を出す**
    （W4 プラン §4 の W4-10）。

    **変化が見えなければ上限そのもの**を返す（NaN にしない）。「180 分以上動いて
    いない」は分かっている情報で、捨てる理由が無い。
    """
    cap = float(CHANGE_CAP_MINUTES)
    if len(change_times) == 0:
        return np.full(len(times), cap, dtype=np.float32)
    taken = np.searchsorted(change_times, times, side="right")
    elapsed = np.where(
        taken > 0, (times - change_times[np.maximum(taken - 1, 0)]) / _MS_PER_MINUTE, cap
    )
    return np.asarray(np.minimum(elapsed, cap), dtype=np.float32)
