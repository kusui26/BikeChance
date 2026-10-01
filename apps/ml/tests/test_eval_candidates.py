"""候補を並べて比べる（`eval/candidates.py`、W6 の PR F、D-32 の (c)、契約 37）。

**主題は 2 つ。** 候補の書き方を当てはめる前に確かめること（28 日の当てはめを数十分回して
から書き間違いに気づかない）と、**D-32 の (c) の判定が、選んだ日で測った甘い値でなく
「1 日目で選び、残りの日で測った」値で比べる**こと。
"""

from typing import Final

import numpy as np
import pytest

from bikechance_ml.baselines.climatology import FromSamples
from bikechance_ml.eval import candidates, gates, harness
from bikechance_ml.eval.candidates import Candidate
from bikechance_ml.eval.dataset import Samples, to_samples
from bikechance_ml.eval.split import mask_of, split_days
from tests import eval_fixture as fixture


# ── 候補の書き方 ───────────────────────────────────────────────
def test_the_candidates_are_read_as_pairs() -> None:
    assert candidates.parse_candidates("50:0.15, 100:0.15,300:0.05") == (
        Candidate(50, 0.15),
        Candidate(100, 0.15),
        Candidate(300, 0.05),
    )


@pytest.mark.parametrize(
    "text",
    ["", "100", "100:", ":0.1", "0:0.1", "100:0", "100:1.5", "a:b", "100:0.1:3", "-5:0.1"],
)
def test_a_candidate_written_wrong_is_refused(text: str) -> None:
    """**当てはめる前に止める**（28 日の当てはめを回してから気づかない）。"""
    with pytest.raises(candidates.CandidateError):
        candidates.parse_candidates(text)


def test_the_same_pair_twice_is_refused() -> None:
    with pytest.raises(candidates.CandidateError, match="2 度"):
        candidates.parse_candidates("100:0.15,100:0.15")


def test_the_name_and_the_spec() -> None:
    """**名前は表の列にも `harness` のモデル名にもなる。** 書き方は `--choose` に渡す形。"""
    one = Candidate(100, 0.15)
    assert (one.name, one.spec) == ("LGBM 100×0.15", "100:0.15")
    assert candidates.parse_one(one.spec) == one


# ── 特徴量の重要度（⑧）─────────────────────────────────────────
def test_the_importance_is_ranked_by_gain_with_its_share() -> None:
    found = candidates.top_importance(("a", "b", "c", "d"), np.asarray([1.0, 6.0, 0.0, 3.0]), top=2)
    assert [(one.column, one.share) for one in found] == [("b", 0.6), ("d", 0.3)]


def test_a_column_never_used_is_not_listed() -> None:
    """**一度も分岐に使われていない列は出さない**（上位 20 に 0 を並べない）。"""
    found = candidates.top_importance(("a", "b", "c"), np.asarray([2.0, 0.0, 0.0]))
    assert [one.column for one in found] == ["a"]


def test_ties_keep_the_column_order() -> None:
    """**同じ gain なら列の並び順**（報告書が回すたびに入れ替わらない）。

    **列を 50 並べる。** numpy の既定の並べ替えは、16 個までは挿入ソートで順を保つので、
    3 列では「安定な並べ替えを使っているか」が見分けられない。
    """
    columns = tuple(f"c{index:02d}" for index in range(50))
    found = candidates.top_importance(columns, np.ones(50))
    assert [one.column for one in found] == list(columns[: candidates.IMPORTANCE_TOP])


# ── 校正の合否 ───────────────────────────────────────────────
def test_the_calibration_verdict_is_a_plain_bool() -> None:
    """**numpy の数から作っても Python の `bool`**（`np.bool_` は JSON にできず、登録で落ちる）。"""
    worse = candidates.EceSummary(np.float64(0.05), "—", {"0": np.float64(0.01)}, 0.0, "—")
    better = candidates.EceSummary(np.float64(0.01), "—", {"0": np.float64(0.01)}, 0.0, "—")
    assert type(worse.passes) is bool
    assert worse.passes is False
    assert better.passes is True


def test_exactly_the_limit_does_not_pass() -> None:
    """**基準は「未満」**（開発プラン §7.1：ECE < 0.03）。ちょうど 0.03 は配らない。"""
    at_limit = candidates.EceSummary(candidates.MAX_ECE, "—", {}, 0.0, "—")
    assert at_limit.passes is False


# ── D-32 の (c) ────────────────────────────────────────────────
def _summary(
    trees: int, rate: float, *, holdout: float | None, served: float
) -> candidates.Summary:
    """配る形の改善だけを持つまとめ（**ほかの欄は見ない**）。"""
    cell = {gates.CELL_GATE: gates.Served(improvement=served, forest_share=0.5)}
    held = {} if holdout is None else {gates.CELL_GATE: gates.Served(holdout, forest_share=0.5)}
    judged = gates.Judged(
        model=Candidate(trees, rate).name, cells=(), horizons=(), served=cell, holdout=held
    )
    ece = candidates.EceSummary(0.0, "—", {}, 0.0, "—")
    return candidates.Summary(Candidate(trees, rate), judged, ece, ())


