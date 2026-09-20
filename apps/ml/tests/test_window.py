"""窓の読み込み（`jobs/window.py`）。

**主題は 2 つある。**

1. **読んだ人が、読んだものの素性を持って帰る**（W4 プラン §8.5.3、PR I）。天気の
   被覆は**表を開く前**に数える。絞ったあとの表で数えると「絞ったから 0%」と
   「本当に 0%」が区別できないので、**列の名前だけで**止める道も残してある
2. **日ごとに読んでも、全日を 1 つの表にしたときと同じ答えになる**（W5 プラン §12 の
   168 の 2 段目）。**速くするための書き換えではなく、山を下げるための書き換え**
   なので、**答えが変わらないこと**を突き合わせで留める

**日を 2 つ作る。** 片方は天気入り、片方は 4 列すべて NULL（アーカイブが始まる前の
日と同じ形）。**同じ数だと取り違えても気づけない**ので、0% と 100% を使う。
突き合わせに使う 2 日は、**ポートの集合そのものを変える**——同じポートしか出て
こない日を並べると、**番号を振り直さなくても一致してしまう**。
"""

from collections.abc import Mapping, Sequence
from dataclasses import fields
from datetime import date
from pathlib import Path
from typing import Final

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from bikechance_ml.eval.dataset import NEEDED_COLUMNS, Samples, jst_ordinal, to_samples
from bikechance_ml.eval.split import mask_of
from bikechance_ml.features import build, coverage
from bikechance_ml.features.grid import features_path
from bikechance_ml.features.schema import WEATHER_COLUMNS
from bikechance_ml.jobs import window
from bikechance_ml.jobs.build_features import to_parquet_bytes
from bikechance_ml.models import matrix
from tests import eval_fixture as eval_rows
from tests import features_fixture as fixture

GOLDEN: Final[pa.Table] = build.build_day(fixture.build_inputs()).table

#: 天気の入っている日と、入っていない日。**被覆は 100% と 0%。**
DAY_WITH: Final[date] = date(2026, 9, 8)
DAY_WITHOUT: Final[date] = date(2026, 9, 7)
BOTH: Final[tuple[date, ...]] = (DAY_WITHOUT, DAY_WITH)

#: 1 日のミリ秒（`t` をずらすのに使う）。
_MS_PER_DAY: Final[int] = 86_400_000


def _nulled(table: pa.Table) -> pa.Table:
    """天気 4 列を全部 NULL にした表。**アーカイブが始まる前の日と同じ形。**"""
    changed = table
    for name in WEATHER_COLUMNS:
        index = changed.schema.get_field_index(name)
        field = changed.schema.field(index)
        changed = changed.set_column(index, field, pa.nulls(changed.num_rows, field.type))
    return changed


def _restationed(table: pa.Table, mark: str) -> pa.Table:
    """`station_id` に印を足した表。**日ごとにポートの集合を変える**ために使う。"""
    index = table.schema.get_field_index("station_id")
    renamed = [f"{one}-{mark}" for one in table.column("station_id").to_pylist()]
    return table.set_column(index, table.schema.field(index), pa.array(renamed, type=pa.string()))


def _on_day(table: pa.Table, day: date) -> pa.Table:
    """`t` をその日にずらした表。**パスの日と中身の日を合わせる。**

    フィクスチャの `t` は作った日のままなので、そのまま別の日のパスに置くと
    **日で切る検査が何も見ていない**ことになる（`mask_of` が空を選ぶ）。
    """
    index = table.schema.get_field_index("t")
    field = table.schema.field(index)
    shift = (day.toordinal() - int(jst_ordinal(table.column("t")).min())) * _MS_PER_DAY
    moved = pc.add(table.column("t").cast(pa.int64()), shift).cast(field.type)
    return table.set_column(index, field, moved)


