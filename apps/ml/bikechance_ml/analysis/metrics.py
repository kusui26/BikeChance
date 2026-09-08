"""EDA #1 の集計（純粋）。W2 プラン §5.9。

**入力は学習が実際に読む Parquet の表**（`gbfs-parquet`）。Postgres ではなく Parquet を
読むのは、学習と同じ経路を通してアーカイブそのものも検証するためである。

副作用は持たない。すべての関数は pyarrow の表を受け取って集計結果の値を返す。
グラフは描かない（画像の依存を増やさない）。数字と表で判断できる形にする。

**`-1` は「観測されなかった」を表す。集計する前に必ず落とす**（データ辞書 §2 の (3)）。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import pyarrow as pa
import pyarrow.compute as pc

from bikechance_ml.jobs.snapshot_table import MISSING

#: JST は UTC より 9 時間進んでいる。日内変動は JST で見る（利用者の時間感覚）。
JST_OFFSET_H: Final[int] = 9

#: 5 分グリッド（開発プラン §6.1 の基準時刻）。
GRID_MINUTES: Final[int] = 5

#: 再配置とみなす閾値（開発プラン §6.5）。`max(FLOOR, RATIO × 容量)`。
REBALANCE_FLOOR: Final[int] = 4
REBALANCE_RATIO: Final[float] = 0.5


def observed(table: pa.Table) -> pa.Table:
    """観測された行だけを残す。**すべての集計の入口。**"""
    return table.filter(pc.greater_equal(table.column("bikes"), 0))


@dataclass(frozen=True)
class Availability:
    """(1) 分布：借りられない・返せないがどれだけ起きているか。"""

    n_rows: int
    bikes_zero_pct: float
    docks_zero_pct: float
    both_zero_pct: float
    bikes_mean: float
    docks_mean: float
    bikes_max: int
    docks_max: int
    suspended_pct: float


def _pct(part: int, whole: int) -> float:
    return 0.0 if whole == 0 else round(100.0 * part / whole, 3)


def _count_where(mask: pa.ChunkedArray) -> int:
    return int(pc.sum(pc.cast(mask, pa.int64())).as_py() or 0)


def availability(table: pa.Table) -> Availability:
    """観測された行に対する在庫の分布。`flags = 1`（運用停止）の割合も出す。"""
    seen = observed(table)
    n = seen.num_rows
    bikes = seen.column("bikes")
    docks = seen.column("docks")
    return Availability(
        n_rows=n,
        bikes_zero_pct=_pct(_count_where(pc.equal(bikes, 0)), n),
        docks_zero_pct=_pct(_count_where(pc.equal(docks, 0)), n),
        both_zero_pct=_pct(_count_where(pc.and_(pc.equal(bikes, 0), pc.equal(docks, 0))), n),
        bikes_mean=round(float(pc.mean(bikes).as_py() or 0.0), 3),
        docks_mean=round(float(pc.mean(docks).as_py() or 0.0), 3),
        bikes_max=int(pc.max(bikes).as_py() or 0),
        docks_max=int(pc.max(docks).as_py() or 0),
        suspended_pct=_pct(_count_where(pc.equal(seen.column("flags"), 1)), n),
    )


@dataclass(frozen=True)
class Missingness:
    """(4) 欠損：除外規則がどれだけの行に効くか。"""

    n_rows: int
    missing_pct: float
    n_snapshots: int
    n_stations: int
    #: スナップショットごとの行数が揃っていない＝台帳が増えた回数
    n_array_lengths: int


def missingness(table: pa.Table) -> Missingness:
    """`-1` の割合と、配列長（＝行数）が変わった回数。

    **`idx >= array_length` の行は Parquet に入っていない**（データ辞書 §7.3）ので、
    ここで数えられるのは `-1` だけである。台帳の増加は「スナップショットごとの行数の
    種類数」で見る。
    """
    n = table.num_rows
    per_snapshot = table.group_by("observed_at").aggregate([("station_id", "count")])
    lengths = set(per_snapshot.column("station_id_count").to_pylist())
    return Missingness(
        n_rows=n,
        missing_pct=_pct(_count_where(pc.equal(table.column("bikes"), MISSING)), n),
        n_snapshots=per_snapshot.num_rows,
        n_stations=len(set(table.column("station_id").to_pylist())),
        n_array_lengths=len(lengths),
    )


@dataclass(frozen=True)
class HourlyRow:
    """(2) 日内変動：JST の時台ごと。"""

    hour_jst: int
    n_rows: int
    #: その時台に含まれる暦日の数。**1 と 2 が混ざると日内変動と日差が混ざる**
    n_days: int
    bikes_zero_pct: float
    docks_zero_pct: float
    bikes_mean: float
    #: その時台に起きた台数の動きの平均（|Δbikes|）。**水準ではなく変化**を見る
    mean_abs_delta: float


def _with_jst_hour(table: pa.Table) -> pa.Table:
    """JST の時台の列を足す。UTC のまま取ると 9 時間ずれる。"""
    jst = pc.add(table.column("observed_at"), pa.scalar(JST_OFFSET_H * 3600_000, pa.duration("ms")))
    return table.append_column("hour_jst", pc.hour(jst))


def _with_zero_flags(table: pa.Table) -> pa.Table:
    """ゼロ率を平均で出せるよう、0/1 の列を足す。"""
    return table.append_column(
        "bikes_is_zero", pc.cast(pc.equal(table.column("bikes"), 0), pa.int8())
    ).append_column("docks_is_zero", pc.cast(pc.equal(table.column("docks"), 0), pa.int8()))


def hourly(table: pa.Table) -> tuple[HourlyRow, ...]:
    """JST の時台ごとの在庫。`minute_of_day` を特徴量にする根拠を見る。"""
    plain = observed(table)
    if plain.num_rows == 0:
        return ()
    seen = _with_zero_flags(_with_jst_hour(plain))
    dated = seen.append_column(
        "day_jst",
        pc.strftime(
            pc.add(
                seen.column("observed_at"), pa.scalar(JST_OFFSET_H * 3600_000, pa.duration("ms"))
            ),
            format="%Y-%m-%d",
        ),
    )
    days = {
        int(row["hour_jst"]): int(row["day_jst_count_distinct"])
        for row in dated.group_by("hour_jst").aggregate([("day_jst", "count_distinct")]).to_pylist()
    }
    grouped = seen.group_by("hour_jst").aggregate(
        [
            ("bikes", "count"),
            ("bikes", "mean"),
            ("bikes_is_zero", "mean"),
            ("docks_is_zero", "mean"),
        ]
    )
    moves = _mean_abs_delta_by_hour(plain)
    return tuple(
        HourlyRow(
            hour_jst=int(row["hour_jst"]),
            n_rows=int(row["bikes_count"]),
            n_days=days.get(int(row["hour_jst"]), 0),
            bikes_zero_pct=round(100.0 * float(row["bikes_is_zero_mean"]), 2),
            docks_zero_pct=round(100.0 * float(row["docks_is_zero_mean"]), 2),
            bikes_mean=round(float(row["bikes_mean"]), 3),
            mean_abs_delta=moves.get(int(row["hour_jst"]), 0.0),
        )
        for row in sorted(grouped.to_pylist(), key=lambda row: int(row["hour_jst"]))
    )


def _mean_abs_delta_by_hour(seen: pa.Table) -> dict[int, float]:
    """時台ごとの `|Δbikes|` の平均。**ポートの境目をまたいだ差は数えない。**

    水準（何台あるか）と変化（どれだけ動いたか）は別の話で、`minute_of_day` が効くのは
    ふつう後者である。朝夕に動きが集中していれば、時刻の特徴量に意味がある。
    """
    ordered = sorted_by_station(seen)
    delta = pc.abs(_within_station_delta(ordered))
    with_hour = _with_jst_hour(ordered).append_column("abs_delta", delta)
    grouped = with_hour.group_by("hour_jst").aggregate([("abs_delta", "mean")])
    return {
        int(row["hour_jst"]): round(float(row["abs_delta_mean"] or 0.0), 4)
        for row in grouped.to_pylist()
    }


# ── ポート単位の時系列 ────────────────────────────────────────────
# `station_id, observed_at` で並べた表に対して、隣り合う行の差を取る。
# **ポートの境目をまたいだ差を捨てる**のが要点で、これを忘れると別のポートの
# 台数との差を「変化」として数えてしまう。


def sorted_by_station(table: pa.Table) -> pa.Table:
    """`station_id, observed_at` の順に並べ替える。差分を取る前提。"""
    return table.sort_by([("station_id", "ascending"), ("observed_at", "ascending")])


def _within_station_delta(table: pa.Table) -> pa.Array:
    """隣り合う行の `bikes` の差。**ポートの境目は null** にする。

    表は `station_id, observed_at` 順に並んでいる前提。`pairwise_diff` は先頭を null に
    するので、境目の判定と合わせて「前の行が同じポートでない位置」がすべて落ちる。
    **これを忘れると、別のポートの台数との差を「変化」として数えてしまう。**
    """
    n = table.num_rows
    delta = pc.pairwise_diff(table.column("bikes").combine_chunks())
    if n == 0:
        return delta
    ids = table.column("station_id").combine_chunks()
    # 1 行目は常に境目。2 行目以降は「前の行と station_id が同じか」で判定する
    same = pa.concat_arrays(
        [pa.array([False], type=pa.bool_()), pc.equal(ids.slice(1), ids.slice(0, n - 1))]
    )
    return pc.if_else(same, delta, pa.nulls(n, type=delta.type))


@dataclass(frozen=True)
class StationSpread:
    """(3) ポート差：ポート別プロファイルが要るかを見る。"""

    n_stations: int
    zero_rate_p10: float
    zero_rate_p50: float
    zero_rate_p90: float
    n_always_empty: int
    n_never_empty: int
    n_never_changed: int
    moves_p50: float
    moves_p90: float
    moves_max: int


def _percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return round(ordered[index], 3)


def station_spread(table: pa.Table) -> StationSpread:
    """ポートごとのゼロ率と変化回数の広がり。

    「どのポートも同じように振る舞う」なら共通モデルで足りる。**広がりが大きいほど
    ポート別プロファイル（開発プラン §6.4）の価値が高い。**
    """
    seen = _with_zero_flags(observed(table))
    if seen.num_rows == 0:
        return StationSpread(0, 0.0, 0.0, 0.0, 0, 0, 0, 0.0, 0.0, 0)
    ordered = sorted_by_station(seen)
    moved = pc.not_equal(_within_station_delta(ordered), 0)
    with_move = ordered.append_column("moved", pc.cast(pc.fill_null(moved, False), pa.int8()))
    per_station = with_move.group_by("station_id").aggregate(
        [("bikes_is_zero", "mean"), ("moved", "sum"), ("bikes", "count")]
    )
    rows = per_station.to_pylist()
    zero_rates = [100.0 * float(row["bikes_is_zero_mean"]) for row in rows]
    moves = [int(row["moved_sum"] or 0) for row in rows]
    return StationSpread(
        n_stations=len(rows),
        zero_rate_p10=_percentile(zero_rates, 0.10),
        zero_rate_p50=_percentile(zero_rates, 0.50),
        zero_rate_p90=_percentile(zero_rates, 0.90),
        n_always_empty=sum(1 for rate in zero_rates if rate >= 99.999),
        n_never_empty=sum(1 for rate in zero_rates if rate <= 0.001),
        n_never_changed=sum(1 for count in moves if count == 0),
        moves_p50=_percentile([float(m) for m in moves], 0.50),
        moves_p90=_percentile([float(m) for m in moves], 0.90),
        moves_max=max(moves) if moves else 0,
    )


@dataclass(frozen=True)
class FlowLoss:
    """(5) 流量：5 分グリッドに畳むと何が失われるか。"""

    n_snapshots_native: int
    n_snapshots_grid: int
    total_abs_delta_native: int
    total_abs_delta_grid: int
    #: グリッドが取りこぼした割合。0 なら失うものが無い
    lost_pct: float


def _grid_timestamps(table: pa.Table, minutes: int) -> set[int]:
    """各グリッド点の直前（as-of）にあたる観測時刻を選ぶ。

    グリッドは「その時刻以前の最新スナップショット」で状態を決める（開発プラン §6.1）。
    同じスナップショットが複数のグリッド点に選ばれることもあるので集合で持つ。
    """
    stamps = sorted({int(value.value) for value in table.column("observed_at").unique()})
    if not stamps:
        return set()
    step_ms = minutes * 60_000
    chosen: set[int] = set()
    cursor = 0
    start = (stamps[0] // step_ms) * step_ms
    for point in range(start, stamps[-1] + step_ms, step_ms):
        while cursor + 1 < len(stamps) and stamps[cursor + 1] <= point:
            cursor += 1
        if stamps[cursor] <= point:
            chosen.add(stamps[cursor])
    return chosen


def _total_abs_delta(table: pa.Table) -> int:
    ordered = sorted_by_station(table)
    delta = _within_station_delta(ordered)
    return int(pc.sum(pc.abs(delta)).as_py() or 0)


def flow_loss(table: pa.Table, minutes: int = GRID_MINUTES) -> FlowLoss:
    """本来の周期と 5 分グリッドで、台数の動きの総和を比べる。

    **毎分収集の価値がここに出る。** 5 分の内側で行って戻った動きはグリッドでは消える。
    HELLO は元々 5 分周期なので差は出ないはずで、ドコモ（81 秒）で差が出る。
    """
    seen = observed(table)
    native_total = _total_abs_delta(seen)
    keep = _grid_timestamps(seen, minutes)
    if not keep:
        return FlowLoss(0, 0, 0, 0, 0.0)
    stamps = pa.array(sorted(keep), type=pa.timestamp("ms", tz="UTC"))
    on_grid = seen.filter(pc.is_in(seen.column("observed_at"), value_set=stamps))
    grid_total = _total_abs_delta(on_grid)
    return FlowLoss(
        n_snapshots_native=len(seen.column("observed_at").unique()),
        n_snapshots_grid=len(keep),
        total_abs_delta_native=native_total,
        total_abs_delta_grid=grid_total,
        lost_pct=_pct(native_total - grid_total, native_total),
    )


@dataclass(frozen=True)
class Rebalancing:
    """(6) 再配置：検知規則が妥当かを見る。"""

    threshold_floor: int
    n_events: int
    n_stations_with_event: int
    events_per_hour: float
    #: JST の時台ごとの件数（0〜23）
    by_hour_jst: tuple[int, ...]
    largest_move: int


def rebalancing(table: pa.Table, capacity: dict[str, int] | None = None) -> Rebalancing:
    """`|Δbikes| >= max(4, 0.5 × 容量)` を満たす変化を数える。

    容量を渡さなければ床（4 台）だけで判定する。**ドコモの `capacity` は動的値なので
    渡してはいけない**（データ辞書 §4.3）。容量の推定が要るなら
    「期間内に観測した `bikes + docks` の最大」を使う。
    """
    seen = observed(table)
    ordered = sorted_by_station(seen)
    delta = _within_station_delta(ordered)
    size = pc.abs(delta)
    ids = ordered.column("station_id").to_pylist()
    sizes = size.to_pylist()
    hours = _with_jst_hour(ordered).column("hour_jst").to_pylist()

    thresholds = _thresholds(ids, capacity)
    hits = [
        (station_id, magnitude, hour)
        for station_id, magnitude, hour, limit in zip(ids, sizes, hours, thresholds, strict=True)
        if magnitude is not None and magnitude >= limit
    ]
    by_hour = [0] * 24
    for _, _, hour in hits:
        by_hour[int(hour)] += 1
    span_h = _span_hours(seen)
    return Rebalancing(
        threshold_floor=REBALANCE_FLOOR,
        n_events=len(hits),
        n_stations_with_event=len({station_id for station_id, _, _ in hits}),
        events_per_hour=round(len(hits) / span_h, 2) if span_h > 0 else 0.0,
        by_hour_jst=tuple(by_hour),
        largest_move=max((magnitude for _, magnitude, _ in hits), default=0),
    )


def _thresholds(ids: Sequence[str], capacity: dict[str, int] | None) -> list[float]:
    if capacity is None:
        return [float(REBALANCE_FLOOR)] * len(ids)
    return [
        max(float(REBALANCE_FLOOR), REBALANCE_RATIO * capacity.get(station_id, 0))
        for station_id in ids
    ]


def _span_hours(table: pa.Table) -> float:
    """観測の張る時間（時）。件数を「1 時間あたり」に直すために使う。"""
    if table.num_rows == 0:
        return 0.0
    stamps = table.column("observed_at")
    first: datetime = pc.min(stamps).as_py()
    last: datetime = pc.max(stamps).as_py()
    return float((last - first).total_seconds()) / 3600.0


def observed_capacity(table: pa.Table) -> dict[str, int]:
    """期間内に観測した `bikes + docks` の最大。**動的容量の推定**（開発プラン §6.7）。"""
    seen = observed(table)
    total = pc.add(seen.column("bikes"), seen.column("docks"))
    grouped = (
        seen.append_column("total", total).group_by("station_id").aggregate([("total", "max")])
    )
    return {row["station_id"]: int(row["total_max"]) for row in grouped.to_pylist()}


#: これを超える返却枠は現実的でない。実測でドコモの「【監視】メンテナンスポート」が
#: `docks = 9997` を返しており、**実在しない監視用のポート**だった（EDA #1）。
#: HELLO の正当な最大は 1000（広場など 19 ポートが宣言）なので、間に十分な隔たりがある。
IMPLAUSIBLE_DOCKS: Final[int] = 2000


@dataclass(frozen=True)
class Extreme:
    """値が極端なポート。**実在しないポートを見つけるため**に見る。"""

    station_id: str
    max_bikes: int
    max_docks: int
    n_observed: int
    implausible: bool


def extremes(table: pa.Table, limit: int = 5) -> tuple[Extreme, ...]:
    """`docks` の最大が大きい順にポートを返す。

    センチネル（事業者が監視用に置いている実在しないポート）は、桁の違う値で自分を
    名乗ることがある。**名前で弾いてはいけない**（実測で「バプテスト教会」が
    「テスト」に一致した）。値の妥当性で見るほうが安全である。
    """
    seen = observed(table)
    if seen.num_rows == 0:
        return ()
    grouped = seen.group_by("station_id").aggregate(
        [("bikes", "max"), ("docks", "max"), ("bikes", "count")]
    )
    rows = sorted(grouped.to_pylist(), key=lambda row: int(row["docks_max"]), reverse=True)
    return tuple(
        Extreme(
            station_id=row["station_id"],
            max_bikes=int(row["bikes_max"]),
            max_docks=int(row["docks_max"]),
            n_observed=int(row["bikes_count"]),
            implausible=int(row["docks_max"]) >= IMPLAUSIBLE_DOCKS,
        )
        for row in rows[:limit]
    )
