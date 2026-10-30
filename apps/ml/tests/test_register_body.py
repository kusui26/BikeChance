"""B3 の登録の本文と評価の要点（`jobs/register_body.py`。W8 の PR G）。

**主題は「#5 の登録と同じ形を、報告書を読み直さずに作ること」**。#4・#5 の手元の道具は点検と
評価の Markdown を正規表現で読んでいた。ここでは成果物と `harness.Outcome` から作るので、
**数が報告書と同じになること**（B3 が B1 以下の組・重み付き Brier）を突き合わせる。
"""

import hashlib
import json
import math
from datetime import date
from typing import Final

from bikechance_ml.baselines import artifact as baseline_artifact
from bikechance_ml.eval import report
from bikechance_ml.jobs import register_body
from tests import eval_fixture, test_infer
from tests import test_eval_harness as harness_test

ARTIFACT: Final = test_infer.ARTIFACT
BODY: Final[bytes] = baseline_artifact.to_bytes(ARTIFACT)
VALIDATION: Final[date] = eval_fixture.DAYS[-1]
FACTS: Final = register_body.eval_facts(harness_test.OUTCOME, VALIDATION)
CONTEXT: Final = register_body.Context(
    card_path="docs/refits/2026-11-01.md",
    note="試験の注記",
    source="docs/refits/2026-11-01.md（当てはめ直しの記録。自動）",
    open_peak_delta_mb=344,
)

#: #5 の登録（`baseline-b3-v0-20261025`）の `metrics` の欄。**形を変えない。**
FIVE_METRICS: Final[set[str]] = {
    "source",
    "b2",
    "artifact",
    "split",
    "n_eval",
    "brier_weighted",
    "brier_weighted_b1",
    "caveat",
}


def _registration() -> dict[str, object]:
    return register_body.registration(ARTIFACT, BODY, FACTS, CONTEXT)


def _metrics() -> dict[str, object]:
    metrics = _registration()["metrics"]
    assert isinstance(metrics, dict)
    return metrics


def test_the_row_is_a_candidate_baseline_with_the_artifact_path() -> None:
    row = _registration()
    assert (row["kind"], row["status"]) == ("baseline", "candidate")
    assert row["model_version"] == ARTIFACT.model_version
    assert row["artifact_path"] == baseline_artifact.artifact_path(ARTIFACT.model_version)
    assert row["train_days"] == list(ARTIFACT.train_days)
    assert (row["card_path"], row["note"]) == (CONTEXT.card_path, CONTEXT.note)


def test_the_metrics_keep_the_shape_of_the_fifth_refit() -> None:
    assert set(_metrics()) == FIVE_METRICS
    json.dumps(_registration(), ensure_ascii=False)  # JSON に載る（RPC にそのまま渡す）


def test_the_artifact_record_describes_the_bytes() -> None:
    record = register_body.artifact_record(ARTIFACT, BODY, 344)
    assert record["sha256"] == hashlib.sha256(BODY).hexdigest()
    assert record["bytes"] == len(BODY)
    assert record["ports"] == len(ARTIFACT.ports)
    assert record["open_peak_delta_mb"] == 344
    blend = record["blend"]
    assert isinstance(blend, dict)
    assert blend["bike"] == ARTIFACT.targets["bike"].b3.describe()


def test_usable_cells_by_dow_add_up_to_the_target_count() -> None:
    """**曜日種別ごとの数は、ターゲットの数を割ったもの**（数え方は `climatology` の 1 か所）。"""
    record = register_body.b2_record(ARTIFACT)
    by_dow, usable = record["usable_cells_by_dow"], record["usable_cells"]
    assert isinstance(by_dow, dict) and isinstance(usable, dict)
    assert sum(by_dow.values()) == usable[register_body.BY_DOW_TARGET]
    assert record["source"] == f"profiles/date={ARTIFACT.train_days[-1]}/profile.parquet"
    assert record["own_day"] == register_body.OWN_DAY


def test_the_facts_count_the_same_pairs_as_the_report() -> None:
    """**報告書の「B3 が B1 以下」と同じ組**（`>` で数える）。"""
    expected = report._b3_behind(harness_test.OUTCOME)
    assert len(FACTS.behind) == len(expected)
    assert FACTS.pairs == len(harness_test.OUTCOME.by_horizon)
    assert all(one.excess > 0 for one in FACTS.behind)


def test_the_briers_are_rounded_like_the_report_table() -> None:
    keys = {f"{one.slice.system}/{one.slice.target}" for one in harness_test.OUTCOME.overall}
    assert set(FACTS.brier_b3) == keys == set(FACTS.brier_b1)
    for value in (*FACTS.brier_b3.values(), *FACTS.brier_b1.values()):
        assert value == round(value, register_body.BRIER_DIGITS)


def test_the_caveat_names_the_split_and_the_served_days() -> None:
    caveat = register_body.caveat(FACTS, ARTIFACT.train_days)
    assert str(VALIDATION) in caveat
    assert FACTS.split in caveat
    assert f"{ARTIFACT.train_days[0]}〜{ARTIFACT.train_days[-1]}" in caveat
    assert f"{FACTS.pairs} 件中 {len(FACTS.behind)} 件" in caveat


def test_a_perfect_b1_does_not_divide_by_zero() -> None:
    """**B1 の Brier が 0 の切り口でも落ちない**（小さな表では B1 が完全に当てることがある）。"""
    behind = register_body.Behind("hellocycling", "bike", 5, math.inf)
    assert behind.describe() == "hellocycling の bike の 5 分（B1 の Brier が 0）"
    # 仕込みの評価には、B1 が完全に当てて B3 が外した切り口がある（ここで 0 で割っていた）
    assert any(math.isinf(one.excess) for one in FACTS.behind)


def test_the_worst_pair_is_the_largest_excess() -> None:
    behind = (
        register_body.Behind("hellocycling", "dock", 30, 0.0596),
        register_body.Behind("docomo-cycle", "bike", 10, 0.0524),
    )
    facts = register_body.EvalFacts(VALIDATION, "分割", 1, {}, {}, behind, 40)
    assert facts.worst == behind[0]
    assert facts.worst.describe() == "hellocycling の dock の 30 分（+5.96%）"
