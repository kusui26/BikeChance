"""天気の被覆（`features/coverage.py`、W4 プラン §8.5.3、PR I）。

**主題は「同じ版で中身が違う」を数字にすること。** `feature_set` は列が在ることしか
語らない。3 日ぶんの `features/` はすべて `v3` だが、天気が入っている行の割合は
**0% / 98.6% / 100%** である。版だけを突き合わせていると、この違いは**例外も警告も
出さずに**学習に混ざる。

**ここで固定するのは 3 つ。**

  * **数え方**：4 列すべてが入っている行だけを被覆ありとする（1 列でも欠ければ落とす）
  * **見分け**：読む列を絞った表を「被覆 0%」と読み違えない
  * **閾値の根拠**：`MAX_SPREAD_PP` が「境目の作り物」と「実害の最小」のあいだに在る
"""

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Final

import pyarrow as pa
import pytest

from bikechance_ml.eval.dataset import NEEDED_COLUMNS
from bikechance_ml.features import coverage
from bikechance_ml.features.schema import WEATHER_COLUMNS

#: 実測（2026-09-11、`.cache/features` の 3 日ぶん）。**§8.5.3 の表そのもの。**
OBSERVED: Final[Mapping[str, float]] = {
    # 予報が 1 件も無い（アーカイブは 2026-09-07 15:17 UTC から）
    "2026-09-07": 0.0,
    # 00:00〜00:15 の 4 点（28,700 行）と、恒久的に格子外の 5 ポート（466 行）
    "2026-09-08": 0.98589,
    # 恒久的に格子外の 5 ポートだけ（483 行）
    "2026-09-09": 0.99977,
}

#: 1 時間ぶんの基準時刻（288 点のうち 12 点）。**実際に起きる最小の欠け**（`MAX_SPREAD_PP`）。
AN_HOUR_PP: Final[float] = 12 / 288 * 100

#: アーカイブが時の途中で始まった日と、その翌日の差。**止めるほどのものではない。**
THE_EDGE_PP: Final[float] = (OBSERVED["2026-09-09"] - OBSERVED["2026-09-08"]) * 100

#: 割合から件数を起こすときの行数。
ROWS: Final[int] = 100_000


def _table(*rows: Sequence[float | None]) -> pa.Table:
    """天気 4 列だけの表。**1 行が 4 つの値**（`None` は NULL）。"""
    columns = list(zip(*rows, strict=True)) if rows else [() for _ in WEATHER_COLUMNS]
    return pa.table(
        {
            name: pa.array(list(values), type=pa.float32())
            for name, values in zip(WEATHER_COLUMNS, columns, strict=True)
        }
    )


def _at(ratio: float) -> coverage.Coverage:
    """その割合になる数え上げ。**表は作らない**（閾値の検査に表は要らない）。"""
    covered = round(ratio * ROWS)
    return coverage.Coverage(
        rows=ROWS, covered=covered, by_column=dict.fromkeys(WEATHER_COLUMNS, covered)
    )


def _observed(*days: str) -> dict[date, coverage.Coverage]:
    """実測の日を**名指しで**並べる（`OBSERVED` から引く）。"""
    return {date.fromisoformat(day): _at(OBSERVED[day]) for day in days}


def _days(*ratios: float) -> dict[date, coverage.Coverage]:
    """9/7 から順に並べる。**日付に意味の無い検査**（絞り込み）でだけ使う。"""
    return {date(2026, 9, 7 + index): _at(ratio) for index, ratio in enumerate(ratios)}


# ── 数え方 ────────────────────────────────────────────────────
def test_a_table_with_every_value_is_fully_covered() -> None:
    built = coverage.measure(_table([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0]))
    assert (built.rows, built.covered, built.ratio) == (2, 2, 1.0)


def test_a_row_that_misses_one_column_is_not_covered() -> None:
    """**4 列すべてが要る。** 1 列でも欠けた行は落とす。

    ここが「どれか 1 列」で数えていると、風速だけ欠けた行が被覆ありになる。
    """
    built = coverage.measure(_table([1.0, 2.0, 3.0, None], [1.0, 2.0, 3.0, 4.0]))
    assert built.covered == 1
    assert built.by_column == {
        "precip_mm_now": 2,
        "precip_mm_target": 2,
        "temp_c": 2,
        "wind_kmh": 1,
    }


def test_the_columns_are_counted_one_by_one() -> None:
    """**列ごとの数も残す。** 4 列は引く時間帯が違うので、端では別々に欠けうる。"""
    built = coverage.measure(_table([1.0, None, 3.0, 4.0], [None, 2.0, 3.0, 4.0]))
    assert built.covered == 0
    assert built.by_column["precip_mm_now"] == 1
    assert built.by_column["precip_mm_target"] == 1
    assert built.by_column["temp_c"] == 2


def test_columns_that_fail_together_are_uniform() -> None:
    assert coverage.measure(_table([None, None, None, None], [1.0, 2.0, 3.0, 4.0])).is_uniform


