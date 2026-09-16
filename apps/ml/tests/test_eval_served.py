"""配った確率と実測の突き合わせ（`eval/served.py`、W5 プラン §6.12 の PR L）。

**この 1 ファイルの主題は 4 つ。**

  * **ラベルは `generated_at + h` で引く**（`base_observed_at` ではない）
  * **除外はオフラインと同じ関数**を通る（`features/exclude.py`）
  * **バケツは基準時刻の台数**で決まり、全体はその和になる
  * **オフラインの学習サンプルとラベルが一致する**（完了条件 3 の土台）
"""

from collections.abc import Sequence
from datetime import date, datetime, timedelta

import numpy as np
import pyarrow as pa
import pytest

from bikechance_ml.eval import served
from bikechance_ml.eval.dataset import BUCKET_LABELS, TARGETS
from bikechance_ml.eval.slices import ALL
from bikechance_ml.features import build
from bikechance_ml.features.constants import GRID_POINTS_PER_DAY, HORIZONS_MIN
from bikechance_ml.features.grid import JST, build_grid
from bikechance_ml.jobs import evaluate
from bikechance_ml.jobs.snapshot_table import SCHEMA as SNAPSHOT_SCHEMA
from tests import features_fixture as fixture

DAY = date(2026, 9, 7)
RENTING_AND_RETURNING = 7
SUSPENDED = 1
MISSING = -1
FETCH_DELAY = timedelta(seconds=40)


def at(hour: int, minute: int, second: int = 0) -> datetime:
    """JST の時刻。"""
    return datetime(DAY.year, DAY.month, DAY.day, hour, minute, second, tzinfo=JST)


def observations(rows: Sequence[tuple[str, datetime, int, int, int]]) -> pa.Table:
    """`(station_id, observed_at, bikes, docks, flags)` から Parquet と同じ形の表を作る。"""
    return pa.table(
        {
            "system_id": pa.array(["hellocycling"] * len(rows), type=pa.string()),
            "station_id": pa.array([one[0] for one in rows], type=pa.string()),
            "observed_at": pa.array(
                [one[1] for one in rows], type=SNAPSHOT_SCHEMA.field("observed_at").type
            ),
            "fetched_at": pa.array(
                [one[1] + FETCH_DELAY for one in rows],
                type=SNAPSHOT_SCHEMA.field("fetched_at").type,
            ),
            "bikes": pa.array([one[2] for one in rows], type=pa.int16()),
            "docks": pa.array([one[3] for one in rows], type=pa.int16()),
            "flags": pa.array([one[4] for one in rows], type=pa.int16()),
            "reported_age_s": pa.array([0] * len(rows), type=pa.int16()),
        },
        schema=SNAPSHOT_SCHEMA,
    )


def stream(
    station: str,
    start: datetime,
    minutes: int,
    bikes: int = 5,
    docks: int = 5,
    flags: int = RENTING_AND_RETURNING,
) -> list[tuple[str, datetime, int, int, int]]:
    """1 分ごとの観測を並べる。**as-of が常に引ける土台**にする。

    土台を敷かないと、試したいことの手前で `no_asof_at_t` に落ちてしまい、
    **検査が何も見ていない状態**になる（最初にこれで 3 本が空振りした）。
    """
    return [
        (station, start + timedelta(minutes=step), bikes, docks, flags) for step in range(minutes)
    ]


def override(
    rows: list[tuple[str, datetime, int, int, int]],
    moment: datetime,
    bikes: int,
    docks: int,
    flags: int = RENTING_AND_RETURNING,
) -> list[tuple[str, datetime, int, int, int]]:
    """ある時刻の観測だけを差し替える。**同じ時刻の行は 1 つだけにする。**"""
    kept = [one for one in rows if one[1] != moment]
    station = rows[0][0]
    return sorted([*kept, (station, moment, bikes, docks, flags)], key=lambda one: one[1])


def cycle_at(moment: datetime) -> int:
    """その JST 時刻が当日の何番目の基準時刻か。"""
    return evaluate.cycle_of(DAY, moment)


def serve(
    stations: Sequence[str],
    cycles: Sequence[int],
    p_bike: float = 0.5,
    p_dock: float = 0.5,
) -> served.Served:
    """全ポート・指定サイクルに同じ確率を配ったことにする。"""
    shape = (len(HORIZONS_MIN), len(stations), GRID_POINTS_PER_DAY)
    values = {
        "bike": np.zeros(shape, dtype=np.int16),
        "dock": np.zeros(shape, dtype=np.int16),
    }
    present = np.zeros((len(stations), GRID_POINTS_PER_DAY), dtype=np.bool_)
    for cycle in cycles:
        present[:, cycle] = True
        values["bike"][:, :, cycle] = round(p_bike * 1000)
        values["dock"][:, :, cycle] = round(p_dock * 1000)
    return served.Served(
        system_id="hellocycling",
        model_version="test-v0",
        day=DAY,
        stations=tuple(stations),
        present=present,
        p_x1000=values,
        n_cycles=len(cycles),
    )


