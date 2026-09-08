"""EDA #1 の集計（`bikechance_ml/analysis/metrics.py`）。

**この数字は判断の材料になる**ので、手で計算できる小さな表で 1 つずつ突き合わせる。
とくに気をつけたいのは 3 つ。
  * `-1`（未観測）を数に混ぜていないか
  * **ポートの境目をまたいだ差**を「変化」として数えていないか
  * 時台が JST になっているか（UTC のままだと 9 時間ずれる）
"""

from datetime import UTC, datetime, timedelta

import pyarrow as pa

from bikechance_ml.analysis.metrics import (
    availability,
    extremes,
    flow_loss,
    hourly,
    missingness,
    observed,
    observed_capacity,
    rebalancing,
    station_spread,
)

BASE = datetime(2026, 9, 7, 15, 0, tzinfo=UTC)  # JST 2026-09-08 00:00


def table(rows: list[tuple[str, int, int, int, int]]) -> pa.Table:
    """`(station_id, 分, bikes, docks, flags)` から表を作る。"""
    return pa.table(
        {
            "system_id": pa.array(["t"] * len(rows), type=pa.string()),
            "station_id": pa.array([r[0] for r in rows], type=pa.string()),
            "observed_at": pa.array(
                [BASE + timedelta(minutes=r[1]) for r in rows],
                type=pa.timestamp("ms", tz="UTC"),
            ),
            "bikes": pa.array([r[2] for r in rows], type=pa.int16()),
            "docks": pa.array([r[3] for r in rows], type=pa.int16()),
            "flags": pa.array([r[4] for r in rows], type=pa.int16()),
            "reported_age_s": pa.array([0] * len(rows), type=pa.int16()),
        }
    )


# ── 未観測の除外 ─────────────────────────────────────────────
def test_observed_drops_missing_rows() -> None:
    t = table([("a", 0, 3, 1, 7), ("b", 0, -1, -1, -1)])
    assert observed(t).num_rows == 1


def test_availability_ignores_missing_rows() -> None:
    # -1 を混ぜても平均が下がらないこと（0 として数えていないこと）
    t = table([("a", 0, 4, 0, 7), ("b", 0, -1, -1, -1)])
    result = availability(t)
    assert result.n_rows == 1
    assert result.bikes_mean == 4.0
    assert result.bikes_zero_pct == 0.0
    assert result.docks_zero_pct == 100.0


def test_availability_counts_zero_and_suspended() -> None:
    t = table([("a", 0, 0, 5, 7), ("b", 0, 2, 0, 1), ("c", 0, 0, 0, 7), ("d", 0, 1, 1, 7)])
    result = availability(t)
    assert result.bikes_zero_pct == 50.0  # a と c
    assert result.docks_zero_pct == 50.0  # b と c
    assert result.both_zero_pct == 25.0  # c だけ
    assert result.suspended_pct == 25.0  # b だけ
    assert (result.bikes_max, result.docks_max) == (2, 5)


# ── 欠損 ─────────────────────────────────────────────────────
def test_missingness_counts_minus_one() -> None:
    t = table([("a", 0, 3, 1, 7), ("b", 0, -1, -1, -1), ("a", 5, 2, 2, 7), ("b", 5, 1, 1, 7)])
    result = missingness(t)
    assert result.n_rows == 4
    assert result.missing_pct == 25.0
    assert result.n_snapshots == 2
    assert result.n_stations == 2
    assert result.n_array_lengths == 1  # どちらのスナップショットも 2 行


def test_missingness_detects_ledger_growth() -> None:
    # 2 回目にポートが 1 つ増えた＝行数の種類が 2 つになる
    t = table([("a", 0, 1, 1, 7), ("a", 5, 1, 1, 7), ("b", 5, 1, 1, 7)])
    assert missingness(t).n_array_lengths == 2


# ── 日内変動 ─────────────────────────────────────────────────
def test_hourly_uses_jst_not_utc() -> None:
    # BASE は UTC 15:00 ＝ JST 翌 00:00。JST の 0 時に入るべき
    t = table([("a", 0, 2, 2, 7)])
    rows = hourly(t)
    assert len(rows) == 1
    assert rows[0].hour_jst == 0


def test_hourly_splits_by_hour_and_computes_rates() -> None:
    t = table(
        [
            ("a", 0, 0, 1, 7),  # JST 00 時
            ("b", 0, 2, 0, 7),  # JST 00 時
            ("a", 60, 1, 1, 7),  # JST 01 時
        ]
    )
    rows = hourly(t)
    assert [r.hour_jst for r in rows] == [0, 1]
    assert rows[0].n_rows == 2
    assert rows[0].bikes_zero_pct == 50.0
    assert rows[0].docks_zero_pct == 50.0
    assert rows[1].bikes_zero_pct == 0.0


def test_hourly_of_empty_table() -> None:
    assert hourly(table([])) == ()


# ── ポート差 ─────────────────────────────────────────────────
def test_station_spread_counts_extremes() -> None:
    t = table(
        [
            ("empty", 0, 0, 5, 7),
            ("empty", 5, 0, 5, 7),
            ("full", 0, 3, 0, 7),
            ("full", 5, 4, 0, 7),
        ]
    )
    result = station_spread(t)
    assert result.n_stations == 2
    assert result.n_always_empty == 1  # empty はずっと 0 台
    assert result.n_never_empty == 1  # full は一度も 0 台にならない
    assert result.n_never_changed == 1  # empty は動いていない
    assert result.moves_max == 1  # full が 1 回動いた


def test_station_spread_does_not_cross_station_boundaries() -> None:
    """**a の最後と b の最初の差**を変化として数えていないこと。"""
    t = table([("a", 0, 0, 1, 7), ("a", 5, 0, 1, 7), ("b", 0, 9, 1, 7), ("b", 5, 9, 1, 7)])
    result = station_spread(t)
    assert result.n_never_changed == 2  # どちらも動いていない
    assert result.moves_max == 0


