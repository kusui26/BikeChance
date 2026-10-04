"""当てはめの側の門（`jobs/fit_lightgbm.py`）。

**主題は「違う値を出す成果物が生まれ得ないこと」。** 配信は `lightgbm` を読み込まず
木を numpy で歩く（W4-19）。同じ値が出ることは**宣言ではなく検査**で担保する、と決めた
——その検査そのものが働いているかを、ここで確かめる。

**照合に足す「実データが踏まない枝」も見る。** 検証日の行だけでは全欠損・全 0・
未知のカテゴリを一度も通らないことがあり、**配信で最初に踏むのがその枝**では困る。

**v1（W6 の PR F）で足した主題は 3 つ。**

1. **通らない指定は、当てはめる前に止まる**（28 日を回してから捨てない）
2. **門の相手の B3 は本番と同じ作り方**（学習窓の最後の 7 暦日。W6-10、契約 36）で、
   B2 の材料も同じ日から作る
3. **森と門の表は組で置き、組で登録する**（表は森の SHA-256 を持つ。契約 35・38）
"""

import gzip
import hashlib
import inspect
import json
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Final

import lightgbm as lgb
import numpy as np
import pytest

from bikechance_ml.baselines import climatology
from bikechance_ml.baselines.climatology import FromSamples
from bikechance_ml.eval import candidates, harness
from bikechance_ml.eval.candidates import Candidate, ForestFacts, Importance
from bikechance_ml.eval.dataset import TARGETS, Samples, to_samples
from bikechance_ml.eval.split import DaySplit, mask_of
from bikechance_ml.features import coverage
from bikechance_ml.features.arrays import Float32
from bikechance_ml.features.fingerprint import Fingerprint
from bikechance_ml.features.schema import WEATHER_COLUMNS
from bikechance_ml.io.supabase import SupabaseIo
from bikechance_ml.jobs import climate as climate_module
from bikechance_ml.jobs import fit_lightgbm as fit
from bikechance_ml.jobs.build_features import to_parquet_bytes
from bikechance_ml.jobs.window import Window
from bikechance_ml.models import artifact as lightgbm_artifact
from bikechance_ml.models import gates as gate_tables
from bikechance_ml.models import matrix, registry
from tests import eval_fixture as eval_rows
from tests.test_model_artifact import ARTIFACT, BOOSTERS, N_COLUMNS, _features
from tests.test_window import read_local, write_distinct_sizes, write_samples

TARGET: Final[str] = "bike"

#: 天気の門で使う 3 日。**`split_days(…, 1, 1)` で 学習 / パージ / 検証 に 1 日ずつ。**
DAYS: Final[tuple[date, ...]] = eval_rows.DAYS
SPLIT: Final[DaySplit] = DaySplit(fit=(DAYS[0],), purge=(DAYS[1],), evaluate=(DAYS[2],))

#: 被覆を割合から起こすときの行数。
ROWS: Final[int] = 1_000

#: 既定の候補（`--candidates` を書かなければこれ 1 つ）。
CANDIDATE: Final[Candidate] = candidates.parse_one(fit.DEFAULT_CANDIDATES)
VERSION: Final[str] = "lgbm-v1-test"


def _values(rows: int = 400) -> Float32:
    return _features(rows, 4242)


# ── 照合の門 ──────────────────────────────────────────────────
def test_a_matching_forest_passes_and_reports_the_gap() -> None:
    """**通ったときも差を残す。** 「0 だったから書かなかった」を作らない。"""
    checked = fit.refuse_if_different(
        BOOSTERS[TARGET], ARTIFACT.forests[TARGET], TARGET, [_values()]
    )
    assert checked.n_rows == 400
    assert checked.max_gap < fit.MAX_DISAGREEMENT


def test_a_forest_that_disagrees_is_refused() -> None:
    """**葉を 1 つずらしただけで止まる。** ここが PR E′ の要である。"""
    forest = ARTIFACT.forests[TARGET]
    value = forest.value.copy()
    value[forest.left == np.arange(forest.n_nodes, dtype=np.int32)] += 0.5
    with pytest.raises(fit.ForestMismatchError, match=r"Booster\.predict"):
        fit.refuse_if_different(BOOSTERS[TARGET], replace(forest, value=value), TARGET, [_values()])


def test_flatten_and_verify_returns_a_usable_forest() -> None:
    forest, checked = fit.flatten_and_verify(BOOSTERS[TARGET], TARGET, [_values()])
    assert len(forest) == BOOSTERS[TARGET].num_trees()
    assert checked.max_gap < fit.MAX_DISAGREEMENT


# ── 照合に使う行 ──────────────────────────────────────────────
def test_the_verification_blocks_add_the_branches_real_data_misses() -> None:
    """**全欠損・全 0・未知のカテゴリ**を、実データの後ろに足す。"""
    values = _values(50)
    real, missing, zero, unseen = fit.verification_blocks(values)
    assert [len(one) for one in (real, missing, zero, unseen)] == [50, 50, 50, 50]

    assert bool(np.isnan(missing).all()), "全欠損の行が入っていません"
    assert not zero.any(), "全 0 の行が入っていません"
    categorical = list(matrix.categorical_indices())
    assert (unseen[:, categorical] == fit.UNSEEN_CATEGORY).all(), "未知のカテゴリが入っていません"


def test_the_blocks_are_not_concatenated() -> None:
    """**つなげない。** つなげると実データぶんの写しができる（§12 の 168）。

    実データのブロックは**渡した配列そのもの**（写しでない）でなければならない。
    """
    values = _values(50)
    real = fit.verification_blocks(values)[0]
    assert real is values, "実データが写されています"


def test_the_unseen_rows_keep_the_numeric_columns() -> None:
    """**未知のカテゴリの行は、カテゴリ列だけ差し替える。** 数値の枝も一緒に通したい。"""
    values = _values(20)
    unseen = fit.verification_blocks(values)[3]
    numeric = [one for one in range(N_COLUMNS) if one not in matrix.categorical_indices()]
    assert np.array_equal(unseen[:, numeric], values[:, numeric], equal_nan=True)


def test_the_stress_rows_are_capped_by_the_real_rows() -> None:
    """実データが `STRESS_ROWS` より多ければ、仕込みは `STRESS_ROWS` 行ずつ。"""
    values = _features(fit.STRESS_ROWS + 10, 9)
    blocks = fit.verification_blocks(values)
    assert [len(one) for one in blocks] == [fit.STRESS_ROWS + 10, *([fit.STRESS_ROWS] * 3)]


