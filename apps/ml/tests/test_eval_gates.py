"""門を当てて「配る形」を測る（`eval/gates.py`、W6 の PR F）。

**主題は「手で計算できる値と一致すること」と「境目」。** 門は配る確率を決めるので、
10% の境目・判定の対象外・バケツの切り方が 1 つずれると、**例外を出さずに別のものを配る。**

確率は答えが手で出る形にしてある：答えがすべて 1 の行なら、確率 `p` の Brier は `(1 − p)²`。
"""

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Final

import numpy as np
import pytest

from bikechance_ml.eval import gates
from bikechance_ml.eval.dataset import BUCKET_LABELS, Samples, to_samples
from bikechance_ml.features.arrays import Float64
from bikechance_ml.features.constants import HORIZONS_MIN
from tests import eval_fixture as fixture

DAY: Final[date] = fixture.DAYS[0]
NEXT: Final[date] = fixture.DAYS[1]
MODEL: Final[str] = "LGBM 100×0.15"


def _samples(rows: Sequence[dict[str, object]]) -> Samples:
    return to_samples(fixture.to_table(rows))


def _rows(
    n: int,
    *,
    system: str = "hellocycling",
    h_min: int = 30,
    bikes: int = 0,
    day: date = DAY,
    weight: float = 1.0,
) -> list[dict[str, object]]:
    """答えがすべて 1 の行を `n` 本（台数 `bikes` のバケツに入る）。"""
    return [
        fixture.row(day, system, f"s{index}", h_min, bikes, 5, 1, 1, weight=weight)
        for index in range(n)
    ]


def _probability(
    n: int, *, b0: float, b3: float, model: float
) -> Mapping[str, Mapping[str, Float64]]:
    """両ターゲットに同じ確率を置く（答えがすべて 1 なので Brier は `(1 − p)²`）。"""
    one = {
        gates.PERSISTENCE: np.full(n, b0),
        gates.REFERENCE: np.full(n, b3),
        MODEL: np.full(n, model),
    }
    return {"bike": one, "dock": one}


def _cells(
    samples: Samples, probability: Mapping[str, Mapping[str, Float64]]
) -> list[gates.Scored]:
    every = np.ones(len(samples), dtype=np.bool_)
    return list(gates.score_units(samples, probability, MODEL, gates.CELL_GATE, every))


# ── 1 つのセルの判定 ────────────────────────────────────────────
def test_the_brier_of_each_model_is_measured_in_the_cell() -> None:
    samples = _samples(_rows(4))
    (bike, dock) = _cells(samples, _probability(4, b0=0.0, b3=0.7, model=0.72))
    assert (bike.target, dock.target) == ("bike", "dock")
    assert (bike.system, bike.h_min, bike.bucket, bike.n) == ("hellocycling", 30, "0", 4)
    assert bike.b0 == pytest.approx(1.0)
    assert bike.b3 == pytest.approx(0.09)
    assert bike.model == pytest.approx(0.0784)


def test_a_cell_improved_by_ten_percent_or_more_passes() -> None:
    """**B3 0.09 → 候補 0.0784 は 12.9%**。10% 以上なので LightGBM を配る。"""
    (cell, _) = _cells(_samples(_rows(2)), _probability(2, b0=0.0, b3=0.7, model=0.72))
    assert cell.improvement == pytest.approx((0.09 - 0.0784) / 0.09)
    assert cell.reason == gates.PASSED


def test_a_cell_improved_by_less_than_ten_percent_stays_on_b3() -> None:
    """**0.09 → 0.0841 は 6.6%**。届かないので B3 のまま。"""
    (cell, _) = _cells(_samples(_rows(2)), _probability(2, b0=0.0, b3=0.7, model=0.71))
    assert cell.reason == gates.BELOW


def test_exactly_ten_percent_passes() -> None:
    """**境目ちょうどは通る**（「10% 以上」。開発プラン §7.1）。"""
    cell = gates.Scored("hellocycling", "bike", 30, "0", 1, 1.0, b0=1.0, b3=0.1, model=0.09)
    assert cell.improvement == pytest.approx(0.10)
    assert cell.reason == gates.PASSED


def test_a_cell_where_persistence_is_nearly_perfect_is_excluded() -> None:
    """**B0 の Brier が 0.001 未満なら判定の対象外**（W3-16）。改善が大きくても B3 を配る。"""
    cell = gates.Scored("hellocycling", "bike", 5, "11+", 1, 1.0, b0=0.0009, b3=0.001, model=0.0)
    assert cell.reason == gates.EXCLUDED


def test_a_cell_whose_reference_is_perfect_cannot_pass() -> None:
    """**相手の Brier が 0 なら相対改善は測れない**（None）。通さない。"""
    cell = gates.Scored("hellocycling", "bike", 5, "0", 1, 1.0, b0=1.0, b3=0.0, model=0.0)
    assert cell.improvement is None
    assert cell.reason == gates.BELOW


