"""学習サンプルの読み込み（`jobs/evaluate_baselines.py` の `load_days`）。

**主題は「読んだ人が、読んだものの素性を持って帰ること」**（W4 プラン §8.5.3、PR I）。

被覆は**読む列を絞る前**に数える。ベースラインは 11 列しか読まず、そこに天気は無い
（`NEEDED_COLUMNS`）。絞ったあとの表で数えると **「絞ったから 0%」と「本当に 0%」が
区別できない**——だから絞る前に数え、絞った表で数えようとしたら
`MissingWeatherColumnsError` で止まるようにしてある。

**日を 2 つ作る。** 片方は天気入り、片方は 4 列すべて NULL（アーカイブが始まる前の
日と同じ形）。**同じ数だと取り違えても気づけない**ので、0% と 100% を使う。
"""

from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from bikechance_ml.eval.dataset import NEEDED_COLUMNS
from bikechance_ml.features import build, coverage
from bikechance_ml.features.grid import features_path
from bikechance_ml.features.schema import WEATHER_COLUMNS
from bikechance_ml.jobs.build_features import to_parquet_bytes
from bikechance_ml.jobs.evaluate_baselines import NoSamplesError, load_days
from tests import features_fixture as fixture

GOLDEN: Final[pa.Table] = build.build_day(fixture.build_inputs()).table

#: 天気の入っている日と、入っていない日。**被覆は 100% と 0%。**
DAY_WITH: Final[date] = date(2026, 9, 8)
DAY_WITHOUT: Final[date] = date(2026, 9, 7)
BOTH: Final[tuple[date, ...]] = (DAY_WITHOUT, DAY_WITH)


def _nulled(table: pa.Table) -> pa.Table:
    """天気 4 列を全部 NULL にした表。**アーカイブが始まる前の日と同じ形。**"""
    changed = table
    for name in WEATHER_COLUMNS:
        index = changed.schema.get_field_index(name)
        field = changed.schema.field(index)
        changed = changed.set_column(index, field, pa.nulls(changed.num_rows, field.type))
    return changed


def write_samples(root: Path, days: Mapping[date, bool]) -> Path:
    """`features/date=…/part.parquet` を置く。**`True` が天気入り、`False` が 4 列 NULL。**

    **`test_fit_lightgbm.py` からも使う**（門が `run` まで届いていることを見る）ので
    公開してある。
    """
    for day, has_weather in days.items():
        path = root / features_path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(to_parquet_bytes(GOLDEN if has_weather else _nulled(GOLDEN)))
    return root


@pytest.fixture
def samples(tmp_path: Path) -> Path:
    """`features/date=…/part.parquet` を 2 日ぶん置く。"""
    return write_samples(tmp_path, {DAY_WITHOUT: False, DAY_WITH: True})


# ── 読めた日 ──────────────────────────────────────────────────
def test_the_days_that_exist_are_read(samples: Path) -> None:
    loaded = load_days(None, BOTH, samples)
    assert loaded.days == BOTH
    assert loaded.table.num_rows == GOLDEN.num_rows * 2


def test_a_missing_day_is_skipped(samples: Path) -> None:
    """**無い日は飛ばす**（補間しない。CLAUDE.md §6）。"""
    loaded = load_days(None, (*BOTH, date(2026, 9, 30)), samples)
    assert loaded.days == BOTH


def test_no_samples_at_all_is_loud(tmp_path: Path) -> None:
    with pytest.raises(NoSamplesError):
        load_days(None, BOTH, tmp_path)


# ── 被覆 ──────────────────────────────────────────────────────
def test_every_read_day_gets_a_coverage(samples: Path) -> None:
    """**読めた日と被覆の日が一致する。** 片方だけ増える道を作らない。"""
    loaded = load_days(None, BOTH, samples)
    assert set(loaded.weather) == set(loaded.days)


def test_the_coverage_survives_the_column_projection(samples: Path) -> None:
    """**ここが要。** 既定は 11 列に絞るが、被覆はそれより前に数えてある。

    絞ったあとの表に天気の列が無いことも一緒に確かめる。**無いのに数が出ている**
    ことが、「絞る前に数えている」の証拠である。
    """
    loaded = load_days(None, BOTH, samples, columns=NEEDED_COLUMNS)

    assert not set(loaded.table.column_names) & set(WEATHER_COLUMNS)
    assert loaded.weather[DAY_WITHOUT].ratio == 0.0
    assert loaded.weather[DAY_WITH].ratio == 1.0


def test_reading_every_column_gives_the_same_coverage(samples: Path) -> None:
    """**絞っても絞らなくても同じ数。** LightGBM は 62 列を読む。"""
    narrow = load_days(None, BOTH, samples, columns=NEEDED_COLUMNS)
    wide = load_days(None, BOTH, samples, columns=None)
    assert narrow.weather == wide.weather
    assert set(WEATHER_COLUMNS) <= set(wide.table.column_names)


def test_the_coverage_matches_the_file_on_disk(samples: Path) -> None:
    """**別経路で検算する。** 読み込みの戻り値ではなく、置いたファイルを数え直す。

    要約どうしを突き合わせると、**両方を同じように間違えたときに気づけない**
    （W4 プラン §8.5.8 で 1 度そうなった）。
    """
    loaded = load_days(None, BOTH, samples)
    for day in BOTH:
        again = coverage.measure(pq.read_table(samples / features_path(day)))
        assert loaded.weather[day] == again, f"{day} の被覆が読み直しと違う"


def test_the_two_days_are_not_the_same_number(samples: Path) -> None:
    """**0% と 100% を使っている。** 同じ数だと取り違えても気づけない。"""
    loaded = load_days(None, BOTH, samples)
    assert loaded.weather[DAY_WITHOUT].ratio != loaded.weather[DAY_WITH].ratio
    assert loaded.weather[DAY_WITHOUT].rows == loaded.weather[DAY_WITH].rows
