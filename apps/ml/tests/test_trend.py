"""ふだんの傾向の純粋な部分（`features/trend.py`。W9 の PR B、開発プランの D-47）。

守りたいのは 7 つ。
  * **72 個の並びが読む側（TypeScript）と同じ**（`fixtures/trend/layout_golden.json`）
  * **日次の和から組んだ版は `profile.sum_dailies` と同じ和**（D-28。窓は 28 日、曜日種別ごと）
  * **1 時間の日数は 4 枠の最小**（格子点の無い枠は最小に入れない）と、**配る側の下限**
  * **校正の行は到着の日の曜日種別で引く**（引けない行・別の系統は落とす）
  * **校正は二項のまま当てはめても、0/1 に広げたのと同じ**（重みは点の数）
  * **門は ECE とポートの平均の両方で決まる**
  * **門を通らない組は NULL**、下限に届かない時間も NULL
"""

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Final

import numpy as np
import pyarrow as pa
import pytest

from bikechance_ml.eval import metrics
from bikechance_ml.features import profile, trend
from bikechance_ml.features.calendar import DOW_TYPE_ORDER

LAYOUT: Final[Path] = (
    Path(__file__).resolve().parents[3] / "fixtures" / "trend" / "layout_golden.json"
)
P: Final[date] = date(2026, 10, 31)  # 土曜
WEEKDAY: Final[date] = date(2026, 10, 28)  # 水曜
SYSTEM: Final[str] = "hellocycling"

#: (system_id, station_id, dow_type, slot15, n, n_bike_ok, n_dock_ok)
type Cell = tuple[str, str, str, int, int, int, int]


def daily_table(cells: list[Cell]) -> pa.Table:
    """`profile.DAILY_SCHEMA` の表を手で組む（数えない列は 0）。"""
    zeros = [0] * len(cells)
    columns: dict[str, list[object]] = {
        "system_id": [one[0] for one in cells],
        "station_id": [one[1] for one in cells],
        "dow_type": [one[2] for one in cells],
        "slot15": [one[3] for one in cells],
        "n": [one[4] for one in cells],
        "n_suspended": list(zeros),
        "n_bike_ok": [one[5] for one in cells],
        "n_dock_ok": [one[6] for one in cells],
        "sum_bikes": list(zeros),
        "sum_bikes_sq": list(zeros),
        "sum_rentals_60": list(zeros),
        "sum_returns_60": list(zeros),
    }
    return pa.table(columns, schema=profile.DAILY_SCHEMA)


def counts_of(tables: dict[date, pa.Table]) -> tuple[trend.Vocabulary, dict[date, trend.DayCounts]]:
    ports = trend.vocabulary(tables.values())
    return ports, {day: trend.day_counts(day, table, ports) for day, table in tables.items()}


def dow_of(day: date) -> str:
    """検査の日の曜日種別（祝日は入れない）。"""
    return {5: "sat", 6: "sun_holiday"}.get(day.weekday(), "weekday")


# ── 72 個の並び ───────────────────────────────────────────────
def test_layout_matches_golden() -> None:
    """**添字が TypeScript と同じ**（ずれても例外は出ず、別の曜日の確率が出る）。"""
    layout = json.loads(LAYOUT.read_text(encoding="utf-8"))
    assert list(DOW_TYPE_ORDER) == layout["dow_type_order"]
    assert layout["hours_per_day"] == trend.HOURS_PER_DAY
    assert len(DOW_TYPE_ORDER) * layout["hours_per_day"] == trend.CELLS
    for cell in layout["cells"]:
        assert trend.cell_index(cell["dow_type"], cell["hour"]) == cell["index"], cell