def test_columns_that_fail_apart_are_not_uniform() -> None:
    """**一致は前提ではなく観測。** ずれたら見える。"""
    assert not coverage.measure(_table([1.0, 2.0, 3.0, None])).is_uniform


def test_an_empty_table_is_not_a_covered_table() -> None:
    """行が無ければ 0。**`pc.sum` が NULL を返す**ところで落ちない。"""
    built = coverage.measure(_table())
    assert (built.rows, built.covered, built.ratio) == (0, 0, 0.0)


# ── 見分け ────────────────────────────────────────────────────
def test_a_table_without_the_weather_columns_is_refused() -> None:
    """**読む列を絞った表を「被覆 0%」と読み違えない**（`NEEDED_COLUMNS` は 11 列）。"""
    with pytest.raises(coverage.MissingWeatherColumnsError, match="wind_kmh"):
        coverage.measure(_table([1.0, 2.0, 3.0, 4.0]).drop_columns(["wind_kmh"]))


def test_the_baselines_do_not_read_the_weather() -> None:
    """**だから `evaluate_baselines` は止めない。** 主張を機械で固定する。"""
    assert not set(NEEDED_COLUMNS) & set(WEATHER_COLUMNS)


def test_the_dict_carries_every_field() -> None:
    """**数えたものは全部出す**（W4 プラン §8.5.8 の教訓）。"""
    built = coverage.measure(_table([1.0, 2.0, 3.0, None], [1.0, 2.0, 3.0, 4.0]))
    assert built.as_dict() == {
        "rows": 2,
        "covered": 1,
        "ratio": 0.5,
        "by_column": {
            "precip_mm_now": 2,
            "precip_mm_target": 2,
            "temp_c": 2,
            "wind_kmh": 1,
        },
    }


# ── 閾値 ──────────────────────────────────────────────────────
def test_the_spread_is_in_percentage_points() -> None:
    assert round(coverage.spread_pp(_observed(*OBSERVED).values()), 3) == 99.977


def test_the_spread_of_nothing_is_zero() -> None:
    assert coverage.spread_pp([]) == 0.0


def test_the_threshold_sits_between_the_edge_and_an_hour() -> None:
    """**閾値の根拠を固定する。**

    予報の先読み（8 時間）は取得が 2 回続けて落ちても吸収するので、被覆が本当に
    落ちるのは 3 時間以上の停止から——**実害の最小は 1 時間ぶん**。いっぽう
    「アーカイブが時の途中で始まった日」は 1.39 ポイントしか違わない。挟んで置く。
    """
    assert round(THE_EDGE_PP, 3) == 1.388
    assert round(AN_HOUR_PP, 3) == 4.167
    assert THE_EDGE_PP < coverage.MAX_SPREAD_PP < AN_HOUR_PP


# ── 門 ────────────────────────────────────────────────────────
def test_a_day_without_weather_stops_the_fit() -> None:
    """**完了条件**（W4 プラン §6.8 の PR I）：09-07 と 09-09 を一緒に渡すと止まる。"""
    with pytest.raises(coverage.MixedWeatherError, match=r"99\.98 ポイント"):
        coverage.refuse_if_mixed(_observed("2026-09-07", "2026-09-09"))


def test_the_message_names_the_days_and_the_way_out() -> None:
    """**どの日が外れているかを名指しし、逃げ道も書く。**"""
    with pytest.raises(coverage.MixedWeatherError) as caught:
        coverage.refuse_if_mixed(_observed("2026-09-07", "2026-09-09"))
    message = str(caught.value)
    assert "2026-09-07 0.000%" in message
    assert "2026-09-09 99.977%" in message
    assert "--allow-mixed-weather" in message


def test_two_days_that_only_differ_at_the_edge_pass() -> None:
    """**09-08 と 09-09 は混ぜてよい。** 20 分ぶんの境目で鳴かない。"""
    coverage.refuse_if_mixed(_observed("2026-09-08", "2026-09-09"))


def test_one_day_alone_passes() -> None:
    coverage.refuse_if_mixed(_observed("2026-09-07"))


def test_nothing_at_all_passes() -> None:
    """読めた日が無いときは `NoSamplesError` の仕事。**ここでは黙って通す。**"""
    coverage.refuse_if_mixed({})


# ── 絞り込み ──────────────────────────────────────────────────
def test_restrict_keeps_only_the_days_asked_for() -> None:
    """**パージ日を数えないために使う**（`DaySplit.used`）。"""
    everything = _days(0.0, 0.5, 1.0)
    kept = coverage.restrict(everything, [date(2026, 9, 7), date(2026, 9, 9)])
    assert sorted(kept) == [date(2026, 9, 7), date(2026, 9, 9)]
    assert kept[date(2026, 9, 9)].ratio == 1.0


def test_restrict_ignores_days_that_were_not_read() -> None:
    assert coverage.restrict(_days(1.0), [date(2026, 12, 25)]) == {}