def measure(
    table: pa.Table, stations: Sequence[str], cycles: Sequence[int], **kwargs: float
) -> served.Outcome:
    grid = build_grid(DAY, evaluate.LOOKBACK_HOURS, 3)
    keys = [("hellocycling", one) for one in stations]
    truth = served.to_truth(table, keys, grid)
    return served.evaluate(serve(stations, cycles, **kwargs), truth, grid)


def rows_of(outcome: served.Outcome, target: str, horizon: int, bucket: str) -> served.MetricRow:
    found = [
        one
        for one in outcome.rows
        if one.target == target and one.h_min == horizon and one.bucket == bucket
    ]
    assert len(found) == 1, f"{target}/{horizon}/{bucket} が {len(found)} 行"
    return found[0]


# ── ラベルは `generated_at + h` で引く ────────────────────────
def test_the_label_comes_from_the_target_time_not_the_base() -> None:
    """**基準時刻に 5 台あっても、`t + h` で 0 台ならラベルは 0。**

    「借りられるか」は着いたときの話である。基準時刻の状態を読んでしまうと、
    **B0（持続）と同じものを測る**ことになり、モデルの良し悪しが消える。
    """
    table = observations(override(stream("p1", at(8, 40), 60), at(9, 5), 0, 10))
    outcome = measure(table, ["p1"], [cycle_at(at(8, 55))], p_bike=0.5)
    # 基準時刻（8:55）は 5 台。5 分先の 9:00 も 5 台、**10 分先の 9:05 だけ 0 台**
    assert rows_of(outcome, "bike", 5, ALL).positives == 1.0
    assert rows_of(outcome, "bike", 10, ALL).positives == 0.0


def test_a_suspended_port_at_the_target_time_is_labelled_zero() -> None:
    """**停止は `t + h` ではラベル 0**（除外ではない。`features/labels.py`）。"""
    table = observations(override(stream("p1", at(8, 40), 60), at(9, 5), 5, 5, SUSPENDED))
    outcome = measure(table, ["p1"], [cycle_at(at(8, 55))])
    assert rows_of(outcome, "bike", 5, ALL).positives == 1.0
    assert rows_of(outcome, "bike", 10, ALL).positives == 0.0
    assert rows_of(outcome, "dock", 10, ALL).positives == 0.0, "返却側も 0 になる"


# ── 除外 ──────────────────────────────────────────────────────
def test_a_port_with_no_observation_at_the_base_is_dropped() -> None:
    """**基準時刻に as-of が引けない行は測らない**（`exclude_at_base`）。"""
    table = observations([("p1", at(13, 0), 5, 5, RENTING_AND_RETURNING)])
    outcome = measure(table, ["p1"], [cycle_at(at(9, 0))])
    assert outcome.rows == ()
    assert outcome.dropped["no_asof_at_t"] == 1


def test_a_missing_value_at_the_target_is_dropped_not_counted_as_zero() -> None:
    """**`-1` は「0 台」ではない**（`unobserved_at_target`）。補間もしない。"""
    table = observations(override(stream("p1", at(8, 40), 60), at(9, 0), MISSING, MISSING))
    outcome = measure(table, ["p1"], [cycle_at(at(8, 55))])
    assert not [one for one in outcome.rows if one.h_min == 5]
    assert outcome.dropped["unobserved_at_target"] == 1
    assert rows_of(outcome, "bike", 10, ALL).n == 1, "次の水平は測れる"


def test_a_cycle_without_a_log_is_not_measured() -> None:
    """**ログの無いサイクルは「居なかった」**（`present` が偽）。"""
    table = observations(stream("p1", at(8, 40), 60))
    one = measure(table, ["p1"], [cycle_at(at(9, 0))])
    two = measure(table, ["p1"], [cycle_at(at(9, 0)), cycle_at(at(9, 5))])
    assert rows_of(one, "bike", 5, ALL).n == 1
    assert rows_of(two, "bike", 5, ALL).n == 2


# ── バケツと全体 ──────────────────────────────────────────────
def test_the_bucket_comes_from_the_count_at_the_base_time() -> None:
    """バケツは**基準時刻**の台数で決まる（`t + h` ではない）。"""
    rows = override(stream("p1", at(8, 40), 60, bikes=8, docks=2), at(8, 54), 1, 9)
    table = observations(override(rows, at(8, 53), 1, 9))
    outcome = measure(table, ["p1"], [cycle_at(at(8, 55))])
    assert rows_of(outcome, "bike", 5, "1").n == 1, "基準時刻（8:54 の観測）は 1 台"
    assert not [one for one in outcome.rows if one.bucket == "6-10" and one.target == "bike"]


