"""評価ハーネス（`eval/harness.py`、`eval/report.py`、W3 プラン §5.9）。

**この 1 ファイルの主題は「すべてのモデルが同じ行の上で測られること」**（§4.4 の 30b）。
この作業でも最初に踏んだ罠で、B0 を 1 日全体、B1 を午後だけで測って「改善」の列が
比較になっていなかった。
"""

from datetime import date, datetime

import pytest

from bikechance_ml.eval import harness, report
from bikechance_ml.eval.dataset import to_samples
from bikechance_ml.eval.split import split_days
from bikechance_ml.features.grid import JST
from tests import eval_fixture as fixture

DAY0, DAY1, DAY2 = fixture.DAYS


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
    text = report.render_markdown(OUTCOME, "検査", "但し書き")
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
    text = report.render_markdown(build(rows), "検査", "")
    assert "判定対象外" in text


def test_report_warns_when_b2_is_really_b1() -> None:
    rows = [
        fixture.row(day, "hellocycling", station, 5, 1, 8, 1, 1, minute_of_day=600 + 60 * index)
        for index, day in enumerate(fixture.DAYS)
        for station in ("a", "b")
    ]
    text = report.render_markdown(build(rows), "検査", "")
    assert "B2 は実質 B1 である" in text


def test_split_failure_is_loud() -> None:
    """1 日しか無いのに測ろうとしたら止まる。"""
    with pytest.raises(ValueError, match="足りません"):
        split_days([date(2026, 9, 7)], evaluate_days=1, purge_days=1)