# ── 日次を密な配列に ─────────────────────────────────────────
def test_day_counts_places_cells_by_port_and_slot() -> None:
    table = daily_table(
        [(SYSTEM, "b", "weekday", 5, 3, 2, 1), ("docomo-cycle", "a", "weekday", 95, 2, 2, 0)]
    )
    ports, counts = counts_of({WEEKDAY: table})
    one = counts[WEEKDAY]
    # **台帳は system_id/station_id の昇順**（docomo が先）
    assert ports.ports == (("docomo-cycle", "a"), (SYSTEM, "b"))
    assert one.dow == DOW_TYPE_ORDER.index("weekday")
    assert (one.n[1, 5], one.ok["bike"][1, 5], one.ok["dock"][1, 5]) == (3, 2, 1)
    assert (one.n[0, 95], one.ok["bike"][0, 95]) == (2, 2)
    assert int(one.n.sum()) == 5


def test_day_counts_rejects_mixed_or_unknown_dow_type() -> None:
    mixed = daily_table([(SYSTEM, "a", "weekday", 0, 3, 3, 3), (SYSTEM, "b", "sat", 0, 3, 3, 3)])
    ports = trend.vocabulary([mixed])
    with pytest.raises(ValueError, match="dow_type"):
        trend.day_counts(WEEKDAY, mixed, ports)
    unknown = daily_table([(SYSTEM, "a", "holiday", 0, 3, 3, 3)])
    with pytest.raises(ValueError, match="dow_type"):
        trend.day_counts(WEEKDAY, unknown, trend.vocabulary([unknown]))


def test_day_counts_rejects_duplicate_cells() -> None:
    """**同じ (ポート, 枠) が 2 行ある日は止める**（密な配列では後の行が前を黙って上書きする）。"""
    doubled = daily_table(
        [(SYSTEM, "a", "weekday", 7, 3, 3, 3), (SYSTEM, "a", "weekday", 7, 2, 0, 0)]
    )
    with pytest.raises(ValueError, match="重なって"):
        trend.day_counts(WEEKDAY, doubled, trend.vocabulary([doubled]))


def test_day_counts_rejects_port_missing_from_vocabulary() -> None:
    known = daily_table([(SYSTEM, "a", "weekday", 0, 3, 3, 3)])
    other = daily_table([(SYSTEM, "z", "weekday", 0, 3, 3, 3)])
    with pytest.raises(ValueError, match="台帳"):
        trend.day_counts(WEEKDAY, other, trend.vocabulary([known]))


# ── 版（28 日の和）──────────────────────────────────────────
def window_tables(version: date, cells_of: dict[int, list[Cell]]) -> dict[date, pa.Table]:
    """`version` から何日前か → その日のセル。曜日種別はその日のもの。"""
    return {
        version - timedelta(days=back): daily_table(
            [
                (s, st, dow_of(version - timedelta(days=back)), slot, n, b, d)
                for s, st, _, slot, n, b, d in cells
            ]
        )
        for back, cells in cells_of.items()
    }


def test_hourly_profile_sums_only_the_28_day_window() -> None:
    """**28 日目までは入り、29 日目は入らない**（`window_days` と同じ窓）。"""
    cell: list[Cell] = [(SYSTEM, "a", "", 0, 3, 3, 0)]
    tables = window_tables(P, {0: cell, 27: cell, 28: cell})
    ports, counts = counts_of(tables)
    hourly = trend.hourly_profile(counts, P, len(ports))
    # P（土曜）と 27 日前（日曜）と 28 日前（土曜）。**28 日前は窓の外**
    assert hourly.n[0, trend.cell_index("sat", 0)] == 3
    assert hourly.n[0, trend.cell_index("sun_holiday", 0)] == 3
    assert hourly.n_days[0, trend.cell_index("sat", 0)] == 1


def test_hourly_profile_days_are_minimum_over_observed_slots() -> None:
    """**4 枠の日数の最小。格子点の無い枠は最小に入れない**（無い時間だけが 0）。"""
    # 0 時の枠 0 は 4 日、枠 1 は 3 日、枠 2・3 は観測なし。1 時は 1 つも無い
    tables = {
        WEEKDAY - timedelta(days=7 * week): daily_table(
            [(SYSTEM, "a", "weekday", 0, 3, 3, 3)]
            + ([(SYSTEM, "a", "weekday", 1, 3, 0, 3)] if week < 3 else [])
        )
        for week in range(4)
    }
    ports, counts = counts_of(tables)
    hourly = trend.hourly_profile(counts, WEEKDAY, len(ports))
    zero, one = trend.cell_index("weekday", 0), trend.cell_index("weekday", 1)
    assert hourly.n_days[0, zero] == 3
    assert hourly.n_days[0, one] == 0
    assert hourly.n[0, zero] == 4 * 3 + 3 * 3
    assert hourly.ok["bike"][0, zero] == 4 * 3


