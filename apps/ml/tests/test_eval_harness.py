"""評価ハーネス（`eval/harness.py`、`eval/report.py`、W3 プラン §5.9）。

**この 1 ファイルの主題は「すべてのモデルが同じ行の上で測られること」**（§4.4 の 30b）。
この作業でも最初に踏んだ罠で、B0 を 1 日全体、B1 を午後だけで測って「改善」の列が
比較になっていなかった。
"""

from datetime import date, datetime
from typing import Final

import numpy as np
import pytest

from bikechance_ml.eval import harness, report
from bikechance_ml.eval.dataset import to_samples
from bikechance_ml.eval.split import mask_of, split_days
from bikechance_ml.features.coverage import Coverage
from bikechance_ml.features.grid import JST
from bikechance_ml.features.schema import WEATHER_COLUMNS
from tests import eval_fixture as fixture

DAY0, DAY1, DAY2 = fixture.DAYS


def _coverage(rows: int, ratio: float) -> Coverage:
    """その割合になる数え上げ。**行数も日ごとに変える**（取り違えを見えるようにする）。"""
    covered = round(rows * ratio)
    return Coverage(rows=rows, covered=covered, by_column=dict.fromkeys(WEATHER_COLUMNS, covered))


#: 学習 100% / **パージ 0%** / 検証 100%。分割は `split_days(DAYS, 1, 1)`。
#: **パージ日まで数えていれば差が 100 ポイントになる**ので、0 が出れば数えていない。
WEATHER: Final[dict[date, Coverage]] = dict(
    zip(fixture.DAYS, (_coverage(100, 1.0), _coverage(200, 0.0), _coverage(300, 1.0)), strict=True)
)

#: **学習日だけ天気が無い。** 止める側の見え方（09-07 と 09-09 を混ぜた形）。
MIXED: Final[dict[date, Coverage]] = dict(
    zip(fixture.DAYS, (_coverage(100, 0.0), _coverage(200, 1.0), _coverage(300, 1.0)), strict=True)
)


def _day_of(row: dict[str, object]) -> date:
    """行の基準時刻の JST 暦日。フィクスチャは JST で組み立てている。"""
    at = row["t"]
    if not isinstance(at, datetime):
        raise TypeError("t が datetime ではありません")
    return at.astimezone(JST).date()


def build(rows: list[dict[str, object]]) -> harness.Outcome:
    samples = to_samples(fixture.to_table(rows))
    return harness.run(samples, split_days(fixture.DAYS, evaluate_days=1, purge_days=1))


def scenario() -> list[dict[str, object]]:
    """3 日 × 2 システム × 2 水平 × 台数違い。**検証日にも学習日にも行がある。**"""
    rows: list[dict[str, object]] = []
    for index, day in enumerate(fixture.DAYS):
        for system in ("hellocycling", "docomo-cycle"):
            for horizon in (5, 60):
                for station, bikes in (("a", 0), ("b", 1), ("c", 7)):
                    rows.append(
                        fixture.row(
                            day,
                            system,
                            station,
                            horizon,
                            bikes,
                            9 - bikes,
                            1 if bikes > 0 else index % 2,
                            1,
                            minute_of_day=600 + 15 * index,
                        )
                    )
    return rows


OUTCOME = build(scenario())


def test_every_model_is_scored_on_the_same_rows() -> None:
    """**件数が違えば集合が違う。** 比較は成立しない。"""
    for part in (*OUTCOME.overall, *OUTCOME.by_horizon, *OUTCOME.by_bucket):
        counts = {name: scores.n for name, scores in part.weighted.items()}
        assert len(set(counts.values())) == 1, part.slice


def test_all_four_models_are_present() -> None:
    for part in OUTCOME.overall:
        assert set(part.weighted) == set(harness.MODELS)
        assert set(part.unweighted) == set(harness.MODELS)


def test_evaluation_uses_only_the_evaluation_day() -> None:
    """学習日とパージ日の行は入らない。"""
    assert OUTCOME.split.evaluate == (DAY2,)
    assert OUTCOME.split.purge == (DAY1,)
    assert OUTCOME.split.fit == (DAY0,)
    on_last_day = [one for one in scenario() if _day_of(one) == DAY2]
    assert OUTCOME.n_eval == len(on_last_day)


def test_empty_slices_are_dropped() -> None:
    """**0 行の切り口は表に出さない。** 空の行は読み手を惑わせる。"""
    assert all(part.n > 0 for part in OUTCOME.by_bucket)


def test_b2_fallback_ratio_is_reported() -> None:
    """蓄積が短いあいだ B2 は実質 B1。**それが分かる形で出す**（W3-17）。"""
    for fit in OUTCOME.fits:
        assert 0.0 <= fit.b2_fallback_ratio <= 1.0


def test_report_renders_the_judgement_first() -> None:
    text = report.render_markdown(OUTCOME, "検査", "但し書き", weather=WEATHER)
    assert text.startswith("# 検査")
    assert "## 1. 判定" in text
    assert "但し書き" in text
    assert "B1 の Brier が全水平で B0 以下" in text


def test_report_marks_unjudgeable_buckets() -> None:
    """**Brier がほぼ 0 のバケツは「判定対象外」と書く**（W3-16）。"""
    rows = [
        fixture.row(day, "hellocycling", station, 5, 9, 0, 1, 1)
        for day in fixture.DAYS
        for station in ("a", "b")
    ]
    text = report.render_markdown(build(rows), "検査", "", weather=WEATHER)
    assert "判定対象外" in text


