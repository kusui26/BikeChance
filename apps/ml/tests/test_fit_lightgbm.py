"""当てはめの側の門（`jobs/fit_lightgbm.py`）。

**主題は「違う値を出す成果物が生まれ得ないこと」。** 配信は `lightgbm` を読み込まず
木を numpy で歩く（W4-19）。同じ値が出ることは**宣言ではなく検査**で担保する、と決めた
——その検査そのものが働いているかを、ここで確かめる。

**照合に足す「実データが踏まない枝」も見る。** 検証日の行だけでは全欠損・全 0・
未知のカテゴリを一度も通らないことがあり、**配信で最初に踏むのがその枝**では困る。
"""

import inspect
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Final

import numpy as np
import pytest

from bikechance_ml.baselines.climatology import FromSamples
from bikechance_ml.eval import harness
from bikechance_ml.eval.split import DaySplit
from bikechance_ml.features import coverage
from bikechance_ml.features.arrays import Float32
from bikechance_ml.features.schema import WEATHER_COLUMNS
from bikechance_ml.jobs import climate as climate_module
from bikechance_ml.jobs import fit_lightgbm as fit
from bikechance_ml.jobs.build_features import to_parquet_bytes
from bikechance_ml.jobs.window import Window
from bikechance_ml.models import artifact as lightgbm_artifact
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


def test_the_fit_matrix_is_built_once_for_both_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**学習の行列は 1 度だけ作る**（検証ぶんと合わせて 2 回）。

    ターゲットで違うのはラベルと重みだけで、特徴量は同じである。ターゲットごとに
    作り直すと、**28 日ぶんで 4 GB の組み立てを 2 回**払うことになる（§12 の 168）。
    **答えは変わらないので、数えないと気づけない。**

    当てはめそのものは差し替える——ここで見たいのは**何回組み立てたか**だけで、
    木の中身は他の検査が見ている。

    **日ごとに行数を変える。** 同じ行数の日を並べると、**検証と学習を取り違えても
    同じ数**になり、確保の回数しか見られない（故意に取り違えたら素通りした）。
    """
    root = write_distinct_sizes(tmp_path)
    opened = read_local(root, DAYS)
    samples, split = fit.prepare(opened, eval_days=1, purge_days=1, allow_mixed_weather=False)

    reserved: list[int] = []
    original = matrix.empty

    def counted(rows: int) -> matrix.Matrix:
        reserved.append(rows)
        return original(rows)

    monkeypatch.setattr(matrix, "empty", counted)
    monkeypatch.setattr(fit, "train_one", lambda *args: None)
    monkeypatch.setattr(
        fit, "flatten_and_verify", lambda *args: (ARTIFACT.forests["bike"], fit.Checked(0, 0.0))
    )
    monkeypatch.setattr(harness, "run", lambda *args, **kwargs: None)

    fit.fit_and_score(opened, samples, split, FromSamples())
    expected = [opened.rows[split.evaluate[0]], opened.rows[split.fit[0]]]
    assert reserved == expected, (
        f"行列を {len(reserved)} 回確保しています（検証と学習で 2 回のはず）"
    )


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
    """**既定はプロファイル**（本番と同じ）。学習期間の日だけを渡す。

    **決めたものが当てはめに届くところまで留める。** 「呼んでいるが使っていない」が
    通ると §12 の 166 はそのまま戻る——`source_for` を呼んだうえで結果を捨て、
    `FromSamples()` を渡せば、**数字は壊れたまま検査は緑になる**。
    """
    root = write_samples(tmp_path, dict.fromkeys(DAYS, True))
    monkeypatch.setattr(fit, "read_storage_config", lambda: None)
    monkeypatch.setattr(fit, "open_storage", lambda config: nullcontext(None))
    asked: list[tuple[tuple[date, ...], bool]] = []
    decided = FromSamples()
    handed: list[object] = []

    def spy(source, days, local, ports, *, from_profiles):  # type: ignore[no-untyped-def]
        asked.append((tuple(days), from_profiles))
        return decided

    def fitting(loaded, samples, split, climate_source):  # type: ignore[no-untyped-def]
        handed.append(climate_source)
        return _fitted(1.0, 1.0, 1.0)

    monkeypatch.setattr(climate_module, "source_for", spy)
    monkeypatch.setattr(fit, "fit_and_score", fitting)

    fit.run(["--from", f"{DAYS[0]}", "--to", f"{DAYS[2]}", "--local", str(root)])
    assert asked, "気候値の作り方を決めていません"
    days, from_profiles = asked[0]
    assert from_profiles is True, "既定でプロファイルを読んでいません"
    assert days == (DAYS[0],), "学習期間の日だけを渡していません（検証日が混ざる）"
    assert handed and handed[0] is decided, "決めた作り方を当てはめに渡していません"


def test_the_no_profile_flag_falls_back_to_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**逃げ道が届いていること。** 2026-09-19 より前の記録と比べるために要る。"""
    root = write_samples(tmp_path, dict.fromkeys(DAYS, True))
    monkeypatch.setattr(fit, "read_storage_config", lambda: None)
    monkeypatch.setattr(fit, "open_storage", lambda config: nullcontext(None))
    asked: list[bool] = []

    def spy(source, days, local, ports, *, from_profiles):  # type: ignore[no-untyped-def]
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
    assert fit.to_metrics(fitted)["climate"] == fitted.outcome.climate


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


def _run_with(shelf: Shelf, root: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """**入り口から**走らせる。当てはめは差し替える（見たいのは置く前後だけ）。"""
    monkeypatch.setattr(fit, "read_storage_config", lambda: None)
    monkeypatch.setattr(fit, "open_storage", lambda config: nullcontext(shelf))
    argv = ["--from", f"{DAYS[0]}", "--to", f"{DAYS[2]}", "--local", str(root)]
    return fit.run([*argv, "--upload", "--register"])


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
    """**candidate は置き直せる。** 登録簿は 2 度引く（当てはめる前と、置く直前）。"""
    root = write_samples(tmp_path, dict.fromkeys(DAYS, True))
    shelf = Shelf(statuses={FIT_NAME: "candidate"})
    monkeypatch.setattr(fit, "fit_and_score", lambda *args: _fitted(1.0, 1.0, 1.0))
    assert _run_with(shelf, root, monkeypatch) == 0
    assert shelf.lookups == [FIT_NAME, FIT_NAME]
    path = lightgbm_artifact.artifact_path(FIT_NAME)
    assert shelf.uploads == [(registry.MODEL_BUCKET, path, lightgbm_artifact.CONTENT_TYPE)]
    assert shelf.registered == [FIT_NAME]


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
