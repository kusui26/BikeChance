"""学習サンプルの窓を、**1 つの表にせずに**読む（W5 プラン §12 の 168 の 2 段目）。

**以前は全日を `pa.concat_tables` で 1 つにしていた。** 28 日ぶんでは
**18,684,940 行 × 264 B ＝ 4.59 GiB** で、しかも**行列を組み立てているあいだ
ずっと生きている**——行列（3.90 GiB）と同じものを 2 つの形で持っているのに等しく、
2026-09-20 の実測では `ubuntu-latest`（15.61 GiB）に対して余りが 0.22 GiB しか
残らなかった。

**ここでは 3 つに分ける。**

  1. `read_window` … 日ごとに取り、**天気の 4 列だけ**開いて被覆と行数を数える。
     中身は**圧縮のまま**持つ（1 日 15〜41 MB。解いた表の 25 分の 1）
  2. `samples_of` … 日ごとに開いて `Samples` にする。**文字列はその日のうちに消える**
  3. `matrix_of` … 先に場所を確保し、**1 日読んでは区間を埋めて捨てる**

**2 度目の取得をしない。** 圧縮のまま持っておけば、当てはめの途中で Storage を
もう一度叩かずに済む——**落ちる機会を増やさない**ほうが、0.46 GB より安い。

**門は 1 の後・2 の前に通る**（`jobs/fit_lightgbm.py` の `prepare`）。天気の被覆は
1 で数え終わっているので、**止めるときは表を 1 つも開いていない。**
"""

import io
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from bikechance_ml.eval.dataset import NEEDED_COLUMNS, Samples, merge, to_chunk
from bikechance_ml.features import coverage
from bikechance_ml.features.grid import features_path
from bikechance_ml.features.schema import WEATHER_COLUMNS
from bikechance_ml.io.supabase import SupabaseIo
from bikechance_ml.jobs import climate
from bikechance_ml.models import matrix

#: 1 日ぶんを取る口。**無い日は `None`**（飛ばす。補間しない）。
type ReadDay = Callable[[date], bytes | None]


class NoSamplesError(RuntimeError):
    """1 日ぶんも読めなかった。"""


@dataclass(frozen=True)
class Window:
    """読めた窓。**表は持たない**——圧縮のままの中身と、日ごとの素性だけ。

    **被覆を一緒に持つのは、読んだ人にしか測れないからである。** 組み立てたときの
    数はどこにも保存されておらず、`feature_set` は「列が在る」しか語らない
    （W4 プラン §8.5.3）。ここで数えておけば、**すでに在る日**にも効く。
    """

    #: 実際に読めた日（無い日は飛ばしてある）。**分割はこれで切る。**
    days: tuple[date, ...]
    #: 日ごとの行数。**先に場所を確保する**ために要る（`matrix_of`）
    rows: Mapping[date, int]
    #: 日ごとの天気の被覆。**表を開く前に数え終わる**ので、門は当てはめの前に通る
    weather: Mapping[date, coverage.Coverage]
    #: 日ごとの Parquet そのもの。**解かずに持つ**（1 日 15〜41 MB）
    bodies: Mapping[date, bytes]

    def __len__(self) -> int:
        return sum(self.rows.values())


def days_between(start: date, end: date) -> tuple[date, ...]:
    """両端を含む JST の暦日。"""
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def day_reader(source: SupabaseIo | None, local: Path | None) -> ReadDay:
    """1 日ぶんを取る口を作る。**プロファイルと同じ読み方**（`jobs/climate.py`）。"""

    def read(day: date) -> bytes | None:
        return climate.read_bytes(source, features_path(day), local)

    return read


def read_window(read: ReadDay, days: Sequence[date]) -> Window:
    """日ごとに取って、**天気の被覆と行数だけ**を先に数える。**表は作らない。**

    開くのは**天気の 4 列だけ**である。被覆は「4 列すべてが入っている行」の割合
    なので列を開かずには数えられないが、4 列なら 1 日ぶんで数十 MB で済む。
    残りの列は、門を通ったあとで**日ごとに**開く（`samples_of`・`matrix_of`）。
    """
    bodies: dict[date, bytes] = {}
    rows: dict[date, int] = {}
    weather: dict[date, coverage.Coverage] = {}
    for day in days:
        body = read(day)
        if body is None:
            continue
        table = _weather_only(body)
        bodies[day] = body
        rows[day] = table.num_rows
        weather[day] = coverage.measure(table)
    if not bodies:
        raise NoSamplesError("学習サンプルが 1 日ぶんも見つかりません")
    return Window(days=tuple(bodies), rows=rows, weather=weather, bodies=bodies)


def samples_of(window: Window) -> Samples:
    """窓ぜんぶのサンプル。**日ごとに開いて、開いたそばから表を捨てる。**

    **文字列がここで消える。** `system_id` と `station_id` を窓ぜんぶぶん
    Python の文字列にすると、1,800 万行では数 GB になる（`eval/dataset.py` の
    `_strings`）。日ごとなら 60 万行で、あとに残るのは語彙と番号だけである。
    """
    return merge([to_chunk(_open(window.bodies[day], NEEDED_COLUMNS)) for day in window.days])


def matrix_of(window: Window, days: Sequence[date]) -> matrix.Matrix:
    """その日ぶんだけの行列。**1 日読んでは区間を埋めて捨てる**（§12 の 168）。

    **窓に無い日を渡せない**（`rows_of` が `KeyError` で止まる）。黙って短い行列を
    作ると、**埋め残した行がゴミのまま学習に入る**。
    """
    built = matrix.empty(rows_of(window, days))
    at = 0
    for day in days:
        at = matrix.fill(built, at, _open(window.bodies[day], matrix.MODEL_COLUMNS))
    matrix.refuse_unfilled(built, at)
    return built


def rows_of(window: Window, days: Sequence[date]) -> int:
    """その日ぶんの行数の合計。**読めなかった日を混ぜたら止まる。**"""
    return sum(window.rows[day] for day in days)


def _weather_only(body: bytes) -> pa.Table:
    """天気の 4 列だけ開く。**列が無ければ「被覆 0%」と区別して止める。**"""
    parquet = pq.ParquetFile(io.BytesIO(body))
    coverage.refuse_missing_weather(parquet.schema_arrow.names)
    return parquet.read(columns=list(WEATHER_COLUMNS))


def _open(body: bytes, columns: Sequence[str]) -> pa.Table:
    """1 日ぶんの Parquet を、**要る列だけ**開く。"""
    return pq.read_table(io.BytesIO(body), columns=list(columns))