# ── 本物の LightGBM で育てる ──────────────────────────────────
def _dataset(rows: int = 4_000) -> lgb.Dataset:
    """**本番と同じ列・カテゴリの位置**の小さな `Dataset`（`test_model_artifact` と同じ作り）。"""
    values = _features(rows, 20260927)
    label = (np.nan_to_num(values[:, 3]) > 0).astype(np.float64)
    return lgb.Dataset(
        values,
        label=label,
        feature_name=list(matrix.MODEL_COLUMNS),
        categorical_feature=list(matrix.categorical_indices()),
        free_raw_data=False,
    )


def test_a_candidate_grows_its_own_trees_and_rate() -> None:
    """**本数と学習率は候補が決める**（契約 37）。`PARAMS` には学習率を置かない。"""
    one = Candidate(7, 0.3)
    booster = fit.train_one(_dataset(), TARGETS[0], one)
    assert booster.num_trees() == one.trees
    assert booster.params["learning_rate"] == one.learning_rate
    assert "learning_rate" not in fit.PARAMS


def test_the_monotone_constraints_follow_the_target() -> None:
    """**単調制約はターゲットごと**（当てる側の台数に対して非減少）。"""
    for target in TARGETS:
        params = fit.params_for(CANDIDATE, target)
        assert params["monotone_constraints"] == list(matrix.monotone_constraints(target.name))


def test_grow_checks_the_forest_and_keeps_the_importance() -> None:
    """**育てた森は照合を通り、重要度は gain の大きい順**（木を捨てる前に取る）。"""
    one = Candidate(5, 0.3)
    grown = fit.grow(_dataset(), TARGETS[0], one, [_values(200)])
    assert len(grown.flat) == one.trees
    assert grown.check.max_gap < fit.MAX_DISAGREEMENT
    gains = [item.gain for item in grown.importance]
    assert gains and gains == sorted(gains, reverse=True)
    assert {item.column for item in grown.importance} <= set(matrix.MODEL_COLUMNS)


# ── 指定（W6 の PR F）─────────────────────────────────────────
BASE: Final[list[str]] = ["--from", "2026-09-07", "--to", "2026-09-09"]


def _request(*extra: str) -> fit.Request:
    return fit.request_of(fit._arguments([*BASE, *extra]))