def test_cells_are_split_by_horizon_and_by_the_count_bucket() -> None:
    """**同じ水平でも台数のバケツが違えば別のセル**（`y_bike` は `bikes` で切る）。"""
    rows = _rows(1, bikes=0) + _rows(1, bikes=7) + _rows(1, bikes=0, h_min=60)
    cells = _cells(_samples(rows), _probability(3, b0=0.0, b3=0.7, model=0.72))
    bike = sorted((one.h_min, one.bucket) for one in cells if one.target == "bike")
    assert bike == [(30, "0"), (30, "6-10"), (60, "0")]


def test_the_weight_is_the_sampling_weight() -> None:
    """**重みは逆抽出確率**（母集団へ戻す）。重い行の誤差がそのぶん効く。"""
    rows = _rows(1, weight=1.0) + _rows(1, weight=3.0)
    samples = _samples(rows)
    one = {
        gates.PERSISTENCE: np.array([0.0, 0.0]),
        gates.REFERENCE: np.array([0.5, 0.9]),
        MODEL: np.array([0.5, 0.9]),
    }
    (cell, _) = _cells(samples, {"bike": one, "dock": one})
    assert cell.b3 == pytest.approx((0.25 * 1 + 0.01 * 3) / 4)


def test_the_horizon_unit_merges_the_buckets() -> None:
    rows = _rows(1, bikes=0) + _rows(1, bikes=7)
    samples = _samples(rows)
    every = np.ones(2, dtype=np.bool_)
    probability = _probability(2, b0=0.0, b3=0.7, model=0.72)
    scored = gates.score_units(samples, probability, MODEL, gates.HORIZON_GATE, every)
    assert [(one.target, one.h_min, one.bucket, one.n) for one in scored] == [
        ("bike", 30, None, 2),
        ("dock", 30, None, 2),
    ]


# ── 番号の往復 ─────────────────────────────────────────────────
@pytest.mark.parametrize("unit", [gates.CELL_GATE, gates.HORIZON_GATE])
def test_every_unit_round_trips_between_the_number_and_the_names(unit: str) -> None:
    samples = _samples(
        _rows(1, system="docomo-cycle") + _rows(1, system="hellocycling", h_min=180, bikes=12)
    )
    per_system = len(HORIZONS_MIN) * (len(BUCKET_LABELS) if unit == gates.CELL_GATE else 1)
    for key in range(len(samples.systems) * per_system):
        system, horizon, bucket = gates.decode(key, unit)
        scored = gates.Scored(
            samples.systems[system],
            "bike",
            HORIZONS_MIN[horizon],
            None if bucket is None else BUCKET_LABELS[bucket],
            1,
            1.0,
            1.0,
            1.0,
            1.0,
        )
        assert gates.encode(samples, scored, unit) == key


def test_an_unknown_horizon_stops() -> None:
    samples = _samples(_rows(1, h_min=7))
    with pytest.raises(gates.UnknownHorizonError):
        _cells(samples, _probability(1, b0=0.0, b3=0.5, model=0.5))


# ── 配る形 ────────────────────────────────────────────────────
def test_the_served_form_uses_the_model_only_in_passing_cells() -> None:
    """**通ったセル（バケツ 0）は候補、通らなかったセル（6-10）は B3**（契約 35）。"""
    rows = _rows(2, bikes=0) + _rows(2, bikes=7)
    samples = _samples(rows)
    one = {
        gates.PERSISTENCE: np.zeros(4),
        gates.REFERENCE: np.array([0.7, 0.7, 0.7, 0.7]),
        MODEL: np.array([0.72, 0.72, 0.701, 0.701]),
    }
    probability = {"bike": one, "dock": one}
    every = np.ones(4, dtype=np.bool_)
    walks = gates.routed(samples, probability, MODEL, gates.CELL_GATE, every)
    mixed = gates.served_probability(probability, MODEL, walks)
    assert walks["bike"].tolist() == [True, True, False, False]
    assert mixed["bike"].tolist() == [0.72, 0.72, 0.7, 0.7]


def test_no_gate_walks_every_row() -> None:
    samples = _samples(_rows(3))
    every = np.ones(3, dtype=np.bool_)
    probability = _probability(3, b0=0.0, b3=0.7, model=0.69)
    result = gates.served(samples, probability, MODEL, gates.NO_GATE, (every, every))
    assert result.forest_share == pytest.approx(1.0)
    assert result.improvement == pytest.approx((0.09 - 0.0961) / 0.09)


