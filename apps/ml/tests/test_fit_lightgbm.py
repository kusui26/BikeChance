"""当てはめの側の門（`jobs/fit_lightgbm.py`）。

**主題は「違う値を出す成果物が生まれ得ないこと」。** 配信は `lightgbm` を読み込まず
木を numpy で歩く（W4-19）。同じ値が出ることは**宣言ではなく検査**で担保する、と決めた
——その検査そのものが働いているかを、ここで確かめる。

**照合に足す「実データが踏まない枝」も見る。** 検証日の行だけでは全欠損・全 0・
未知のカテゴリを一度も通らないことがあり、**配信で最初に踏むのがその枝**では困る。
"""

from contextlib import nullcontext
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Final

import numpy as np
import pytest

from bikechance_ml.eval import harness
from bikechance_ml.eval.split import DaySplit
from bikechance_ml.features import coverage
from bikechance_ml.features.arrays import Float64
from bikechance_ml.features.schema import WEATHER_COLUMNS
from bikechance_ml.jobs import fit_lightgbm as fit
from bikechance_ml.jobs.evaluate_baselines import Loaded
from bikechance_ml.models import matrix
from tests import eval_fixture as eval_rows
from tests.test_evaluate_baselines import write_samples
from tests.test_model_artifact import ARTIFACT, BOOSTERS, N_COLUMNS, _features

TARGET: Final[str] = "bike"

#: 天気の門で使う 3 日。**`split_days(…, 1, 1)` で 学習 / パージ / 検証 に 1 日ずつ。**
DAYS: Final[tuple[date, ...]] = eval_rows.DAYS
SPLIT: Final[DaySplit] = DaySplit(fit=(DAYS[0],), purge=(DAYS[1],), evaluate=(DAYS[2],))

#: 被覆を割合から起こすときの行数。
ROWS: Final[int] = 1_000


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
    """`to_registration` を通すだけの最小の結果。**指標は見ない。**"""
    return harness.Outcome(
        split=SPLIT, n_fit=1, n_eval=1, fits=(), overall=(), by_horizon=(), by_bucket=()
    )