def test_hourly_profile_matches_sum_dailies() -> None:
    """**`profile.sum_dailies`（D-28 の作り直し）と同じ和**を、1 時間に畳んで突き合わせる。"""
    rng = np.random.default_rng(7)
    tables: dict[date, pa.Table] = {}
    for back in range(30):
        day = P - timedelta(days=back)
        cells: list[Cell] = []
        for station in ("a", "b", "c"):
            for slot in rng.choice(96, size=20, replace=False).tolist():
                n = int(rng.integers(1, 4))
                cells.append(
                    (
                        SYSTEM,
                        station,
                        dow_of(day),
                        int(slot),
                        n,
                        int(rng.integers(0, n + 1)),
                        int(rng.integers(0, n + 1)),
                    )
                )
        tables[day] = daily_table(cells)
    ports, counts = counts_of(tables)
    mine = trend.hourly_profile(counts, P, len(ports))
    summed = profile.sum_dailies(tables[day] for day in trend.window_days(P))
    names = [f"{s}/{st}" for s, st in ports.ports]
    row = np.asarray(
        [
            names.index(f"{s}/{st}")
            for s, st in zip(
                summed["system_id"].to_pylist(), summed["station_id"].to_pylist(), strict=True
            )
        ],
        dtype=np.int64,
    )
    cell = np.asarray(
        [
            DOW_TYPE_ORDER.index(k) * 24 + s // 4
            for k, s in zip(
                summed["dow_type"].to_pylist(), summed["slot15"].to_pylist(), strict=True
            )
        ],
        dtype=np.int64,
    )
    expected_n = np.zeros_like(mine.n)
    np.add.at(expected_n, (row, cell), summed["n"].to_numpy())
    expected_days = np.full(mine.n_days.shape, np.iinfo(np.int64).max)
    np.minimum.at(expected_days, (row, cell), summed["n_days"].to_numpy().astype(np.int64))
    expected_days = np.where(expected_days == np.iinfo(np.int64).max, 0, expected_days)
    np.testing.assert_array_equal(mine.n, expected_n)
    np.testing.assert_array_equal(mine.n_days, expected_days)


def test_rate_honours_serving_floor_by_dow_type() -> None:
    """**平日は 3 日、土日祝は 4 日**に届かない時間は NaN（`SERVE_DAYS`）。"""
    tables = {
        P - timedelta(days=back): daily_table(
            [(SYSTEM, "a", dow_of(P - timedelta(days=back)), 0, 3, 3, 0)]
        )
        for back in (0, 7, 14, 1, 2, 3, 4)  # 土曜 3 日、日曜 1 日、平日 3 日（金・木・水）
    }
    ports, counts = counts_of(tables)
    rate = trend.hourly_profile(counts, P, len(ports)).rate("bike")
    assert np.isnan(rate[0, trend.cell_index("sat", 0)])  # 3 日 < 4
    assert np.isnan(rate[0, trend.cell_index("sun_holiday", 0)])  # 1 日 < 4
    assert rate[0, trend.cell_index("weekday", 0)] == 1.0  # 3 日 ≥ 3


def test_port_rate_ignores_floor_and_uses_every_cell() -> None:
    table = daily_table(
        [(SYSTEM, "a", "weekday", 0, 3, 3, 0), (SYSTEM, "a", "weekday", 40, 3, 0, 3)]
    )
    ports, counts = counts_of({WEEKDAY: table})
    hourly = trend.hourly_profile(counts, WEEKDAY, len(ports))
    assert hourly.port_rate("bike")[0] == 0.5
    assert hourly.port_rate("dock")[0] == 0.5
    assert np.isnan(hourly.rate("bike")[0]).all()  # 1 日しか無いので 1 時間の率は出ない