def test_report_warns_when_b2_is_really_b1() -> None:
    rows = [
        fixture.row(day, "hellocycling", station, 5, 1, 8, 1, 1, minute_of_day=600 + 60 * index)
        for index, day in enumerate(fixture.DAYS)
        for station in ("a", "b")
    ]
    text = report.render_markdown(build(rows), "検査", "", weather=WEATHER)
    assert "B2 は実質 B1 である" in text


# ── §2 データの素性（W4 プラン §8.5.3、PR I）─────────────────
def test_the_report_lists_the_days_and_their_weather() -> None:
    """**何で測ったかを、数字の前に出す。** 役割・行数・被覆を日ごとに並べる。"""
    text = report.render_markdown(OUTCOME, "検査", "但し書き", weather=WEATHER)
    assert "## 2. データ（読んだ日と天気の被覆）" in text
    assert "| 2026-09-07 | 学習 | 100 | 100.000% |" in text
    assert "| 2026-09-08 | パージ | 200 | 0.000% |" in text
    assert "| 2026-09-09 | 検証 | 300 | 100.000% |" in text


def test_the_spread_ignores_the_purge_day() -> None:
    """**パージ日は読むが捨てる。** 差は学習日と検証日だけで測る。

    この仕掛けではパージ日だけ 0% なので、**数えていれば 100 ポイント**になる。
    """
    text = report.render_markdown(OUTCOME, "検査", "", weather=WEATHER)
    assert "**学習と検証の被覆の差**：0.00 ポイント" in text
    assert "**揃っている**" in text


def test_a_mixed_period_is_called_out() -> None:
    """**学習日だけ天気が無い**なら、そう書く（`fit_lightgbm` はここで止まる）。"""
    text = report.render_markdown(OUTCOME, "検査", "", weather=MIXED)
    assert "**学習と検証の被覆の差**：100.00 ポイント" in text
    assert "**混ざっている**" in text


def test_the_column_evenness_is_stated_either_way() -> None:
    """**揃っているときも書く。** 黙ると「見ていない」と区別できない。"""
    even = report.render_markdown(OUTCOME, "検査", "", weather=WEATHER)
    assert "**4 列の欠けかた**：全日で一致" in even

    uneven = {**WEATHER, DAY2: Coverage(rows=300, covered=10, by_column={"temp_c": 20})}
    text = report.render_markdown(OUTCOME, "検査", "", weather=uneven)
    assert "**4 列の欠けかた**：**2026-09-09 で列ごとに違う**" in text


def test_split_failure_is_loud() -> None:
    """1 日しか無いのに測ろうとしたら止まる。"""
    with pytest.raises(ValueError, match="足りません"):
        split_days([date(2026, 9, 7)], evaluate_days=1, purge_days=1)


# ── 外から足したモデル（W4 プラン §6.5）─────────────────────
def _extra(outcome_rows: list[dict[str, object]], value: float) -> harness.Outcome:
    """**同じ検証行**の上に、外で当てはめたモデルの予測を並べる。"""
    samples = to_samples(fixture.to_table(outcome_rows))
    split = split_days(fixture.DAYS, evaluate_days=1, purge_days=1)
    n_eval = int(mask_of(samples, split.evaluate).sum())
    return harness.run(
        samples,
        split,
        {"LGBM": {name: np.full(n_eval, value) for name in ("bike", "dock")}},
    )


def test_an_extra_model_joins_every_table() -> None:
    """**合否はバケツ別で決める**（W3-16）ので、外から足したモデルもそこに並ぶ。"""
    outcome = _extra(scenario(), 0.5)
    assert outcome.models == ("B0", "B1", "B2", "B3", "LGBM")
    text = report.render_markdown(outcome, "検査", "", weather=WEATHER)
    for heading in ("## 3.", "## 4.", "## 5.", "## 6."):
        block = text.split(heading)[1].split("\n##")[0]
        assert "LGBM" in block, f"{heading} に LGBM が出ていない"


def test_the_tables_stay_rectangular() -> None:
    """**列の数が行と揃っている。** 見出しだけ増やして本文がずれる事故を止める。"""
    text = report.render_markdown(_extra(scenario(), 0.5), "検査", "", weather=WEATHER)
    for block in text.split("\n\n"):
        rows = [one for one in block.splitlines() if one.startswith("|")]
        if len(rows) < 3:
            continue
        widths = {one.count("|") for one in rows}
        assert len(widths) == 1, f"列の数が揃っていない: {rows[0]}"


def test_a_misaligned_extra_model_stops() -> None:
    """**別の行で測らない**（§4.4 の 30b）。行数が違えば例外にする。"""
    samples = to_samples(fixture.to_table(scenario()))
    split = split_days(fixture.DAYS, evaluate_days=1, purge_days=1)
    with pytest.raises(harness.MisalignedPredictionError):
        harness.run(samples, split, {"LGBM": {"bike": np.zeros(3), "dock": np.zeros(3)}})


def test_without_extras_the_tables_are_unchanged() -> None:
    """**足さなければ今までどおり。** 既存の報告の形を壊さない。"""
    assert build(scenario()).models == ("B0", "B1", "B2", "B3")