def test_the_whole_row_is_the_sum_of_the_buckets() -> None:
    """**「全体」はバケツの和**（同じ行を 2 度数えない）。"""
    table = observations(
        [
            *stream("p1", at(8, 40), 60, bikes=0, docks=10),
            *stream("p2", at(8, 40), 60, bikes=7, docks=3),
        ]
    )
    outcome = measure(table, ["p1", "p2"], [cycle_at(at(8, 55))])
    parts = [
        one.n
        for one in outcome.rows
        if one.target == "bike" and one.h_min == 5 and one.bucket in BUCKET_LABELS
    ]
    assert sum(parts) == rows_of(outcome, "bike", 5, ALL).n == 2


# ── 確率と参照 ────────────────────────────────────────────────
def test_the_probability_is_read_as_served() -> None:
    """**整数で配ったものをそのまま読む**（1000 分の 1 の刻み）。"""
    table = observations(stream("p1", at(8, 40), 60))
    outcome = measure(table, ["p1"], [cycle_at(at(8, 55))], p_bike=0.75)
    # ラベルは 1 なので Brier は (1 - 0.75)^2
    assert rows_of(outcome, "bike", 5, ALL).brier == pytest.approx(0.0625)


def test_persistence_is_the_reference_and_the_skill_follows() -> None:
    """**BSS の基準は B0**（オフラインと同じ。`eval/harness.py`）。"""
    rows = override(stream("p1", at(8, 40), 60), at(8, 54), 0, 10)
    table = observations(override(rows, at(8, 53), 0, 10))
    outcome = measure(table, ["p1"], [cycle_at(at(8, 55))], p_bike=0.5)
    row = rows_of(outcome, "bike", 5, ALL)
    # 基準時刻は 0 台なので B0 = 0、ラベルは 1 → B0 の Brier は 1
    assert row.brier_b0 == pytest.approx(1.0)
    assert row.brier == pytest.approx(0.25)
    assert row.skill_vs_b0 == pytest.approx(0.75)


def test_the_skill_is_none_when_persistence_is_perfect() -> None:
    """**B0 が完璧なら改善は測れない**（発散させずに「測れない」と言う）。"""
    table = observations(stream("p1", at(8, 40), 60))
    outcome = measure(table, ["p1"], [cycle_at(at(8, 55))])
    assert rows_of(outcome, "bike", 5, ALL).skill_vs_b0 is None


# ── 水平の契約 ────────────────────────────────────────────────
def test_a_log_with_other_horizons_is_refused() -> None:
    """**配列の何番目が何分先かが狂うと、読むときには気づけない。**"""
    with pytest.raises(served.HorizonMismatchError):
        served.check_horizons([5, 10, 15], "作り物")


def test_the_horizons_are_accepted_when_they_match() -> None:
    served.check_horizons(list(HORIZONS_MIN), "作り物")


# ── オフラインとの一致（完了条件 3 の土台）────────────────────
def test_labels_match_the_training_samples_row_by_row() -> None:
    """**学習サンプルと同じラベルが出る。**

    同じ日・同じ観測から `features/build.py` が作った行を 1 つずつ引き当て、
    `y_bike` / `y_dock` が一致することを見る。**ここがずれると、同じモデルの Brier が
    2 通り出る**（開発プラン §8.5）。

    ゴールデンのフィクスチャは 1% 抽出を通っているので行数は少ないが、
    **通った行は 1 つ残らず一致しなければならない。**
    """
    built = build.build_day(fixture.build_inputs()).table.to_pylist()
    grid = build_grid(fixture.DAY, evaluate.LOOKBACK_HOURS, 3)
    checked = 0
    for system_id in ("hellocycling", "docomo-cycle"):
        rows = [one for one in built if one["system_id"] == system_id]
        if not rows:
            continue
        checked += _compare_labels(system_id, rows, grid)
    assert checked == len(built), "作った行をすべて突き合わせた"
    assert checked > 0, "突き合わせる行が 1 つも無い"


def _compare_labels(system_id: str, rows: list[dict[str, object]], grid: object) -> int:
    """1 システムぶんを突き合わせ、見た行数を返す。"""
    table = fixture.load_snapshots()
    stations = sorted({str(one["station_id"]) for one in rows})
    keys = [(system_id, name) for name in stations]
    truth = served.to_truth(_only(table, system_id), keys, grid)  # type: ignore[arg-type]
    index = {name: position for position, name in enumerate(stations)}
    for one in rows:
        cycle = int(str(one["minute_of_day"])) // 5
        horizon = int(str(one["h_min"]))
        station = index[str(one["station_id"])]
        for target in TARGETS:
            found = served.label_of(truth, grid, target, horizon)  # type: ignore[arg-type]
            assert int(found[station, cycle]) == int(str(one[target.label])), (
                f"{system_id}/{one['station_id']} t={one['minute_of_day']} h={horizon}"
            )
    return len(rows)


def _only(table: pa.Table, system_id: str) -> pa.Table:
    mask = pa.array([one == system_id for one in table.column("system_id").to_pylist()])
    return table.filter(mask)