# ── 窓 ──────────────────────────────────────────────────────
def test_windows_pairs_and_reads() -> None:
    windows = trend.TrendWindows(P)
    day = timedelta(days=1)
    assert windows.serve()[0] == (P - 6 * day, P - 8 * day)
    assert windows.serve()[-1] == (P, P - 2 * day)
    assert windows.gate_fit()[0] == (P - 13 * day, P - 15 * day)
    assert windows.gate_fit()[-1] == (P - 7 * day, P - 9 * day)
    # **門の測りは P−7 の版 1 つで、先の日 0〜6 日**（本番が P の版を 7 日に出すのと同じ形）
    assert {version for _, version in windows.gate_test()} == {P - 7 * day}
    assert [arrival for arrival, _ in windows.gate_test()] == [a for a, _ in windows.serve()]
    assert windows.versions() == (*(P - k * day for k in range(15, 1, -1)), P)
    assert windows.daily_days()[0] == P - 42 * day
    assert windows.daily_days()[-1] == P
    assert len(windows.daily_days()) == 43


# ── 校正の行 ─────────────────────────────────────────────────
def test_pair_rows_look_up_the_arrival_dow_type_and_drop_unusable() -> None:
    """**到着の日の曜日種別のセルを引く**。下限に届かないセル・別の系統は落とす。"""
    history = {
        WEEKDAY - timedelta(days=back): daily_table(
            [
                (SYSTEM, "a", dow_of(WEEKDAY - timedelta(days=back)), 8, 3, 2, 3),
                ("docomo-cycle", "x", dow_of(WEEKDAY - timedelta(days=back)), 8, 3, 3, 3),
            ]
        )
        for back in range(1, 15)
    }
    arrival_day = WEEKDAY + timedelta(days=1)
    tables = {
        **history,
        arrival_day: daily_table(
            [
                (SYSTEM, "a", "weekday", 8, 3, 1, 3),
                (SYSTEM, "a", "weekday", 9, 3, 3, 3),
                ("docomo-cycle", "x", "weekday", 8, 3, 0, 0),
            ]
        ),
    }
    ports, counts = counts_of(tables)
    version = trend.hourly_profile(counts, WEEKDAY, len(ports))
    inside = np.asarray([s == SYSTEM for s, _ in ports.ports], dtype=np.bool_)
    rows = trend.pair_rows(trend.outcome(counts[arrival_day]), version, "bike", inside)
    # 2 時（枠 8・9）の 1 行だけ。率は版の平日 2 時（2/3）、答えは (1 + 3) / 6
    assert len(rows) == 1
    assert rows.rate[0] == pytest.approx(2 / 3)
    assert rows.y[0] == pytest.approx(4 / 6)
    assert rows.weight[0] == 6
    assert not rows.weekend[0]
    assert rows.day[0] == arrival_day.toordinal()


# ── 校正 ────────────────────────────────────────────────────
def synthetic_rows(seed: int, size: int = 20_000) -> trend.CalibrationRows:
    """真の確率が「縮めた率」になる行（生の率は自信を持ちすぎ）。土日祝は縮め方が強い。"""
    rng = np.random.default_rng(seed)
    rate = rng.uniform(0.05, 0.95, size)
    port = np.clip(rate + rng.normal(0, 0.1, size), 0.05, 0.95)
    weekend = rng.random(size) < 0.3
    slope = np.where(weekend, 0.3, 0.7)
    truth = 1 / (1 + np.exp(-(0.2 + slope * trend.logit(rate) + 0.2 * trend.logit(port))))
    weight = rng.integers(1, 13, size).astype(np.float64)
    ok = rng.binomial(weight.astype(np.int64), truth)
    return trend.CalibrationRows(
        rate=rate,
        port_rate=port,
        weekend=weekend,
        y=ok / weight,
        weight=weight,
        day=np.full(size, P.toordinal(), dtype=np.int64),
    )


