"""学習サンプルの天気の被覆を測る（W4 プラン §8.5.3、PR I）。

**純粋。** 表を数えるだけで、I/O も引数の解釈も持たない（CLAUDE.md §3）。

**`feature_set` は「列が在る」を表すだけで、「値が入っている」を表さない。** 手元の
3 日ぶんの `features/` はすべて `v3` だが、天気 4 列の欠けかたは同じではない
（実測 2026-09-11）。

| 日 | 被覆 | 欠けている中身 |
|---|---:|---|
| 2026-09-07 | **0.000%** | 予報が 1 件も無い（アーカイブは 09-07 15:17 UTC から。§5.4） |
| 2026-09-08 | 98.589% | **00:00〜00:15 の 4 点**（28,700 行）と格子外の 5 ポート（466 行） |
| 2026-09-09 | 99.977% | 恒久的に格子外の 5 ポートだけ（483 行） |

**版だけを突き合わせていると、この違いは例外も警告も出さずに混ざる。** 混ざると、
**配信では必ず天気が在るのに、学習には「天気を一度も見ていない日」の枝ができる**——
その枝は実戦で一度も通らない。

**測るのは表そのものから。** 組み立てたときの数を控えておいて後で読むのではなく、
**Parquet を読んだ人がその場で数える**。そうすると 2 つ良いことがある。

  * **すでに在る日にも効く**（3 日ぶんは被覆を記録せずに作ってある）
  * **作り直した日と、控えてあった数が食い違う**という状態が生まれない
"""

import functools
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Final

import pyarrow as pa
import pyarrow.compute as pc

from bikechance_ml.features.schema import WEATHER_COLUMNS

#: 日ごとの被覆の差がこれを超えたら、明示しない限り当てはめない（パーセントポイント）。
#:
#: **実際に起きる最小の欠けと、境目の作り物のあいだに置く。**
#:
#:   * 予報の先読み（`WEATHER_LEAD_HOURS = 8`）は**取得が 2 回続けて落ちても吸収する**
#:     ので、被覆が本当に落ちるのは **3 時間以上の停止**からで、そのとき見えなくなるのは
#:     概ね（停止 − 2）時間ぶん。**最小の実害は 1 時間 ＝ 288 点のうち 12 点 ＝ 4.17 ポイント**
#:   * いっぽう「アーカイブが時の途中で始まった日」（09-08）は **1.39 ポイント**しか違わない。
#:     20 分ぶんの境目であって、止めるほどのものではない
#:
#: 挟んで **3.0**。**観測は 2 日ぶんしか無い**ので、日が貯まったら測り直す（W4 プラン §8.5.3）。
MAX_SPREAD_PP: Final[float] = 3.0


class MissingWeatherColumnsError(ValueError):
    """表に天気の列が無い。**「被覆 0%」と区別できないので止める。**

    読むときに列を絞ると（`eval/dataset.py` の `NEEDED_COLUMNS` は 11 列）、天気の列は
    落ちる。そこで黙って 0 を返すと、**絞ったせいなのか本当に入っていないのか**が
    後から見分けられない。
    """


class MixedWeatherError(RuntimeError):
    """被覆の違う日が混ざっている。**明示しない限り当てはめない。**"""


@dataclass(frozen=True)
class Coverage:
    """1 つの表の天気の被覆。**割合ではなく件数で持つ**（足し合わせられる）。

    `covered` は **4 列すべてが非 NULL の行**である。列ごとの数も持つのは、4 つが
    同じ行で欠けるとは限らないため——引く時間帯が違う（降水は `t` を含む時間帯と
    目標時刻の時間帯、気温と風速は最も近い毎正時。`features/weather.py`）ので、
    **予報の端では片方だけが欠けうる**。実測ではいまのところ 4 列は完全に一致するが、
    **一致は前提ではなく観測**なので、ずれたら見えるようにしておく。
    """

    rows: int
    covered: int
    by_column: Mapping[str, int]

    @property
    def ratio(self) -> float:
        """4 列すべてが入っている行の割合。**行が無ければ 0。**"""
        return 0.0 if self.rows == 0 else self.covered / self.rows

    @property
    def is_uniform(self) -> bool:
        """4 列が**同じ行で**欠けているか。偽なら端の欠けかたが列で違う。"""
        return all(one == self.covered for one in self.by_column.values())

    def as_dict(self) -> dict[str, object]:
        """JSON に出す形。**単体で読める**ようにする（`rows` も入れる）。"""
        return {
            "rows": self.rows,
            "covered": self.covered,
            "ratio": round(self.ratio, 6),
            "by_column": dict(sorted(self.by_column.items())),
        }


def measure(table: pa.Table) -> Coverage:
    """表の天気の被覆を数える。**4 列すべてが入っている行**を被覆ありとする。"""
    refuse_a_table_without_weather(table)
    valid = {name: pc.is_valid(table.column(name)) for name in WEATHER_COLUMNS}
    covered = functools.reduce(pc.and_, valid.values())
    return Coverage(
        rows=table.num_rows,
        covered=_true(covered),
        by_column={name: _true(one) for name, one in valid.items()},
    )


def refuse_a_table_without_weather(table: pa.Table) -> None:
    """**天気の列が無ければ止める。** 読む列を絞ったのを 0% と読み違えないため。"""
    missing = tuple(name for name in WEATHER_COLUMNS if name not in table.column_names)
    if missing:
        raise MissingWeatherColumnsError(f"天気の列が表にありません: {', '.join(missing)}")


def _true(values: pa.ChunkedArray) -> int:
    """真の数。**空の表では `pc.sum` が NULL を返す**ので 0 に落とす。"""
    return int(pc.sum(values).as_py() or 0)


def spread_pp(coverage: Iterable[Coverage]) -> float:
    """被覆のいちばん高い日と低い日の差（パーセントポイント）。**空なら 0。**"""
    ratios = [one.ratio for one in coverage]
    return 0.0 if not ratios else (max(ratios) - min(ratios)) * 100.0


def restrict(weather: Mapping[date, Coverage], days: Iterable[date]) -> dict[date, Coverage]:
    """その日だけを取り出す。**パージ日を数えないために使う**（`DaySplit.used`）。"""
    wanted = set(days)
    return {day: one for day, one in weather.items() if day in wanted}


def refuse_if_mixed(weather: Mapping[date, Coverage]) -> None:
    """**被覆の違う日が混ざっていたら止める**（`fit_lightgbm` の `--allow-mixed-weather`）。

    止める側に倒すのは、**混ざったまま当てはめると静かに壊れる**からである。天気の
    無い日の行は木の中で `default_left` の枝に落ち、そこに学習が張り付く。例外は出ず、
    **確率だけが配信でずれる**。`refuse_if_different`（`jobs/fit_lightgbm.py`）と同じ作法。
    """
    spread = spread_pp(weather.values())
    if spread <= MAX_SPREAD_PP:
        return
    raise MixedWeatherError(
        f"天気の被覆が日によって {spread:.2f} ポイント違います"
        f"（許容 {MAX_SPREAD_PP} ポイント）: {describe(weather)}。"
        "期間を狭めるか、承知のうえなら --allow-mixed-weather を付けてください"
    )


def describe(weather: Mapping[date, Coverage]) -> str:
    """日と被覆を 1 行にする。**どの日が外れているかを名指しする。**"""
    return " / ".join(f"{day:%Y-%m-%d} {one.ratio:.3%}" for day, one in sorted(weather.items()))