def test_the_improvement_is_the_mean_of_the_groups_not_the_pooled_rows() -> None:
    """**system × ターゲットの組ごとに出して平均する**（行の多い系統に引かれない）。

    HELLO 9 行は 12.9% の改善、ドコモ 1 行は 0%。行をまとめると 11.6%、組の平均は 6.4%。
    """
    rows = _rows(9, system="hellocycling") + _rows(1, system="docomo-cycle")
    samples = _samples(rows)
    one = {
        gates.PERSISTENCE: np.zeros(10),
        gates.REFERENCE: np.full(10, 0.7),
        MODEL: np.array([0.72] * 9 + [0.7]),
    }
    mixed = {"bike": one[MODEL], "dock": one[MODEL]}
    every = np.ones(10, dtype=np.bool_)
    gain = gates.improvement(samples, {"bike": one, "dock": one}, mixed, every)
    assert gain == pytest.approx(((0.09 - 0.0784) / 0.09 + 0.0) / 2)


def test_the_forest_share_is_weighted_by_the_cost_of_each_system() -> None:
    """**ドコモの行は 1 行 1 木の秒が 1.35 倍**。同じ行数でも割合はドコモに寄る。"""
    rows = _rows(1, system="docomo-cycle") + _rows(1, system="hellocycling")
    samples = _samples(rows)
    walks = {name: np.array([True, False]) for name in ("bike", "dock")}
    share = gates.forest_share(samples, walks, np.ones(2, dtype=np.bool_))
    docomo, hello = gates.ROW_TREE_SECONDS["docomo-cycle"], gates.ROW_TREE_SECONDS["hellocycling"]
    assert share == pytest.approx(docomo / (docomo + hello))


def test_the_forest_share_is_weighted_by_the_sampling_weight() -> None:
    rows = _rows(1, weight=1.0) + _rows(1, weight=3.0)
    samples = _samples(rows)
    walks = {name: np.array([False, True]) for name in ("bike", "dock")}
    assert gates.forest_share(samples, walks, np.ones(2, dtype=np.bool_)) == pytest.approx(0.75)


def test_an_unknown_system_stops_the_cost_weighting() -> None:
    samples = _samples(_rows(1, system="unknown-cycle"))
    with pytest.raises(gates.UnknownSystemError):
        gates.system_costs(samples)


# ── 1 日目で選び、残りの日で測る ─────────────────────────────────
def test_the_holdout_chooses_on_the_first_day_and_measures_on_the_rest() -> None:
    """**1 日目は候補が良く、2 日目は悪い。** 1 日目で通したセルを 2 日目で測ると損になる。

    同じ日で選んで測ると（全検証日）、この損は見えない——門は 2 日ぶんの平均で決まり、
    平均では 10% に届かないので B3 のまま（改善 0）になる。
    """
    rows = _rows(2, day=DAY) + _rows(2, day=NEXT)
    samples = _samples(rows)
    one = {
        gates.PERSISTENCE: np.zeros(4),
        gates.REFERENCE: np.full(4, 0.7),
        MODEL: np.array([0.8, 0.8, 0.6, 0.6]),
    }
    judged = gates.judge(samples, {"bike": one, "dock": one}, MODEL, (DAY, NEXT))
    cell = judged.holdout[gates.CELL_GATE]
    assert cell.improvement == pytest.approx((0.09 - 0.16) / 0.09)
    assert cell.forest_share == pytest.approx(1.0)
    assert judged.served[gates.CELL_GATE].improvement == pytest.approx(0.0)
    assert judged.served[gates.CELL_GATE].forest_share == pytest.approx(0.0)


def test_a_single_evaluation_day_has_no_holdout() -> None:
    samples = _samples(_rows(2))
    judged = gates.judge(samples, _probability(2, b0=0.0, b3=0.7, model=0.72), MODEL, (DAY,))
    assert judged.holdout == {}
    assert set(judged.served) == set(gates.UNITS)


def test_the_judged_candidate_lists_what_passed() -> None:
    """**返却は `docks` のバケツで切る。** 4 行とも docks 5 なので、返却のセルは「3-5」の 1 つ。

    貸出のバケツ 6-10 は B0 が当たっている（Brier 0）ので判定の対象外になる。
    """
    rows = _rows(2, bikes=0) + _rows(2, bikes=7)
    samples = _samples(rows)
    one = {
        gates.PERSISTENCE: np.array([0.0, 0.0, 1.0, 1.0]),
        gates.REFERENCE: np.full(4, 0.7),
        MODEL: np.array([0.72, 0.72, 0.9, 0.9]),
    }
    judged = gates.judge(samples, {"bike": one, "dock": one}, MODEL, (DAY,))
    assert [(one.target, one.bucket) for one in judged.passing(gates.CELL_GATE)] == [
        ("bike", "0"),
        ("dock", "3-5"),
    ]
    assert [(one.target, one.bucket) for one in judged.judgeable(gates.CELL_GATE)] == [
        ("bike", "0"),
        ("dock", "3-5"),
    ]
    assert len(judged.cells) == 3