def test_fit_recovers_split_slopes() -> None:
    """**平日と土日祝で傾きを分ける**（土日祝の追加の傾きは負）。"""
    calibration = trend.fit_calibration(synthetic_rows(1, 200_000))
    weekday_slope = calibration.coefficients[1]
    weekend_slope = calibration.coefficients[1] + calibration.coefficients[4]
    assert weekday_slope == pytest.approx(0.7, abs=0.05)
    assert weekend_slope == pytest.approx(0.3, abs=0.05)


def test_binomial_fit_equals_expanded_fit() -> None:
    """**二項のまま当てはめても、0/1 に広げて当てはめても同じ係数**（重みは点の数）。"""
    rows = synthetic_rows(2)
    ok = rows.y * rows.weight
    wide = trend.CalibrationRows(
        rate=np.concatenate([rows.rate, rows.rate]),
        port_rate=np.concatenate([rows.port_rate, rows.port_rate]),
        weekend=np.concatenate([rows.weekend, rows.weekend]),
        y=np.concatenate([np.ones(len(rows)), np.zeros(len(rows))]),
        weight=np.concatenate([ok, rows.weight - ok]),
        day=np.concatenate([rows.day, rows.day]),
    )
    narrow, expanded = trend.fit_calibration(rows), trend.fit_calibration(wide)
    np.testing.assert_allclose(narrow.coefficients, expanded.coefficients, atol=1e-8)


def test_apply_keeps_nan_and_is_monotone_in_rate() -> None:
    calibration = trend.fit_calibration(synthetic_rows(3))
    rate = np.asarray([0.1, 0.3, 0.5, 0.7, 0.9, np.nan])
    port = np.full(len(rate), 0.6)
    p = calibration.apply(rate, port, np.zeros(len(rate), dtype=np.bool_))
    assert np.isnan(p[-1])
    assert np.all(np.diff(p[:-1]) >= 0)
    assert np.all((p[:-1] >= 0) & (p[:-1] <= 1))


def test_pav_is_weighted_and_monotone() -> None:
    values = np.asarray([0.2, 0.6, 0.4, 0.8, 0.1])
    weights = np.asarray([1.0, 1.0, 3.0, 1.0, 1.0])
    pooled = trend.pav(values, weights)
    assert len(pooled) == len(values)
    assert np.all(np.diff(pooled) >= 0)
    # 0.6 と 0.4（重み 3）は (0.6 + 1.2) / 4 = 0.45、0.8 と 0.1 は 0.45
    np.testing.assert_allclose(pooled, [0.2, 0.45, 0.45, 0.45, 0.45])


def test_isotonic_knots_are_monotone() -> None:
    rows = synthetic_rows(4)
    p = 1 / (1 + np.exp(-trend.logit(rows.rate)))
    x, y = trend.isotonic_knots(p, rows.y, rows.weight)
    assert len(x) == len(y) <= trend.ISOTONIC_BINS
    assert np.all(np.diff(x) >= 0)
    assert np.all(np.diff(y) >= 0)


# ── 門 ──────────────────────────────────────────────────────
def test_score_equals_metrics_on_expanded_points() -> None:
    """**物差しは短期の森と同じ**：0/1 に広げた格子点の上の `metrics.ece`・`metrics.brier`。"""
    rows = synthetic_rows(5, 5_000)
    p = np.clip(rows.rate, 0, 1)
    scores = trend.score(rows, p)
    ok = rows.y * rows.weight
    y = np.concatenate([np.ones(len(rows)), np.zeros(len(rows))]).astype(np.int8)
    w = np.concatenate([ok, rows.weight - ok]).astype(np.float32)
    keep = w > 0
    assert scores.ece == pytest.approx(metrics.ece(y[keep], np.concatenate([p, p])[keep], w[keep]))
    assert scores.brier == pytest.approx(
        metrics.brier(y[keep], np.concatenate([p, p])[keep], w[keep])
    )
    assert list(scores.ece_by_day) == [P]