def test_a_candidate_within_one_point_of_300_trees_is_enough() -> None:
    """**50 本 +11.44% 対 300 本 +12.66%** は 1.22 ポイント差で「超える」。

    100 本（+12.03%）は 0.63 ポイント差で「以内」。
    """
    found = candidates.tree_budget(
        [
            _summary(50, 0.15, holdout=0.1144, served=0.15),
            _summary(100, 0.15, holdout=0.1203, served=0.15),
            _summary(300, 0.05, holdout=0.1266, served=0.15),
        ]
    )
    by_trees = {one.candidate.trees: one for one in found}
    assert by_trees[50].gap_pp == pytest.approx(1.22)
    assert by_trees[50].within is False
    assert by_trees[100].gap_pp == pytest.approx(0.63)
    assert by_trees[100].within is True
    assert by_trees[300].gap_pp == pytest.approx(0.0)


def test_exactly_one_point_is_within() -> None:
    found = candidates.tree_budget(
        [
            _summary(50, 0.15, holdout=0.11, served=0.0),
            _summary(300, 0.05, holdout=0.12, served=0.0),
        ]
    )
    assert found[0].gap_pp == pytest.approx(1.0)
    assert found[0].within is True


def test_the_budget_compares_the_held_out_improvement() -> None:
    """**選んだ日で測った値（甘い）ではなく、1 日目で選び残りで測った値で比べる。**

    検証日ぜんぶで測ると差は 0 だが、残りの日で測ると 3 ポイント開いている。
    """
    found = candidates.tree_budget(
        [
            _summary(50, 0.15, holdout=0.09, served=0.15),
            _summary(300, 0.05, holdout=0.12, served=0.15),
        ]
    )
    assert found[0].gap_pp == pytest.approx(3.0)
    assert found[0].within is False


def test_with_one_evaluation_day_the_whole_period_is_compared() -> None:
    """**検証が 1 日なら**、残りの日が無いので検証日ぜんぶの値で比べる。"""
    found = candidates.tree_budget(
        [
            _summary(50, 0.15, holdout=None, served=0.10),
            _summary(300, 0.05, holdout=None, served=0.12),
        ]
    )
    assert found[0].gap_pp == pytest.approx(2.0)


def test_without_300_trees_there_is_nothing_to_compare() -> None:
    """**300 本の候補が無ければ判定しない**（None）。黙って別の本数と比べない。"""
    found = candidates.tree_budget([_summary(50, 0.15, holdout=0.1, served=0.1)])
    assert (found[0].gap_pp, found[0].within) == (None, None)


def test_the_lower_learning_rate_is_the_300_trees_reference() -> None:
    """**300 本が 2 つあれば、設計どおりの 0.05 を相手にする**（学習率の低いほう）。"""
    found = candidates.tree_budget(
        [
            _summary(100, 0.15, holdout=0.10, served=0.0),
            _summary(300, 0.10, holdout=0.20, served=0.0),
            _summary(300, 0.05, holdout=0.12, served=0.0),
        ]
    )
    assert found[0].gap_pp == pytest.approx(2.0)


# ── 1 つの候補のまとめ（harness の結果から）─────────────────────
SPLIT: Final = split_days(fixture.DAYS, evaluate_days=1, purge_days=1)
MODEL: Final[str] = Candidate(50, 0.15).name


def _outcome() -> tuple[harness.Outcome, Samples]:
    """3 日（学習・パージ・検証 1 日ずつ）× 2 系統 × 2 水平 × 台数の違う 3 ポート。"""
    rows = [
        fixture.row(day, system, station, horizon, bikes, 9 - bikes, 1 if bikes else 0, 1)
        for day in fixture.DAYS
        for system in ("hellocycling", "docomo-cycle")
        for horizon in (5, 60)
        for station, bikes in (("a", 0), ("b", 1), ("c", 7))
    ]
    samples = to_samples(fixture.to_table(rows))
    evaluated = samples.take(mask_of(samples, SPLIT.evaluate))
    guess = {name: np.full(len(evaluated), 0.8) for name in ("bike", "dock")}
    return harness.run(samples, SPLIT, {MODEL: guess}, climate=FromSamples()), evaluated


def test_a_summary_is_made_from_the_evaluation() -> None:
    outcome, evaluated = _outcome()
    summary = candidates.summarize(Candidate(50, 0.15), outcome, evaluated, SPLIT.evaluate)
    assert summary.judged.model == MODEL
    assert set(summary.ece.bucket) == set(candidates.ECE_BUCKETS)
    assert len(summary.decisions) == 4, "system × ターゲットの 4 組"
    assert summary.judged.holdout == {}, "検証が 1 日なので、残りの日で測る値は無い"


def test_the_bucket_ece_is_measured_on_that_bucket_only() -> None:
    """**バケツ 0 の行（台数 0）だけで測る。** 0.8 と出して全部外れたので、ずれは 0.8。"""
    outcome, evaluated = _outcome()
    probability = candidates.probabilities_of(outcome)
    summary = candidates.ece_summary(outcome, evaluated, probability, MODEL)
    assert summary.bucket["0"] == pytest.approx(0.8)
    assert summary.bucket["1"] == pytest.approx(0.2)
    assert summary.passes is False


def test_the_decision_compares_the_served_form_with_b3() -> None:
    """**配る形の `coverage@0.9`**：0.8 と出す候補が通ったセルでは 0.9 以上を出さない。"""
    outcome, evaluated = _outcome()
    probability = candidates.probabilities_of(outcome)
    groups = candidates.decision_groups(evaluated, probability, MODEL)
    assert {(one.system, one.target) for one in groups} == {
        (system, target)
        for system in ("docomo-cycle", "hellocycling")
        for target in ("bike", "dock")
    }
    for one in groups:
        assert 0.0 <= one.served.coverage <= 1.0
        assert 0.0 <= one.reference.coverage <= 1.0
