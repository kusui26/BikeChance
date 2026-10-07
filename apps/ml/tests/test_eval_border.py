"""合成器の境目の差（`eval/border.py`、W6 プラン §5.10、PR H の完了条件 3）。"""

import numpy as np
import pytest

from bikechance_ml.eval import border
from bikechance_ml.eval.dataset import BUCKET_LABELS, TARGETS
from bikechance_ml.features.constants import HORIZONS_MIN
from bikechance_ml.models import composite
from tests import composite_fixture as fixture
from tests import test_composite, test_infer

N_HORIZONS = len(HORIZONS_MIN)
N_BUCKETS = len(BUCKET_LABELS)


def _found(*cells: tuple[int, int]) -> np.ndarray[tuple[int, ...], np.dtype[np.bool_]]:
    table = np.zeros((N_HORIZONS, N_BUCKETS), dtype=np.bool_)
    for horizon, bucket in cells:
        table[horizon, bucket] = True
    return table


def _edges(
    found: np.ndarray[tuple[int, ...], np.dtype[np.bool_]], rows: list[tuple[int, int]]
) -> list[tuple[bool, bool, bool]]:
    horizon = np.array([one[0] for one in rows], dtype=np.int64)
    bucket = np.array([one[1] for one in rows], dtype=np.int64)
    walked, h_edge, b_edge = border.edges(found, horizon, bucket)
    return list(zip(walked.tolist(), h_edge.tolist(), b_edge.tolist(), strict=True))


def test_a_lonely_cell_is_an_edge_both_ways() -> None:
    """**隣がどちらも B3** のセル：水平にもバケツにも境。森を歩かない行は数えない。"""
    found = _found((4, 2))
    assert _edges(found, [(4, 2), (4, 3)]) == [(True, True, True), (False, False, False)]


def test_a_run_of_cells_has_edges_only_at_its_ends() -> None:
    """水平に続くセルは、**端だけが境**（真ん中は両隣が森）。"""
    found = _found((3, 1), (4, 1), (5, 1))
    rows = [(3, 1), (4, 1), (5, 1)]
    assert [one[1] for one in _edges(found, rows)] == [True, False, True]


def test_the_outside_of_the_table_is_not_an_edge() -> None:
    """**表の外は境にしない**（最初の水平の手前・最後のバケツの先には点が無い）。"""
    full_horizon = _found(*((one, N_BUCKETS - 1) for one in range(N_HORIZONS)))
    corner = [(0, N_BUCKETS - 1), (N_HORIZONS - 1, N_BUCKETS - 1)]
    walked, horizon_edge, bucket_edge = zip(*_edges(full_horizon, corner), strict=True)
    assert walked == (True, True)
    assert horizon_edge == (False, False)
    assert bucket_edge == (True, True), "手前のバケツ（B3）との境は数える"


def test_an_empty_spread_says_nothing() -> None:
    """**行が無ければ 0 と None**（0 と書くと「差が無い」と読めてしまう）。"""
    empty = border.spread_of(np.zeros(0, dtype=np.float64))
    assert (empty.rows, empty.p50, empty.largest) == (0, None, None)


def test_the_spread_reports_quantiles_and_the_largest() -> None:
    values = np.linspace(0.0, 1.0, 101, dtype=np.float64)
    spread = border.spread_of(values)
    assert spread.rows == 101
    assert spread.p50 == pytest.approx(0.5)
    assert spread.p90 == pytest.approx(0.9)
    assert spread.p99 == pytest.approx(0.99)
    assert spread.largest == 1.0


def test_the_border_counts_the_walked_rows_of_both_systems() -> None:
    """**配る形と同じ関数で**測る：森を歩いた行の数は、合成器が歩く行の数と同じ。"""
    made = fixture.build()
    tables = {one: test_composite.serving_table(one) for one in test_composite.SYSTEMS}
    measured = border.measure(made, test_infer.AT, tables)
    walked = sum(sum(one.values()) for one in test_composite.EXPECTED_WALKS.values())
    assert measured.walked.rows == walked
    assert measured.horizon_edge.rows <= walked and measured.bucket_edge.rows <= walked
    assert set(measured.as_json()) == {"walked", "horizon_edge", "bucket_edge"}


def test_the_gaps_are_zero_off_the_forest() -> None:
    """**森を歩かない行では |P_LGBM − P_B3| は 0**（配る値が B3 そのもの）。"""
    made = fixture.build()
    table = test_composite.serving_table("hellocycling")
    for gaps, target in zip(
        border.gaps_of(made, "hellocycling", test_infer.AT, table), TARGETS, strict=True
    ):
        assert not gaps.difference[~gaps.walked].any(), target.name
        lookup = composite.lookup(made.lightgbm_cells(), "hellocycling", target)
        assert int(gaps.walked.sum()) == test_composite.EXPECTED_WALKS["hellocycling"][target.name]
        assert lookup.any()