def make_judged(ece: float, brier: float, brier_port: float) -> trend.Judged:
    calibration = trend.Calibration(coefficients=(0.0,) * 6, knots_x=(0.0, 1.0), knots_y=(0.0, 1.0))
    return trend.Judged(
        calibration=calibration,
        gate=trend.Scores(ece=ece, brier=brier, brier_port=brier_port, ece_by_day={}),
        rows_fit=1,
        rows_gate=1,
    )


def test_passes_needs_both_ece_and_port_mean() -> None:
    assert make_judged(0.029, 0.10, 0.11).passes is True
    assert make_judged(0.030, 0.10, 0.11).passes is False  # 上限は「未満」
    assert make_judged(0.010, 0.11, 0.11).passes is False  # ポートの平均と同じでは出さない


def test_judge_measures_the_shifted_fit_on_the_test_rows() -> None:
    judged = trend.judge(synthetic_rows(6), synthetic_rows(7), synthetic_rows(8))
    # 同じ作りの行どうしなら校正は効き、ポートの平均より良い
    assert judged.gate.ece < 0.02
    assert judged.gate.brier < judged.gate.brier_port
    assert judged.passes
    assert judged.describe()["passes"] is True


# ── 表の行 ──────────────────────────────────────────────────
def test_trend_rows_null_for_failing_groups_and_unusable_hours() -> None:
    tables = {
        WEEKDAY - timedelta(days=back): daily_table(
            [
                (SYSTEM, "a", dow_of(WEEKDAY - timedelta(days=back)), 8, 3, 3, 0),
                ("docomo-cycle", "x", dow_of(WEEKDAY - timedelta(days=back)), 8, 3, 3, 3),
            ]
        )
        for back in range(28)
    }
    ports, counts = counts_of(tables)
    hourly = trend.hourly_profile(counts, WEEKDAY, len(ports))
    identity = trend.Calibration(
        coefficients=(0.0, 1.0, 0.0, 0.0, 0.0, 0.0), knots_x=(0.0, 1.0), knots_y=(0.0, 1.0)
    )
    good = trend.Judged(identity, trend.Scores(0.01, 0.1, 0.2, {}), 1, 1)
    bad = trend.Judged(identity, trend.Scores(0.05, 0.1, 0.2, {}), 1, 1)
    judged = {(SYSTEM, "bike"): good, (SYSTEM, "dock"): good, ("docomo-cycle", "bike"): bad}
    rows = {
        (one["system_id"], one["station_id"]): one
        for one in trend.trend_rows(hourly, ports.ports, judged)
    }
    hello, docomo = rows[(SYSTEM, "a")], rows[("docomo-cycle", "x")]
    assert hello["profile_date"] == WEEKDAY.isoformat()
    bike = hello["p_bike_x1000"]
    assert isinstance(bike, list) and len(bike) == trend.CELLS
    # 平日 2 時は率 1.0 → logit の端（0.99）で 990。下限に届かない時間は None
    assert bike[trend.cell_index("weekday", 2)] == 990
    assert bike[trend.cell_index("weekday", 3)] is None
    # 土曜 2 時は 4 日（28 日に土曜が 4 回）で下限ちょうど
    assert bike[trend.cell_index("sat", 2)] == 990
    # **門を通らない組・当てはめの無い組は 72 個とも None**
    assert docomo["p_bike_x1000"] == [None] * trend.CELLS
    assert docomo["p_dock_x1000"] == [None] * trend.CELLS
    n_days = hello["n_days"]
    assert isinstance(n_days, list)
    assert n_days[trend.cell_index("weekday", 2)] == 20


def test_trend_rows_skip_ports_without_observations_in_the_window() -> None:
    """**窓に観測の無いポートは写さない**（古い日次にだけ居たポート）。"""
    tables = {
        P: daily_table([(SYSTEM, "a", "sat", 0, 3, 3, 3)]),
        P - timedelta(days=40): daily_table([(SYSTEM, "gone", "weekday", 0, 3, 3, 3)]),
    }
    ports, counts = counts_of(tables)
    rows = trend.trend_rows(trend.hourly_profile(counts, P, len(ports)), ports.ports, {})
    assert [(one["system_id"], one["station_id"]) for one in rows] == [(SYSTEM, "a")]