def test_the_window_can_be_counted_back_from_the_last_day() -> None:
    """**`--train-days 28` は「学習 28・パージ 1・検証 2」が `--to` で終わる窓**（W6-14）。"""
    options = fit._arguments(
        ["--to", "2026-10-08", "--train-days", "28", "--eval-days", "2", "--purge-days", "1"]
    )
    days = fit.request_of(options).days
    assert (days[0], days[-1], len(days)) == (date(2026, 9, 8), date(2026, 10, 8), 31)


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["--to", "2026-09-09"], id="始まりが無い"),
        pytest.param([*BASE, "--train-days", "3"], id="始まりが 2 つ"),
        pytest.param(["--to", "2026-09-09", "--train-days", "0"], id="学習 0 日"),
        pytest.param(["--from", "2026-09-10", "--to", "2026-09-09"], id="始まりが後"),
        pytest.param(["--from", "2026/09/07", "--to", "2026-09-09"], id="日付の書き方"),
        pytest.param([*BASE, "--candidates", "100"], id="候補の書き方"),
        pytest.param(
            [*BASE, "--candidates", "50:0.15,100:0.15", "--choose", "300:0.05"],
            id="選んだ候補が並びに無い",
        ),
        pytest.param([*BASE, "--register", "--card", "c.md"], id="置かずに登録"),
        pytest.param([*BASE, "--upload", "--register"], id="カード無しで登録"),
        pytest.param(
            [*BASE, "--candidates", "50:0.15,100:0.15", "--upload"], id="2 つから選ばずに置く"
        ),
        pytest.param([*BASE, "--label", "Rehearsal"], id="印に大文字"),
        pytest.param([*BASE, "--label", "re-hearsal"], id="印にハイフン"),
        pytest.param([*BASE, "--label", ""], id="空の印"),
    ],
)
def test_a_wrong_request_stops_before_anything_is_opened(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """**当てはめる前に止まり、Storage も開かない**（終了コード 2）。

    28 日の当てはめは Actions で数十分かかる。**書き間違いに気づくのが最後では遅い。**
    """

    def must_not_open(config: object) -> object:
        raise AssertionError("指定が違うのに Storage を開いた")

    monkeypatch.setattr(fit, "open_storage", must_not_open)
    monkeypatch.setattr(fit, "read_storage_config", lambda: None)
    assert fit.run(argv) == 2
    assert "指定が違います" in capsys.readouterr().err


def test_one_candidate_is_chosen_without_asking() -> None:
    """**候補が 1 つならそれを置く**（`--choose` を書かせない）。2 つ以上なら人が選ぶ。"""
    assert _request().chosen == CANDIDATE
    assert _request("--candidates", "50:0.15,100:0.15").chosen is None
    two = _request("--candidates", "50:0.15,100:0.15", "--choose", "100:0.15")
    assert two.chosen == Candidate(100, 0.15)


def test_a_comparison_run_may_ask_only_for_the_report() -> None:
    """**選ばずに並べるだけ**の回は、置かず登録せず、報告書だけを書く（10/8 の比較）。"""
    request = _request("--candidates", "50:0.15,100:0.15,300:0.05", "--report", "r.md")
    assert request.chosen is None
    assert (request.upload, request.register) == (False, False)


def test_the_label_goes_between_the_prefix_and_the_day() -> None:
    """**予行演習の版を本物の v1 と取り違えない**（`lgbm-v1-rehearsal-…`）。"""
    days = (date(2026, 10, 4), date(2026, 10, 5))
    assert fit.model_version_for(days) == "lgbm-v1-20261005"
    assert fit.model_version_for(days, "rehearsal") == "lgbm-v1-rehearsal-20261005"


# ── 門の相手の B3（W6-10、契約 36）──────────────────────────────
def _calendar(first: date, count: int) -> tuple[date, ...]:
    return tuple(first + timedelta(days=offset) for offset in range(count))


def test_the_reference_b3_is_fitted_on_the_last_seven_days() -> None:
    """**本番の B3 は「昨日までの 7 日」**（`fit_baseline.DEFAULT_TRAIN_DAYS`）。28 日の窓でも。"""
    fit_days = _calendar(date(2026, 9, 8), 28)
    assert fit.baseline_days(fit_days) == fit_days[-7:]


def test_a_missing_day_is_not_replaced_by_an_older_one() -> None:
    """**暦日で数える**（本番の `fit_baseline` と同じ）。欠けた日の代わりに 8 日前を足さない。"""
    fit_days = tuple(day for day in _calendar(date(2026, 9, 8), 10) if day != date(2026, 9, 15))
    chosen = fit.baseline_days(fit_days)
    assert len(chosen) == 6
    assert chosen[0] == date(2026, 9, 11)


def test_a_short_window_gives_all_its_days() -> None:
    assert fit.baseline_days(DAYS[:2]) == DAYS[:2]


# ── 当てはめの結果（小さいが本物の形）─────────────────────────
def _outcome(split: DaySplit = SPLIT) -> tuple[harness.Outcome, Samples]:
    """3 日 × 2 系統 × 2 水平 × 台数の違う 3 ポート。**候補は 0.8 と出す。**"""
    rows = [
        eval_rows.row(day, system, station, horizon, bikes, 9 - bikes, 1 if bikes else 0, 1)
        for day in DAYS
        for system in ("hellocycling", "docomo-cycle")
        for horizon in (5, 60)
        for station, bikes in (("a", 0), ("b", 1), ("c", 7))
    ]
    samples = to_samples(eval_rows.to_table(rows))
    evaluated = samples.take(mask_of(samples, split.evaluate))
    guess = {name: np.full(len(evaluated), 0.8) for name in ("bike", "dock")}
    outcome = harness.run(samples, split, {CANDIDATE.name: guess}, climate=FromSamples())
    return outcome, evaluated


CHECKS: Final[dict[str, fit.Checked]] = {
    "bike": fit.Checked(n_rows=1, max_gap=0.0),
    "dock": fit.Checked(n_rows=1, max_gap=0.0),
}

FACTS: Final[ForestFacts] = ForestFacts(
    trees={"bike": 12, "dock": 12},
    nodes={"bike": 100, "dock": 100},
    depth={"bike": 5, "dock": 5},
    row_tree_seconds={"hellocycling": 2.2e-7},
    importance={"bike": (Importance("bikes", 3.0, 0.75),), "dock": ()},
)

#: 読んだ日ごとの素性（**中身は何でもよい**。形が登録簿とカードに届くかを見る）。
PRINTS: Final[dict[date, Fingerprint]] = {
    day: Fingerprint(rows=ROWS, sha256=f"{index:x}" * 64) for index, day in enumerate(DAYS)
}


def _weather(*ratios: float) -> dict[date, coverage.Coverage]:
    """3 日ぶんの被覆。**行数は同じで割合だけ変える**（差が被覆だけになる）。"""
    return {
        day: coverage.Coverage(
            rows=ROWS,
            covered=round(ROWS * ratio),
            by_column=dict.fromkeys(WEATHER_COLUMNS, round(ROWS * ratio)),
        )
        for day, ratio in zip(DAYS, ratios, strict=True)
    }


def _by_day(*ratios: float) -> dict[str, object]:
    """登録簿に入る `by_day` の期待値。"""
    return {day.isoformat(): one.as_dict() for day, one in _weather(*ratios).items()}


def _fitted(*ratios: float) -> fit.Fitted:
    """当てはめの結果ひとそろい（**森は小さく、指標と天気は本物の形**）。"""
    outcome, evaluated = _outcome()
    summary = candidates.summarize(CANDIDATE, outcome, evaluated, SPLIT.evaluate)
    return fit.Fitted(
        fits={CANDIDATE: fit.CandidateFit(CANDIDATE, ARTIFACT.forests, CHECKS, FACTS)},
        outcome=outcome,
        summaries=(summary,),
        weather=_weather(*ratios),
        data=PRINTS,
    )


def _chosen(fitted: fit.Fitted) -> fit.Chosen:
    return fit.choose(fitted, CANDIDATE, VERSION)


# ── 森と門の表は組（契約 35）──────────────────────────────────
def test_the_gate_table_is_bound_to_the_forest_it_was_decided_for() -> None:
    """**表は、置く森のバイト列の SHA-256 を持つ。** 置き直した森と前の表を組にしない。"""
    chosen = _chosen(_fitted(1.0, 1.0, 1.0))
    table = gate_tables.from_bytes(chosen.gates_body)
    gate_tables.refuse_other_forest(table, chosen.body)
    assert table.artifact_sha256 == hashlib.sha256(chosen.body).hexdigest()
    assert (table.model_version, table.evaluate_days) == (VERSION, (DAYS[2].isoformat(),))


def test_the_gate_table_follows_the_judgement_of_the_chosen_candidate() -> None:
    """**門の表のセルは、選んだ候補を検証日で測った結果そのもの**（契約 35）。"""
    fitted = _fitted(1.0, 1.0, 1.0)
    table = _chosen(fitted).gates
    judged = fitted.summary_of(CANDIDATE).judged
    assert len(table.cells) == len(judged.cells)
    passed = {cell.key for cell in table.cells if cell.reason == gate_tables.PASSED}
    assert passed == table.lightgbm_cells()


def test_the_artifact_records_the_chosen_pair() -> None:
    """**成果物の `params` に本数と学習率の組が残る**（契約 37）。"""
    artifact = _chosen(_fitted(1.0, 1.0, 1.0)).artifact
    assert artifact.params["num_boost_round"] == CANDIDATE.trees
    assert artifact.params["learning_rate"] == CANDIDATE.learning_rate
    assert artifact.train_days == (DAYS[0].isoformat(),)


# ── 登録に残るもの ────────────────────────────────────────────
def test_the_check_is_recorded_for_the_registry() -> None:
    """**照合の結果が `model_versions.metrics` に入る。** 後から読める事実にする。"""
    rows = fit._to_check_rows({TARGET: fit.Checked(n_rows=1234, max_gap=1e-15)})
    assert rows["tolerance"] == fit.MAX_DISAGREEMENT
    assert rows["targets"] == {TARGET: {"n_rows": 1234, "max_gap": 1e-15}}


def test_the_registration_normalises_the_card_path(tmp_path: Path) -> None:
    """**登録する形が `card_reference` を通っている。**

    `card_reference` を直接試すだけでは、**呼び出し側が使うのをやめても気づけない**
    （実際に 1 度素通りした）。ここは `to_registration` の出力そのものを見る。
    """
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs").mkdir()
    card = f"{tmp_path}/apps/ml/../../docs/card.md"
    fitted = _fitted(1.0, 1.0, 1.0)

    row = fit.to_registration(fitted, _chosen(fitted), card)

    assert row["card_path"] == "docs/card.md"


def test_the_registration_can_be_sent_as_json() -> None:
    """**登録する形がそのまま JSON にできる。** numpy の値が紛れると、**森を置いた後の
    登録で落ちる**（`np.bool_` は JSON にできない。ECE の合否で実際に起こりえた）。

    **NaN も許さない**（`allow_nan=False`）。Python の `json` は既定で `NaN` を書くが、
    **Postgres の jsonb は受けない**——これも置いた後の登録で落ちる。
    """
    fitted = _fitted(1.0, 1.0, 1.0)
    row = fit.to_registration(fitted, _chosen(fitted), None)
    assert json.loads(json.dumps(row, allow_nan=False)) == row


def test_the_metrics_carry_what_v1_is_judged_by() -> None:
    """**v1 の判断に使う数字が登録簿に入る**：候補・門の相手・門・配る形・意思決定・データ。"""
    fitted = _fitted(1.0, 1.0, 1.0)
    chosen = _chosen(fitted)
    metrics = fit.to_metrics(fitted, chosen)
    assert metrics["candidate"] == {"trees": CANDIDATE.trees, "learning_rate": 0.05}
    assert metrics["baseline"] == {
        "days": [DAYS[0].isoformat()],
        "n_fit": fitted.outcome.n_baseline_fit,
    }
    gates_row = metrics["gates"]
    assert isinstance(gates_row, dict)
    assert gates_row["path"] == f"lightgbm/{VERSION}.gates.json.gz"
    assert gates_row["sha256"] == hashlib.sha256(chosen.gates_body).hexdigest()
    assert gates_row["artifact_sha256"] == hashlib.sha256(chosen.body).hexdigest()
    decision = metrics["decision"]
    assert isinstance(decision, dict)
    assert set(decision) == {
        f"{system}/{target}"
        for system in ("docomo-cycle", "hellocycling")
        for target in ("bike", "dock")
    }
    assert metrics["data"] == {
        "by_day": {
            day.isoformat(): {"rows": ROWS, "sha256": one.sha256} for day, one in PRINTS.items()
        }
    }


def test_the_brier_rows_name_the_chosen_candidate() -> None:
    """**全体の Brier は選んだ候補の列から取る**（候補の名前が表の列の名前）。"""
    fitted = _fitted(1.0, 1.0, 1.0)
    rows = fit._to_brier_rows(fitted.outcome, CANDIDATE.name)
    first = next(iter(rows.values()))
    assert isinstance(first, dict)
    assert set(first) == {"n", "b3", "lgbm"}


# ── 天気の門（W4 プラン §8.5.3、PR I）─────────────────────────
def _window(*ratios: float) -> Window:
    """`prepare` に渡す形。**中身は小さいが本物**（`samples_of` が実際に開く）。

    **日ごとに 1 つ**置く。窓は表を持たないので、渡すのは**その日の Parquet**である。
    """
    tables = {
        day: eval_rows.to_table(
            [
                eval_rows.row(day, "hellocycling", station, horizon, 5, 5, 1, 1)
                for station in ("a", "b")
                for horizon in (5, 60)
            ]
        )
        for day in DAYS
    }
    return Window(
        days=DAYS,
        rows={day: table.num_rows for day, table in tables.items()},
        weather=_weather(*ratios),
        bodies={day: to_parquet_bytes(table) for day, table in tables.items()},
    )


def test_a_mixed_period_stops_before_fitting() -> None:
    """**完了条件**（W4 プラン §6.8 の PR I）。

    09-07（被覆 0%）と 09-09（被覆 100%）を一緒に渡すと、**当てはめる前に**止まる。
    """
    with pytest.raises(coverage.MixedWeatherError, match=r"100\.00 ポイント"):
        fit.prepare(_window(0.0, 1.0, 1.0), eval_days=1, purge_days=1, allow_mixed_weather=False)


def test_the_flag_lets_a_mixed_period_through() -> None:
    """**承知のうえなら通す。** 通したことは登録簿に残る（下の検査）。"""
    samples, split = fit.prepare(
        _window(0.0, 1.0, 1.0), eval_days=1, purge_days=1, allow_mixed_weather=True
    )
    assert split.fit == (DAYS[0],)
    assert len(samples) > 0


def test_the_gate_stops_before_any_table_is_opened() -> None:
    """**止まるときは表を 1 つも開いていない**（§12 の 168 の 2 段目）。

    窓の中身を**開けないバイト列**にしておく。門が先に通れば天気の例外で止まり、
    順番が入れ替われば**その前に Parquet を開こうとして別の例外**になる。
    「止まること」だけを見る検査では、この入れ替えは素通りした。
    """
    broken = replace(_window(0.0, 1.0, 1.0), bodies=dict.fromkeys(DAYS, b"this is not parquet"))
    with pytest.raises(coverage.MixedWeatherError):
        fit.prepare(broken, eval_days=1, purge_days=1, allow_mixed_weather=False)


def test_a_day_that_is_only_purged_does_not_stop_the_fit() -> None:
    """**パージ日は読むが捨てる**ので、そこだけ天気が無くても止めない。

    ここが素通りするようだと、門は「読んだ日ぜんぶ」を見ていることになり、
    **鳴かなくてよいところで鳴く**。
    """
    fit.prepare(_window(1.0, 0.0, 1.0), eval_days=1, purge_days=1, allow_mixed_weather=False)


def test_a_period_with_the_same_weather_passes() -> None:
    fit.prepare(_window(1.0, 1.0, 1.0), eval_days=1, purge_days=1, allow_mixed_weather=False)


def _local_run(monkeypatch: pytest.MonkeyPatch, shelf: object = None) -> None:
    """入り口から走らせる準備。**Storage は開かず**（`--local`）、登録簿は `shelf` が代わる。"""
    monkeypatch.setattr(fit, "read_storage_config", lambda: None)
    monkeypatch.setattr(fit, "open_storage", lambda config: nullcontext(shelf))


def test_the_gate_is_reached_from_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**`run` が門を通っていること。**

    `prepare` を直に試すだけでは、**`run` が呼ぶのをやめても気づけない**（PR E′ で
    1 度そうなった）。ここは入り口から入れて、**当てはめに入る前に**止まるのを見る。
    3 日ぶんの `features/` を置き、学習日だけ天気を NULL にしてある。
    """
    root = write_samples(tmp_path, {DAYS[0]: False, DAYS[1]: True, DAYS[2]: True})
    _local_run(monkeypatch)

    with pytest.raises(coverage.MixedWeatherError):
        fit.run(["--from", f"{DAYS[0]}", "--to", f"{DAYS[2]}", "--local", str(root)])


def test_a_window_without_its_last_day_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**`--to` の日が無ければ当てはめない。** 飛ばすと検証日も版の名前も 1 日ずれる。

    10/9 の朝に 10-08 の学習サンプルができる前に回すと、**1 日短い v1 が本物の名前で
    置かれる**（W6 プラン §8.7）。
    """
    root = write_samples(tmp_path, dict.fromkeys(DAYS[:2], True))
    _local_run(monkeypatch)

    def must_not_fit(*args: object) -> fit.Fitted:
        raise AssertionError("当てはめに入った")

    monkeypatch.setattr(fit, "fit_and_score", must_not_fit)
    with pytest.raises(fit.MissingLastDayError, match=f"{DAYS[2]}"):
        fit.run(["--from", f"{DAYS[0]}", "--to", f"{DAYS[2]}", "--local", str(root)])


def test_a_missing_day_inside_the_window_is_told_and_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """**途中の欠けは飛ばす**（収集の欠損は補わない。CLAUDE.md §6）。**黙っては飛ばさない。**"""
    root = write_samples(tmp_path, {DAYS[0]: True, DAYS[2]: True})
    opened = read_local(root, DAYS)
    fit.refuse_missing_end(opened, DAYS)
    assert f"{DAYS[1]}" in capsys.readouterr().err


def test_the_fit_matrix_is_built_once_for_all_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**学習の行列は 1 度だけ、`Dataset` はターゲットごとに 1 つ**（候補が 2 つでも）。

    ターゲットで違うのはラベルと重みだけ、候補で違うのは本数と学習率だけである。
    作り直すと、**28 日ぶんで 4 GB の組み立て**やビン分けを何度も払う（§12 の 168）。
    **答えは変わらないので、数えないと気づけない。**

    **日ごとに行数を変える。** 同じ行数の日を並べると、**検証と学習を取り違えても
    同じ数**になり、確保の回数しか見られない（故意に取り違えたら素通りした）。
    """
    opened = read_local(write_distinct_sizes(tmp_path), DAYS)
    samples, split = fit.prepare(opened, eval_days=1, purge_days=1, allow_mixed_weather=False)
    reserved: list[int] = []
    datasets: list[str] = []
    grown: list[tuple[str, Candidate]] = []
    original = matrix.empty

    def counted(rows: int) -> matrix.Matrix:
        reserved.append(rows)
        return original(rows)

    def dataset(built: matrix.Matrix, samples: Samples, mask: object, target: object) -> str:
        datasets.append(getattr(target, "name", ""))
        return "dataset"

    def growing(data: object, target: object, one: Candidate, blocks: object) -> fit.Grown:
        grown.append((getattr(target, "name", ""), one))
        return fit.Grown(ARTIFACT.forests["bike"], fit.Checked(0, 0.0), ())

    monkeypatch.setattr(matrix, "empty", counted)
    monkeypatch.setattr(fit, "dataset_for", dataset)
    monkeypatch.setattr(fit, "grow", growing)
    monkeypatch.setattr(fit, "row_tree_seconds", lambda *args: {})
    monkeypatch.setattr(harness, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(candidates, "summarize", lambda *args: None)

    pair = (Candidate(50, 0.15), Candidate(100, 0.15))
    fit.fit_and_score(opened, samples, split, FromSamples(), pair)
    assert reserved == [opened.rows[split.evaluate[0]], opened.rows[split.fit[0]]], (
        f"行列を {len(reserved)} 回確保しています（検証と学習で 2 回のはず）"
    )
    assert datasets == ["bike", "dock"], "Dataset はターゲットごとに 1 つのはず"
    assert grown == [(target, one) for target in ("bike", "dock") for one in pair]


def test_the_reference_b3_reaches_the_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**門の相手の B3 を当てはめる日が `harness.run` まで届く**（W6-10、契約 36）。

    `baseline_days` を直に試すだけでは、**呼ぶのをやめても気づけない**。
    ここは 10 日の窓（学習 8 日）で `fit_and_score` を通し、渡った日を見る。
    """
    days = _calendar(date(2026, 9, 7), 10)
    opened = read_local(write_samples(tmp_path, dict.fromkeys(days, True)), days)
    samples, split = fit.prepare(opened, eval_days=1, purge_days=1, allow_mixed_weather=False)
    seen: list[tuple[date, ...] | None] = []
    original = harness.run

    def spy(
        samples: Samples,
        split: DaySplit,
        extra: harness.ExtraModels | None = None,
        *,
        climate: climatology.Source,
        baseline_days: Sequence[date] | None = None,
    ) -> harness.Outcome:
        seen.append(None if baseline_days is None else tuple(baseline_days))
        return original(samples, split, extra, climate=climate, baseline_days=baseline_days)

    same = fit.Grown(ARTIFACT.forests["bike"], fit.Checked(0, 0.0), ())
    monkeypatch.setattr(fit, "grow", lambda *args: same)
    monkeypatch.setattr(fit, "dataset_for", lambda *args: None)
    monkeypatch.setattr(harness, "run", spy)
    fitted = fit.fit_and_score(opened, samples, split, FromSamples(), (CANDIDATE,))
    assert seen == [split.fit[-7:]]
    assert fitted.outcome.baseline_days == split.fit[-7:]


# ── 気候値の作り方（§12 の 166）──────────────────────────────
def test_the_climate_source_has_no_default() -> None:
    """**`harness.run` も `fit_and_score` も、B2 の作り方を既定値で決めない。**

    既定値が在ったときに `fit_lightgbm` が渡し忘れ、**本番より弱いベースラインと
    比べていた**——例外も警告も出ず、数字だけが静かに変わった。**渡さなければ
    型検査で止まる**状態にしてある。
    """
    assert inspect.signature(harness.run).parameters["climate"].default is inspect.Parameter.empty
    assert (
        inspect.signature(fit.fit_and_score).parameters["climate_source"].default
        is inspect.Parameter.empty
    )


def test_the_run_reads_the_profiles_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**既定はプロファイル**（本番と同じ）。**門の相手の B3 を当てはめる日だけ**を渡す。

    **決めたものが当てはめに届くところまで留める。** 「呼んでいるが使っていない」が
    通ると §12 の 166 はそのまま戻る——`source_for` を呼んだうえで結果を捨て、
    `FromSamples()` を渡せば、**数字は壊れたまま検査は緑になる**。

    **10 日の窓（学習 8 日）で見る。** 3 日の窓だと学習日が 1 日しか無く、「学習の日
    ぜんぶ」と「最後の 7 日」を取り違えても同じ日が渡る。
    """
    days = _calendar(date(2026, 9, 7), 10)
    root = write_samples(tmp_path, dict.fromkeys(days, True))
    _local_run(monkeypatch)
    asked: list[tuple[tuple[date, ...], bool]] = []
    decided = FromSamples()
    handed: list[object] = []

    def spy(
        source: SupabaseIo | None,
        days: Sequence[date],
        local: Path | None,
        ports: Sequence[str],
        *,
        from_profiles: bool,
    ) -> climatology.Source:
        asked.append((tuple(days), from_profiles))
        return decided

    def fitting(
        loaded: Window,
        samples: Samples,
        split: DaySplit,
        climate_source: climatology.Source,
        wanted: Sequence[Candidate],
    ) -> fit.Fitted:
        handed.append(climate_source)
        return _fitted(1.0, 1.0, 1.0)

    monkeypatch.setattr(climate_module, "source_for", spy)
    monkeypatch.setattr(fit, "fit_and_score", fitting)

    fit.run(["--from", f"{days[0]}", "--to", f"{days[-1]}", "--local", str(root)])
    assert asked, "気候値の作り方を決めていません"
    chosen_days, from_profiles = asked[0]
    assert from_profiles is True, "既定でプロファイルを読んでいません"
    assert chosen_days == days[1:8], (
        "学習の最後の 7 日だけを渡していません（検証日や古い日が混ざる）"
    )
    assert handed and handed[0] is decided, "決めた作り方を当てはめに渡していません"


def test_the_no_profile_flag_falls_back_to_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**逃げ道が届いていること。** 2026-09-19 より前の記録と比べるために要る。"""
    root = write_samples(tmp_path, dict.fromkeys(DAYS, True))
    _local_run(monkeypatch)
    asked: list[bool] = []

    def spy(
        source: SupabaseIo | None,
        days: Sequence[date],
        local: Path | None,
        ports: Sequence[str],
        *,
        from_profiles: bool,
    ) -> climatology.Source:
        asked.append(from_profiles)
        return FromSamples()

    monkeypatch.setattr(climate_module, "source_for", spy)
    monkeypatch.setattr(fit, "fit_and_score", lambda *args: _fitted(1.0, 1.0, 1.0))

    fit.run(["--from", f"{DAYS[0]}", "--to", f"{DAYS[2]}", "--local", str(root), "--no-profile"])
    assert asked == [False]
    assert fit._arguments(["--from", "2026-09-07", "--to", "2026-09-09"]).no_profile is False


def test_the_registry_records_how_b2_was_made() -> None:
    """**何を相手に測ったかを残す。** `feature_set` は B2 の作り方を語らない。

    同じ `v3` でも、B2 を学習サンプルから作ったかプロファイルから作ったかで
    **門の判定が変わる**（学習 3 日で 2 → 0。§12 の 166）。
    """
    fitted = _fitted(1.0, 1.0, 1.0)
    assert fit.to_metrics(fitted, _chosen(fitted))["climate"] == fitted.outcome.climate


def test_the_flag_is_on_the_command_line() -> None:
    """**逃げ道が届いていること。** 門が在っても呼べなければ運用が止まる。"""
    assert fit._arguments(BASE).allow_mixed_weather is False
    assert fit._arguments([*BASE, "--allow-mixed-weather"]).allow_mixed_weather is True


# ── 登録簿に残る天気 ──────────────────────────────────────────
def test_the_weather_coverage_is_recorded_for_the_registry() -> None:
    """**通した事実は消えない。** `spread_pp` が `max_spread_pp` を超えていれば、
    それは `--allow-mixed-weather` で通したということである（別の印は持たない）。"""
    assert fit._to_weather_rows(_fitted(0.0, 1.0, 1.0)) == {
        "max_spread_pp": coverage.MAX_SPREAD_PP,
        "spread_pp": 100.0,
        "by_day": _by_day(0.0, 1.0, 1.0),
    }


def test_the_purge_day_is_listed_but_not_counted() -> None:
    """**3 日ぶん残すが、差は学習日と検証日だけで測る。**"""
    rows = fit._to_weather_rows(_fitted(1.0, 0.0, 1.0))
    assert rows == {
        "max_spread_pp": coverage.MAX_SPREAD_PP,
        "spread_pp": 0.0,
        "by_day": _by_day(1.0, 0.0, 1.0),
    }


def test_the_metrics_carry_the_weather() -> None:
    """**呼び出し側を見る。** `_to_weather_rows` だけ試すと、使うのをやめても通る。"""
    fitted = _fitted(0.0, 1.0, 1.0)
    assert fit.to_metrics(fitted, _chosen(fitted))["weather"] == fit._to_weather_rows(fitted)


def test_the_registration_carries_the_metrics() -> None:
    fitted = _fitted(1.0, 1.0, 1.0)
    chosen = _chosen(fitted)
    assert fit.to_registration(fitted, chosen, None)["metrics"] == fit.to_metrics(fitted, chosen)


# ── モデルカード ──────────────────────────────────────────────
def _card(*ratios: float) -> str:
    fitted = _fitted(*ratios)
    return fit.render_card(fitted, _chosen(fitted))


def test_the_card_shows_the_measured_coverage() -> None:
    """**カードは日付を決め打ちしない。** 測った数を日ごとに出す。"""
    text = _card(1.0, 0.6, 1.0)
    assert "## 学習に使ったデータ" in text
    assert "| 2026-09-07 | 学習 | 1,000 | 100.000% |" in text
    assert "| 2026-09-08 | パージ | 1,000 | 60.000% |" in text
    assert "| 2026-09-09 | 検証 | 1,000 | 100.000% |" in text
    assert "**天気の被覆は揃っている**（学習日と検証日の差 0.00 ポイント）" in text


def test_the_card_says_when_the_gate_was_forced() -> None:
    """**限界の節に、通したことが出る。** 読む人が後から判断できる。"""
    text = _card(0.0, 1.0, 1.0)
    assert "**天気の被覆が学習日と検証日で 100.00 ポイント違う**" in text
    assert "--allow-mixed-weather" in text


def test_the_card_carries_the_data_hash_and_the_gates() -> None:
    """**v1 のカードに足したもの**：データの SHA-256（W6-17）・門・意思決定の指標（W6-16）。"""
    text = _card(1.0, 1.0, 1.0)
    for day, one in PRINTS.items():
        assert f"| {day} | " in text
        assert f"`{one.sha256}`" in text, "SHA-256 を縮めずに出していません"
    assert "## 門（セル単位" in text
    assert f"lightgbm/{VERSION}.gates.json.gz" in text
    assert "## 意思決定の指標" in text
    assert "precision@0.9" in text


def test_the_card_does_not_tell_to_serve_lightgbm_alone() -> None:
    """**LightGBM 単体を active にしない**（契約 33）。カードの使い方もそう書く。"""
    text = _card(1.0, 1.0, 1.0)
    assert "**LightGBM 単体を active にしない**" in text
    assert "'Authorization: Bearer $CRON_SECRET'" not in text, "単引用符では変数が展開されない"
    assert '"Authorization: Bearer $CRON_SECRET"' in text


def test_the_registration_can_only_ask_for_candidate() -> None:
    """**`register_model_version` は candidate しか受け付けない。** 送る側も揃える。"""
    fitted = _fitted(1.0, 1.0, 1.0)
    row = fit.to_registration(fitted, _chosen(fitted), None)
    assert row["status"] == "candidate"
    assert row["card_path"] is None
    assert row["artifact_path"] == f"lightgbm/{VERSION}.json.gz"


# ── 書き出す ──────────────────────────────────────────────────
def _made(*, chosen: bool) -> fit.Made:
    fitted = _fitted(1.0, 1.0, 1.0)
    return fit.Made(fitted, VERSION, _chosen(fitted) if chosen else None)


def test_the_chosen_candidate_is_written_under_its_version(tmp_path: Path) -> None:
    """**`{version}` は版の名前になり、成果物・門の表・カード・報告書がそろう。**"""
    outputs = fit.Outputs(
        out=f"{tmp_path}/{{version}}.json.gz",
        gates=f"{tmp_path}/{{version}}.gates.json.gz",
        report=f"{tmp_path}/{{version}}.md",
        card=f"{tmp_path}/cards/{{version}}.md",
    )
    made = _made(chosen=True)
    fit.write_outputs(outputs, made)
    chosen = made.chosen
    assert chosen is not None
    assert (tmp_path / f"{VERSION}.json.gz").read_bytes() == chosen.body
    assert (tmp_path / f"{VERSION}.gates.json.gz").read_bytes() == chosen.gates_body
    assert (
        (tmp_path / "cards" / f"{VERSION}.md")
        .read_text(encoding="utf-8")
        .startswith(f"# モデルカード：{VERSION}")
    )
    assert "## 12. 候補の比較" in (tmp_path / f"{VERSION}.md").read_text(encoding="utf-8")


def test_without_a_choice_only_the_report_is_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """**選んでいなければ、候補ごとのもの（成果物・門の表・カード）は書かない。** 言う。"""
    outputs = fit.Outputs(
        out=f"{tmp_path}/a.json.gz",
        gates=f"{tmp_path}/g.json.gz",
        report=f"{tmp_path}/r.md",
        card=f"{tmp_path}/c.md",
    )
    fit.write_outputs(outputs, _made(chosen=False))
    assert sorted(path.name for path in tmp_path.iterdir()) == ["r.md"]
    assert "書きません" in capsys.readouterr().err
    assert "**選んでいない**" in (tmp_path / "r.md").read_text(encoding="utf-8")


def test_the_gate_table_file_is_gzip_json() -> None:
    """**`models` バケットは gzip しか受けない**（0027）。表も gzip した JSON で置く。"""
    chosen = _chosen(_fitted(1.0, 1.0, 1.0))
    assert json.loads(gzip.decompress(chosen.gates_body))["kind"] == gate_tables.KIND


# ── ⑦ 1 行 1 木の秒 ──────────────────────────────────────────
def test_the_row_tree_seconds_divide_by_rows_and_trees(monkeypatch: pytest.MonkeyPatch) -> None:
    """**最小の秒 ÷（行 × 2 ターゲットの木の和）**。検証の行が無い系統は出さない。"""
    _, evaluated = _outcome()
    ticks = iter([0.0, 3.0, 10.0, 12.0, 20.0, 25.0])
    # **このモジュールの時計だけ**を差し替える（`time` そのものを替えると pytest も巻き込む）
    monkeypatch.setattr(fit, "time", SimpleNamespace(perf_counter=lambda: next(ticks)))
    hello = evaluated.systems.index("hellocycling")
    only_hello = evaluated.take(np.asarray(evaluated.system == hello, dtype=np.bool_))
    values = _values(len(only_hello))
    found = fit.row_tree_seconds(ARTIFACT.forests, values, only_hello)
    trees = sum(len(one) for one in ARTIFACT.forests.values())
    assert set(found) == {"hellocycling"}, "行の無い系統を出しています"
    assert found["hellocycling"] == pytest.approx(2.0 / (len(only_hello) * trees))


# ── 上書きの防止（W6 の PR B、W6-19、契約 38）──────────────────
#: 天気の門と同じ 3 日で走らせたときの版の名前（学習日は 09-07 の 1 日）。
FIT_NAME: Final[str] = fit.model_version_for(SPLIT.fit)


@dataclass
class Shelf:
    """登録簿と `models` バケットの代役。**引いた名前・置いたもの・登録したものを覚える。**

    `promoted_from` 回目に引かれたときから、その名前を active と答える（当てはめの間の昇格）。
    """

    statuses: dict[str, str] = field(default_factory=dict)
    lookups: list[str] = field(default_factory=list)
    uploads: list[tuple[str, str, str]] = field(default_factory=list)
    registered: list[str] = field(default_factory=list)
    promoted_from: int | None = None

    def find_model(self, model_version: str) -> registry.Registered | None:
        self.lookups.append(model_version)
        promoted = self.promoted_from is not None and len(self.lookups) >= self.promoted_from
        status = "active" if promoted else self.statuses.get(model_version)
        if status is None:
            return None
        return registry.Registered(
            model_version=model_version,
            kind=registry.LIGHTGBM_KIND,
            feature_set=ARTIFACT.feature_set,
            artifact_path=lightgbm_artifact.artifact_path(model_version),
            status=status,
        )

    def upload(self, bucket: str, path: str, body: bytes, content_type: str) -> None:
        self.uploads.append((bucket, path, content_type))

    def register_model_version(self, row: dict[str, object]) -> str:
        self.registered.append(str(row["model_version"]))
        return str(row["model_version"])


def _run_with(shelf: Shelf, root: Path, monkeypatch: pytest.MonkeyPatch, *extra: str) -> int:
    """**入り口から**走らせる。当てはめは差し替える（見たいのは置く前後だけ）。"""
    _local_run(monkeypatch, shelf)
    argv = ["--from", f"{DAYS[0]}", "--to", f"{DAYS[2]}", "--local", str(root)]
    card = ["--card", str(root / "card-{version}.md")]
    return fit.run([*argv, "--upload", "--register", *card, *extra])


def _both_paths(version: str) -> list[tuple[str, str, str]]:
    return [
        (registry.MODEL_BUCKET, lightgbm_artifact.artifact_path(version), "application/gzip"),
        (registry.MODEL_BUCKET, gate_tables.gates_path(version), "application/gzip"),
    ]


@pytest.mark.parametrize("status", ["active", "shadow"])
def test_a_serving_name_stops_before_fitting(
    status: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**配信中の版と同じ名前なら、森を育てる前に止まる。** 置きも登録もしない。"""
    root = write_samples(tmp_path, dict.fromkeys(DAYS, True))
    shelf = Shelf(statuses={FIT_NAME: status})

    def must_not_fit(*args: object) -> fit.Fitted:
        raise AssertionError("当てはめに入った（止まるのは当てはめの前のはず）")

    monkeypatch.setattr(fit, "fit_and_score", must_not_fit)
    with pytest.raises(registry.ServingVersionError, match=status):
        _run_with(shelf, root, monkeypatch)
    assert shelf.lookups == [FIT_NAME]
    assert (shelf.uploads, shelf.registered) == ([], [])


def test_a_candidate_name_is_put_and_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**candidate は置き直せる。** 登録簿は 3 度引く（当てはめる前・森を置く直前・表を置く直前）。

    **森 → 門の表 → 登録の順。** カードは登録より先に手元へ書いてある（CLAUDE.md §6）。
    """
    root = write_samples(tmp_path, dict.fromkeys(DAYS, True))
    shelf = Shelf(statuses={FIT_NAME: "candidate"})
    monkeypatch.setattr(fit, "fit_and_score", lambda *args: _fitted(1.0, 1.0, 1.0))
    assert _run_with(shelf, root, monkeypatch) == 0
    assert shelf.lookups == [FIT_NAME] * 3
    assert shelf.uploads == _both_paths(FIT_NAME)
    assert shelf.registered == [FIT_NAME]
    assert (root / f"card-{FIT_NAME}.md").exists(), "登録したのにカードが無い"


def test_a_put_without_register_does_not_register(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**置くだけの回は登録しない**（予行演習の森を置いて、登録は確かめてから、ができる）。"""
    root = write_samples(tmp_path, dict.fromkeys(DAYS, True))
    shelf = Shelf()
    monkeypatch.setattr(fit, "fit_and_score", lambda *args: _fitted(1.0, 1.0, 1.0))
    _local_run(monkeypatch, shelf)
    argv = ["--from", f"{DAYS[0]}", "--to", f"{DAYS[2]}", "--local", str(root), "--upload"]
    assert fit.run(argv) == 0
    assert shelf.uploads == _both_paths(FIT_NAME)
    assert shelf.registered == []


def test_the_label_reaches_every_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**印は、引く名前・置く場所・登録する名前のすべてに入る**（予行演習の取り違えを防ぐ）。"""
    root = write_samples(tmp_path, dict.fromkeys(DAYS, True))
    shelf = Shelf()
    monkeypatch.setattr(fit, "fit_and_score", lambda *args: _fitted(1.0, 1.0, 1.0))
    _run_with(shelf, root, monkeypatch, "--label", "rehearsal")
    marked = fit.model_version_for(SPLIT.fit, "rehearsal")
    assert marked.startswith("lgbm-v1-rehearsal-")
    assert shelf.lookups == [marked] * 3
    assert shelf.uploads == _both_paths(marked)
    assert shelf.registered == [marked]


def test_a_name_promoted_during_the_fit_is_neither_put_nor_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**当てはめの間に昇格されたら、置く直前に止まる。** 登録にも進まない。"""
    root = write_samples(tmp_path, dict.fromkeys(DAYS, True))
    shelf = Shelf(promoted_from=2)
    monkeypatch.setattr(fit, "fit_and_score", lambda *args: _fitted(1.0, 1.0, 1.0))
    with pytest.raises(registry.ServingVersionError):
        _run_with(shelf, root, monkeypatch)
    assert len(shelf.lookups) == 2
    assert (shelf.uploads, shelf.registered) == ([], [])


def test_a_name_promoted_between_the_two_puts_is_not_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**森を置いた直後に昇格されたら、門の表は置かず、登録もしない。**

    置いた森は前の表と組にならない（表は森の SHA-256 を持つ。`refuse_other_forest`）。
    **カードは置く前に書いてある**——手元に書くのを登録の後に回すと、ここで止まった回に
    カードが残らない（登録したのにカードが無い形も作りうる。CLAUDE.md §6）。
    """
    root = write_samples(tmp_path, dict.fromkeys(DAYS, True))
    shelf = Shelf(promoted_from=3)
    monkeypatch.setattr(fit, "fit_and_score", lambda *args: _fitted(1.0, 1.0, 1.0))
    with pytest.raises(registry.ServingVersionError):
        _run_with(shelf, root, monkeypatch)
    assert shelf.uploads == _both_paths(FIT_NAME)[:1]
    assert shelf.registered == []
    assert (root / f"card-{FIT_NAME}.md").exists(), "カードを置く前に書いていない"
