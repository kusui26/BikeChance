"""当てはめの側の門（`jobs/fit_lightgbm.py`）。

**主題は「違う値を出す成果物が生まれ得ないこと」。** 配信は `lightgbm` を読み込まず
木を numpy で歩く（W4-19）。同じ値が出ることは**宣言ではなく検査**で担保する、と決めた
——その検査そのものが働いているかを、ここで確かめる。

**照合に足す「実データが踏まない枝」も見る。** 検証日の行だけでは全欠損・全 0・
未知のカテゴリを一度も通らないことがあり、**配信で最初に踏むのがその枝**では困る。
"""

from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Final

import numpy as np
import pytest

from bikechance_ml.eval import harness
from bikechance_ml.eval.split import DaySplit
from bikechance_ml.features.arrays import Float64
from bikechance_ml.jobs import fit_lightgbm as fit
from bikechance_ml.models import matrix
from tests.test_model_artifact import ARTIFACT, BOOSTERS, N_COLUMNS, _features

TARGET: Final[str] = "bike"


def _values(rows: int = 400) -> Float64:
    return _features(rows, 4242)


# ── 照合の門 ──────────────────────────────────────────────────
def test_a_matching_forest_passes_and_reports_the_gap() -> None:
    """**通ったときも差を残す。** 「0 だったから書かなかった」を作らない。"""
    checked = fit.refuse_if_different(BOOSTERS[TARGET], ARTIFACT.forests[TARGET], TARGET, _values())
    assert checked.n_rows == 400
    assert checked.max_gap < fit.MAX_DISAGREEMENT


def test_a_forest_that_disagrees_is_refused() -> None:
    """**葉を 1 つずらしただけで止まる。** ここが PR E′ の要である。"""
    forest = ARTIFACT.forests[TARGET]
    value = forest.value.copy()
    value[forest.left == np.arange(forest.n_nodes, dtype=np.int32)] += 0.5
    with pytest.raises(fit.ForestMismatchError, match=r"Booster\.predict"):
        fit.refuse_if_different(BOOSTERS[TARGET], replace(forest, value=value), TARGET, _values())


def test_flatten_and_verify_returns_a_usable_forest() -> None:
    forest, checked = fit.flatten_and_verify(BOOSTERS[TARGET], TARGET, _values())
    assert len(forest) == BOOSTERS[TARGET].num_trees()
    assert checked.max_gap < fit.MAX_DISAGREEMENT


# ── 照合に使う行 ──────────────────────────────────────────────
def test_the_verification_rows_add_the_branches_real_data_misses() -> None:
    """**全欠損・全 0・未知のカテゴリ**を、実データの後ろに足す。"""
    values = _values(50)
    rows = fit.verification_rows(values)
    assert len(rows) == 50 + 3 * 50  # 実データが少ないので仕込みも 50 行ずつ

    missing, zero, unseen = rows[50:100], rows[100:150], rows[150:]
    assert bool(np.isnan(missing).all()), "全欠損の行が入っていません"
    assert not zero.any(), "全 0 の行が入っていません"
    categorical = list(matrix.categorical_indices())
    assert (unseen[:, categorical] == fit.UNSEEN_CATEGORY).all(), "未知のカテゴリが入っていません"


def test_the_unseen_rows_keep_the_numeric_columns() -> None:
    """**未知のカテゴリの行は、カテゴリ列だけ差し替える。** 数値の枝も一緒に通したい。"""
    values = _values(20)
    unseen = fit.verification_rows(values)[60:]
    numeric = [one for one in range(N_COLUMNS) if one not in matrix.categorical_indices()]
    assert np.array_equal(unseen[:, numeric], values[:, numeric], equal_nan=True)


def test_the_stress_rows_are_capped_by_the_real_rows() -> None:
    """実データが `STRESS_ROWS` より多ければ、仕込みは `STRESS_ROWS` 行ずつ。"""
    values = _features(fit.STRESS_ROWS + 10, 9)
    assert len(fit.verification_rows(values)) == fit.STRESS_ROWS + 10 + 3 * fit.STRESS_ROWS


# ── 登録に残るもの ────────────────────────────────────────────
def test_the_card_path_is_recorded_relative_to_the_repository(tmp_path: Path) -> None:
    """**登録簿には「どこ起点か」が分かる形で残す。**

    `--card` はシェルから見た書き出し先なので、`apps/ml` から走らせると
    `../../docs/…` になる。それをそのまま入れると読む人が辿れない。
    """
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "model_cards").mkdir(parents=True)
    card = tmp_path / "docs" / "model_cards" / "x.md"
    deep = tmp_path / "apps" / "ml"
    deep.mkdir(parents=True)

    assert fit.card_reference(str(card)) == "docs/model_cards/x.md"
    assert fit.card_reference(f"{deep}/../../docs/model_cards/x.md") == "docs/model_cards/x.md"


def test_a_card_outside_any_repository_is_left_alone(tmp_path: Path) -> None:
    """**勝手に別の場所を指さない。** `.git` が見つからなければそのまま残す。"""
    outside = tmp_path / "loose.md"
    assert fit.card_reference(str(outside)) == str(outside)


def test_no_card_stays_none() -> None:
    assert fit.card_reference(None) is None


def test_the_check_is_recorded_for_the_registry() -> None:
    """**照合の結果が `model_versions.metrics` に入る。** 後から読める事実にする。"""
    rows = fit._to_check_rows({TARGET: fit.Checked(n_rows=1234, max_gap=1e-15)})
    assert rows["tolerance"] == fit.MAX_DISAGREEMENT
    assert rows["targets"] == {TARGET: {"n_rows": 1234, "max_gap": 1e-15}}


def _empty_outcome() -> harness.Outcome:
    """`to_registration` を通すだけの最小の結果。**中身は見ない。**"""
    day = date(2026, 9, 7)
    return harness.Outcome(
        split=DaySplit(fit=(day,), purge=(day,), evaluate=(day,)),
        n_fit=1,
        n_eval=1,
        fits=(),
        overall=(),
        by_horizon=(),
        by_bucket=(),
    )


CHECKS: Final[dict[str, fit.Checked]] = {
    "bike": fit.Checked(n_rows=1, max_gap=0.0),
    "dock": fit.Checked(n_rows=1, max_gap=0.0),
}


def test_the_registration_normalises_the_card_path(tmp_path: Path) -> None:
    """**登録する形が `card_reference` を通っている。**

    `card_reference` を直接試すだけでは、**呼び出し側が使うのをやめても気づけない**
    （実際に 1 度素通りした）。ここは `to_registration` の出力そのものを見る。
    """
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs").mkdir()
    card = f"{tmp_path}/apps/ml/../../docs/card.md"

    row = fit.to_registration(ARTIFACT, _empty_outcome(), CHECKS, card)

    assert row["card_path"] == "docs/card.md"


def test_the_registration_can_only_ask_for_candidate() -> None:
    """**`register_model_version` は candidate しか受け付けない。** 送る側も揃える。"""
    row = fit.to_registration(ARTIFACT, _empty_outcome(), CHECKS, None)
    assert row["status"] == "candidate"
    assert row["card_path"] is None
    assert row["artifact_path"] == "lightgbm/lgbm-v0-test.json.gz"