CHECKS: Final[dict[str, fit.Checked]] = {
    "bike": fit.Checked(n_rows=1, max_gap=0.0),
    "dock": fit.Checked(n_rows=1, max_gap=0.0),
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
    """当てはめの結果ひとそろい（**森と指標は最小、天気だけ本物の形**）。"""
    return fit.Fitted(
        forests=ARTIFACT.forests,
        outcome=_empty_outcome(),
        checks=CHECKS,
        weather=_weather(*ratios),
    )


def test_the_registration_normalises_the_card_path(tmp_path: Path) -> None:
    """**登録する形が `card_reference` を通っている。**

    `card_reference` を直接試すだけでは、**呼び出し側が使うのをやめても気づけない**
    （実際に 1 度素通りした）。ここは `to_registration` の出力そのものを見る。
    """
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs").mkdir()
    card = f"{tmp_path}/apps/ml/../../docs/card.md"

    row = fit.to_registration(ARTIFACT, _fitted(1.0, 1.0, 1.0), card)

    assert row["card_path"] == "docs/card.md"


# ── 天気の門（W4 プラン §8.5.3、PR I）─────────────────────────
def _loaded(*ratios: float) -> Loaded:
    """`prepare` に渡す形。**表は小さいが本物**（`to_samples` を通る）。"""
    rows = [
        eval_rows.row(day, "hellocycling", station, horizon, 5, 5, 1, 1)
        for day in DAYS
        for station in ("a", "b")
        for horizon in (5, 60)
    ]
    return Loaded(table=eval_rows.to_table(rows), days=DAYS, weather=_weather(*ratios))


def test_a_mixed_period_stops_before_fitting() -> None:
    """**完了条件**（W4 プラン §6.8 の PR I）。

    09-07（被覆 0%）と 09-09（被覆 100%）を一緒に渡すと、**当てはめる前に**止まる。
    """
    with pytest.raises(coverage.MixedWeatherError, match=r"100\.00 ポイント"):
        fit.prepare(_loaded(0.0, 1.0, 1.0), eval_days=1, purge_days=1, allow_mixed_weather=False)


def test_the_flag_lets_a_mixed_period_through() -> None:
    """**承知のうえなら通す。** 通したことは登録簿に残る（下の検査）。"""
    samples, split = fit.prepare(
        _loaded(0.0, 1.0, 1.0), eval_days=1, purge_days=1, allow_mixed_weather=True
    )
    assert split.fit == (DAYS[0],)
    assert len(samples) > 0


def test_a_day_that_is_only_purged_does_not_stop_the_fit() -> None:
    """**パージ日は読むが捨てる**ので、そこだけ天気が無くても止めない。

    ここが素通りするようだと、門は「読んだ日ぜんぶ」を見ていることになり、
    **鳴かなくてよいところで鳴く**。
    """
    fit.prepare(_loaded(1.0, 0.0, 1.0), eval_days=1, purge_days=1, allow_mixed_weather=False)


def test_a_period_with_the_same_weather_passes() -> None:
    fit.prepare(_loaded(1.0, 1.0, 1.0), eval_days=1, purge_days=1, allow_mixed_weather=False)


def test_the_gate_is_reached_from_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**`run` が門を通っていること。**

    `prepare` を直に試すだけでは、**`run` が呼ぶのをやめても気づけない**（PR E′ で
    1 度そうなった）。ここは入り口から入れて、**当てはめに入る前に**止まるのを見る。
    3 日ぶんの `features/` を置き、学習日だけ天気を NULL にしてある。
    """
    root = write_samples(tmp_path, {DAYS[0]: False, DAYS[1]: True, DAYS[2]: True})
    monkeypatch.setattr(fit, "read_storage_config", lambda: None)
    monkeypatch.setattr(fit, "open_storage", lambda config: nullcontext(None))

    with pytest.raises(coverage.MixedWeatherError):
        fit.run(["--from", f"{DAYS[0]}", "--to", f"{DAYS[2]}", "--local", str(root)])


def test_the_flag_is_on_the_command_line() -> None:
    """**逃げ道が届いていること。** 門が在っても呼べなければ運用が止まる。"""
    base = ["--from", "2026-09-07", "--to", "2026-09-09"]
    assert fit._arguments(base).allow_mixed_weather is False
    assert fit._arguments([*base, "--allow-mixed-weather"]).allow_mixed_weather is True


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
    assert fit.to_metrics(fitted)["weather"] == fit._to_weather_rows(fitted)


def test_the_registration_carries_the_metrics() -> None:
    fitted = _fitted(1.0, 1.0, 1.0)
    assert fit.to_registration(ARTIFACT, fitted, None)["metrics"] == fit.to_metrics(fitted)


# ── モデルカード ──────────────────────────────────────────────
def test_the_card_shows_the_measured_coverage() -> None:
    """**カードは日付を決め打ちしない。** 測った数を日ごとに出す。"""
    text = fit.render_card(ARTIFACT, _fitted(1.0, 0.6, 1.0))
    assert "## 学習に使ったデータ" in text
    assert "| 2026-09-07 | 学習 | 1,000 | 100.000% |" in text
    assert "| 2026-09-08 | パージ | 1,000 | 60.000% |" in text
    assert "| 2026-09-09 | 検証 | 1,000 | 100.000% |" in text
    assert "**天気の被覆は揃っている**（学習日と検証日の差 0.00 ポイント）" in text


def test_the_card_says_when_the_gate_was_forced() -> None:
    """**限界の節に、通したことが出る。** 読む人が後から判断できる。"""
    text = fit.render_card(ARTIFACT, _fitted(0.0, 1.0, 1.0))
    assert "**天気の被覆が学習日と検証日で 100.00 ポイント違う**" in text
    assert "--allow-mixed-weather" in text


def test_the_registration_can_only_ask_for_candidate() -> None:
    """**`register_model_version` は candidate しか受け付けない。** 送る側も揃える。"""
    row = fit.to_registration(ARTIFACT, _fitted(1.0, 1.0, 1.0), None)
    assert row["status"] == "candidate"
    assert row["card_path"] is None
    assert row["artifact_path"] == "lightgbm/lgbm-v0-test.json.gz"