# ── 流量 ─────────────────────────────────────────────────────
def test_flow_loss_is_zero_when_cadence_matches_grid() -> None:
    t = table([("a", 0, 0, 5, 7), ("a", 5, 2, 3, 7), ("a", 10, 1, 4, 7)])
    result = flow_loss(t)
    assert result.n_snapshots_native == result.n_snapshots_grid == 3
    assert result.total_abs_delta_native == result.total_abs_delta_grid == 3  # |2-0| + |1-2|
    assert result.lost_pct == 0.0


def test_flow_loss_catches_movement_hidden_inside_the_grid() -> None:
    """5 分の内側で行って戻った動きは、グリッドでは消える。"""
    t = table(
        [
            ("a", 0, 0, 5, 7),
            ("a", 1, 3, 2, 7),  # +3
            ("a", 2, 0, 5, 7),  # -3（グリッドからは見えない）
            ("a", 5, 0, 5, 7),
        ]
    )
    result = flow_loss(t)
    assert result.total_abs_delta_native == 6
    assert result.total_abs_delta_grid == 0
    assert result.lost_pct == 100.0
    assert result.n_snapshots_native == 4
    assert result.n_snapshots_grid == 2


# ── 再配置 ───────────────────────────────────────────────────
def test_rebalancing_uses_the_floor_without_capacity() -> None:
    t = table([("a", 0, 0, 9, 7), ("a", 5, 4, 5, 7), ("a", 10, 2, 7, 7)])
    result = rebalancing(t)
    assert result.n_events == 1  # +4 だけ（-2 は届かない）
    assert result.largest_move == 4
    assert result.n_stations_with_event == 1


def test_rebalancing_scales_with_capacity() -> None:
    # 容量 20 のポートは閾値が 10 になるので、+4 では検知しない
    t = table([("a", 0, 0, 20, 7), ("a", 5, 4, 16, 7)])
    assert rebalancing(t, capacity={"a": 20}).n_events == 0
    assert rebalancing(t, capacity={"a": 4}).n_events == 1


def test_rebalancing_records_the_jst_hour() -> None:
    t = table([("a", 0, 0, 9, 7), ("a", 5, 5, 4, 7)])
    result = rebalancing(t)
    assert result.by_hour_jst[0] == 1  # JST 00 時
    assert sum(result.by_hour_jst) == 1


def test_rebalancing_does_not_cross_station_boundaries() -> None:
    t = table([("a", 0, 0, 9, 7), ("b", 0, 9, 0, 7)])
    assert rebalancing(t).n_events == 0


# ── 容量の推定 ───────────────────────────────────────────────
def test_observed_capacity_takes_the_max_of_bikes_plus_docks() -> None:
    t = table([("a", 0, 1, 2, 7), ("a", 5, 4, 1, 7), ("b", 0, 0, 0, 7)])
    assert observed_capacity(t) == {"a": 5, "b": 0}


def test_observed_capacity_ignores_missing_rows() -> None:
    t = table([("a", 0, 3, 3, 7), ("a", 5, -1, -1, -1)])
    assert observed_capacity(t) == {"a": 6}


# ── 極端な値（実在しないポートの検出）─────────────────────────
def test_extremes_flags_implausible_docks() -> None:
    t = table([("normal", 0, 2, 8, 7), ("sentinel", 0, 2, 9997, 7)])
    rows = extremes(t)
    assert rows[0].station_id == "sentinel"
    assert rows[0].implausible is True
    assert rows[1].implausible is False


def test_extremes_accepts_large_but_real_capacity() -> None:
    """HELLO の広場（宣言容量 1000）は正当な値なので弾かない。"""
    t = table([("plaza", 0, 0, 1000, 7)])
    assert extremes(t)[0].implausible is False


def test_extremes_ignores_missing_rows() -> None:
    t = table([("a", 0, 1, 1, 7), ("a", 5, -1, -1, -1)])
    rows = extremes(t)
    assert len(rows) == 1
    assert rows[0].n_observed == 1


def test_extremes_of_an_empty_table() -> None:
    assert extremes(table([])) == ()


# ── 時台ごとの日数 ───────────────────────────────────────────
def test_hourly_counts_the_days_in_each_hour() -> None:
    """同じ時台が 2 日ぶん入っていることが分かるようにする。"""
    t = table([("a", 0, 1, 1, 7), ("a", 24 * 60, 1, 1, 7), ("a", 60, 1, 1, 7)])
    rows = {row.hour_jst: row for row in hourly(t)}
    assert rows[0].n_days == 2  # 0 時台は 2 日ぶん
    assert rows[1].n_days == 1  # 1 時台は 1 日ぶん


def test_hourly_reports_movement_not_just_level() -> None:
    """水準（何台あるか）と変化（どれだけ動いたか）を分けて出していること。"""
    t = table(
        [
            ("a", 0, 5, 0, 7),  # JST 00 時
            ("a", 30, 5, 0, 7),  # JST 00 時：動いていない
            ("a", 60, 0, 5, 7),  # JST 01 時：5 動いた
        ]
    )
    rows = {row.hour_jst: row for row in hourly(t)}
    assert rows[0].mean_abs_delta == 0.0  # 1 行目は境目で null、2 行目は 0
    assert rows[1].mean_abs_delta == 5.0


def test_hourly_movement_does_not_cross_station_boundaries() -> None:
    t = table([("a", 0, 0, 5, 7), ("b", 0, 9, 0, 7)])
    rows = hourly(t)
    assert rows[0].mean_abs_delta == 0.0