def write_day(root: Path, day: date, table: pa.Table) -> None:
    """`features/date=…/part.parquet` を 1 日ぶん置く。**中身もその日にする。**"""
    path = root / features_path(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(to_parquet_bytes(_on_day(table, day)))


def write_samples(root: Path, days: Mapping[date, bool]) -> Path:
    """**`True` が天気入り、`False` が 4 列 NULL。**

    **`test_fit_lightgbm.py` からも使う**（門が `run` まで届いていることを見る）ので
    公開してある。
    """
    for day, has_weather in days.items():
        write_day(root, day, GOLDEN if has_weather else _nulled(GOLDEN))
    return root


def write_distinct_sizes(root: Path) -> Path:
    """3 日ぶんを、**日ごとに行数を変えて**置く（天気はすべて入り）。

    **同じ行数の日を並べると、検証と学習を取り違えても同じ数になる**——
    「どの日から作ったか」を数で見分けられるようにしておく。
    """
    for index, day in enumerate(eval_rows.DAYS):
        write_day(root, day, GOLDEN.slice(0, GOLDEN.num_rows - index * 2))
    return root


def read_local(root: Path, days: Sequence[date]) -> window.Window:
    """手元に置いたファイルから窓を開く。"""
    return window.read_window(window.day_reader(None, root), days)


@pytest.fixture
def samples(tmp_path: Path) -> Path:
    """`features/date=…/part.parquet` を 2 日ぶん置く。"""
    return write_samples(tmp_path, {DAY_WITHOUT: False, DAY_WITH: True})


@pytest.fixture
def distinct(tmp_path: Path) -> tuple[window.Window, pa.Table]:
    """**ポートの集合が違う 2 日**を置いて、窓と「1 つにつないだ表」を返す。"""
    tables = {
        DAY_WITHOUT: _on_day(_restationed(GOLDEN, "a"), DAY_WITHOUT),
        DAY_WITH: _on_day(_restationed(GOLDEN, "b"), DAY_WITH),
    }
    for day, table in tables.items():
        write_day(tmp_path, day, table)
    return read_local(tmp_path, tuple(tables)), pa.concat_tables(list(tables.values()))


# ── 読めた日 ──────────────────────────────────────────────────
def test_the_days_that_exist_are_read(samples: Path) -> None:
    opened = read_local(samples, BOTH)
    assert opened.days == BOTH
    assert len(opened) == GOLDEN.num_rows * 2


def test_a_missing_day_is_skipped(samples: Path) -> None:
    """**無い日は飛ばす**（補間しない。CLAUDE.md §6）。"""
    assert read_local(samples, (*BOTH, date(2026, 9, 30))).days == BOTH


def test_no_samples_at_all_is_loud(tmp_path: Path) -> None:
    with pytest.raises(window.NoSamplesError):
        read_local(tmp_path, BOTH)


def test_the_rows_match_the_file_on_disk(samples: Path) -> None:
    """**行数は先に数えておく。** 行列の場所を先に確保するのに要る（`matrix_of`）。"""
    opened = read_local(samples, BOTH)
    for day in BOTH:
        assert opened.rows[day] == pq.read_table(samples / features_path(day)).num_rows


def test_the_body_is_the_file_itself(samples: Path) -> None:
    """**解かずに持つ。** 2 度目の取得をしないためで、中身は置いたファイルそのもの。"""
    opened = read_local(samples, BOTH)
    for day in BOTH:
        assert opened.bodies[day] == (samples / features_path(day)).read_bytes()


def test_the_window_does_not_hold_a_table(samples: Path) -> None:
    """**窓は表を持たない。** 持った瞬間に「日ごとに読む」意味が消える。

    **大きさでは測らない。** フィクスチャでは Parquet の見出しのほうが中身より
    大きく（69 列 × 数百行で 41 KB 対 11 KB）、比が本番と逆に出る。効くのは
    1 日 60 万行の規模で、そこでは解いた表が圧縮のままの 25 倍になる（§12 の 168）。
    ここで留めるのは**宣言そのもの**である。
    """
    opened = read_local(samples, BOTH)
    declared = {one.name: str(one.type) for one in fields(window.Window)}
    assert not any("Table" in kind for kind in declared.values()), declared
    assert all(isinstance(body, bytes) for body in opened.bodies.values())


# ── 被覆 ──────────────────────────────────────────────────────
def test_every_read_day_gets_a_coverage(samples: Path) -> None:
    """**読めた日と被覆の日が一致する。** 片方だけ増える道を作らない。"""
    opened = read_local(samples, BOTH)
    assert set(opened.weather) == set(opened.days)


def test_the_coverage_matches_the_file_on_disk(samples: Path) -> None:
    """**別経路で検算する。** 読み込みの戻り値ではなく、置いたファイルを数え直す。

    要約どうしを突き合わせると、**両方を同じように間違えたときに気づけない**
    （W4 プラン §8.5.8 で 1 度そうなった）。
    """
    opened = read_local(samples, BOTH)
    for day in BOTH:
        again = coverage.measure(pq.read_table(samples / features_path(day)))
        assert opened.weather[day] == again, f"{day} の被覆が読み直しと違う"


def test_the_two_days_are_not_the_same_number(samples: Path) -> None:
    """**0% と 100% を使っている。** 同じ数だと取り違えても気づけない。"""
    opened = read_local(samples, BOTH)
    assert opened.weather[DAY_WITHOUT].ratio != opened.weather[DAY_WITH].ratio
    assert opened.weather[DAY_WITHOUT].rows == opened.weather[DAY_WITH].rows


def test_only_the_weather_columns_are_opened_first(
    samples: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**先に開くのは天気の 4 列だけ。** ここが太ると「日ごとに読む」意味が薄れる。

    被覆を数えるところに渡ってくる表の列を見る。**数え方の中身ではなく、
    何を開いたか**を留めるのがここの役目である。
    """
    seen: list[tuple[str, ...]] = []
    original = coverage.measure

    def spy(table: pa.Table) -> coverage.Coverage:
        seen.append(tuple(table.column_names))
        return original(table)

    monkeypatch.setattr(coverage, "measure", spy)
    read_local(samples, BOTH)

    assert seen, "被覆を数えていません"
    assert all(names == tuple(WEATHER_COLUMNS) for names in seen), seen


def test_a_file_without_weather_columns_stops(tmp_path: Path) -> None:
    """**「天気の列が無い」を「被覆 0%」と読み違えない。** 見出しだけで止める。"""
    write_day(tmp_path, DAY_WITH, GOLDEN.drop_columns(list(WEATHER_COLUMNS)))
    with pytest.raises(coverage.MissingWeatherColumnsError):
        read_local(tmp_path, (DAY_WITH,))


# ── 日ごとに読んでも答えが変わらない（§12 の 168 の 2 段目）──────
def _same_samples(left: Samples, right: Samples) -> None:
    """2 つの `Samples` が**完全に同じ**であること。"""
    assert left.systems == right.systems
    assert left.ports == right.ports
    assert np.array_equal(left.system, right.system)
    assert np.array_equal(left.port, right.port)
    assert np.array_equal(left.day, right.day)
    assert np.array_equal(left.h_min, right.h_min)
    assert np.array_equal(left.minute_of_day, right.minute_of_day)
    assert np.array_equal(left.dow_type, right.dow_type)
    assert np.array_equal(left.weight, right.weight)
    assert sorted(left.labels) == sorted(right.labels)
    for label, labels in left.labels.items():
        assert np.array_equal(labels, right.labels[label]), label
    for name, counts in left.counts.items():
        assert np.array_equal(counts, right.counts[name]), name


def test_the_samples_match_the_single_table_version(
    distinct: tuple[window.Window, pa.Table],
) -> None:
    """**この PR の主題。** 日ごとに開いた `Samples` が、つないだ表のそれと一致する。"""
    opened, whole = distinct
    _same_samples(window.samples_of(opened), to_samples(whole.select(list(NEEDED_COLUMNS))))


def test_the_ports_of_every_day_are_in_the_vocabulary(
    distinct: tuple[window.Window, pa.Table],
) -> None:
    """**語彙は日をまたいで合流する。** 2 日目にしか出ないポートも番号を持つ。"""
    opened, whole = distinct
    built = window.samples_of(opened)
    assert len(built.ports) == len(set(whole.column("station_id").to_pylist()))
    assert any(one.endswith("-a") for one in built.ports)
    assert any(one.endswith("-b") for one in built.ports)


def test_the_matrix_matches_the_single_table_version(
    distinct: tuple[window.Window, pa.Table],
) -> None:
    """**行列も一致する。** 日ごとに区間を埋めても、まとめて作ったのと同じ値になる。"""
    opened, whole = distinct
    built = window.samples_of(opened)
    for days in ((DAY_WITHOUT,), (DAY_WITH,), opened.days):
        streamed = window.matrix_of(opened, days)
        at_once = matrix.build(whole, mask_of(built, days))
        assert np.array_equal(streamed.values, at_once.values, equal_nan=True), days


def test_the_matrix_is_filled_one_day_at_a_time(
    distinct: tuple[window.Window, pa.Table], monkeypatch: pytest.MonkeyPatch
) -> None:
    """**1 日読んでは埋めて捨てる。** まとめて 1 つの表にしてから埋めていない。"""
    opened, _ = distinct
    rows: list[int] = []
    original = matrix.fill

    def spy(into: matrix.Matrix, at: int, table: pa.Table, keep: None = None) -> int:
        rows.append(table.num_rows)
        return original(into, at, table, keep)

    monkeypatch.setattr(matrix, "fill", spy)
    window.matrix_of(opened, opened.days)

    assert rows == [opened.rows[day] for day in opened.days], rows


def test_a_day_outside_the_window_stops(distinct: tuple[window.Window, pa.Table]) -> None:
    """**読めなかった日を混ぜたら止まる。** 黙って短い行列を作らない。"""
    opened, _ = distinct
    with pytest.raises(KeyError):
        window.matrix_of(opened, (*opened.days, date(2026, 9, 30)))


def test_an_unfilled_matrix_stops(
    distinct: tuple[window.Window, pa.Table], monkeypatch: pytest.MonkeyPatch
) -> None:
    """**埋め残しを許さない。** `np.empty` の中身は未定義で、例外は出ない。

    行数を 1 行多く言わせると、最後まで埋まらない。**そのまま返すと、ゴミの行が
    学習に入る**——確率だけが静かに変わる。
    """
    opened, _ = distinct
    monkeypatch.setattr(window, "rows_of", lambda one, days: len(one) + 1)
    with pytest.raises(matrix.UnfilledMatrixError):
        window.matrix_of(opened, opened.days)
