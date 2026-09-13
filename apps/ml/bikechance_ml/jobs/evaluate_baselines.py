"""ベースライン B0〜B3 を測る（W3 プラン §5.9）。

**ここが副作用の置き場所。** 分割・当てはめ・評価の規則は `eval/` と `baselines/` に
あり、このファイルは「Storage から学習サンプルを取る」「Markdown を書き出す」だけを
持つ（CLAUDE.md §3）。

使い方（環境変数は `.env` から読み込んでから）:

    set -a; . ./.env; set +a
    cd apps/ml
    ./.venv/bin/python -m bikechance_ml.jobs.evaluate_baselines \\
        --from 2026-09-07 --to 2026-09-11 --eval-days 1 \\
        --local .cache --out ../../docs/260908_eda_02_baseline.md

`--from` / `--to` は **JST の暦日**（両端を含む）。`--local` を指すと、Storage の
代わりにそこの `features/date=…/part.parquet`（と `profiles/date=…`）を読む。

**B2 はプロファイルから作る**（W5 プラン §6.4 の PR D）。読むのは**学習の最終日の版**で、
検証日は 1 点も入っていない。無ければ従来どおり学習サンプルの行から作る。
**`--no-profile` を付けると常に従来のやり方**になる（入れ替える前後を比べるため）。

**日が足りなければ止まる。** 学習・パージ・検証を切り分けられないまま進むと、
何を測ったのか分からない表が出る。
"""

import argparse
import io
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from bikechance_ml.baselines import climatology
from bikechance_ml.config import read_storage_config
from bikechance_ml.eval import harness, report
from bikechance_ml.eval.dataset import NEEDED_COLUMNS, Samples, concat, to_samples
from bikechance_ml.eval.split import DaySplit, split_days
from bikechance_ml.features import coverage
from bikechance_ml.features.grid import features_path
from bikechance_ml.io.supabase import SupabaseIo, open_storage
from bikechance_ml.jobs import climate


class NoSamplesError(RuntimeError):
    """1 日ぶんも読めなかった。"""


@dataclass(frozen=True)
class Loaded:
    """読めた学習サンプル。**表と、読めた日と、日ごとの天気の被覆。**

    **被覆を一緒に返すのは、読んだ人にしか測れないからである。** 組み立てたときの数は
    どこにも保存されておらず、`feature_set` は「列が在る」しか語らない（§8.5.3）。
    ここで数えておけば、**すでに在る日**にも効き、作り直しても食い違わない。
    """

    table: pa.Table
    #: 実際に読めた日（無い日は飛ばしてある）。**分割はこれで切る。**
    days: tuple[date, ...]
    #: 日ごとの天気の被覆。**読む列を絞る前**の表から数える
    weather: Mapping[date, coverage.Coverage]


def days_between(start: date, end: date) -> tuple[date, ...]:
    """両端を含む JST の暦日。"""
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def load_days(
    source: SupabaseIo | None,
    days: Sequence[date],
    local: Path | None,
    columns: Sequence[str] | None = NEEDED_COLUMNS,
) -> Loaded:
    """日ごとのサンプルを読む。**無い日は飛ばし、読めた日を返す。**

    `columns` を `None` にすると**全列**を返す。ベースラインが読むのは 11 列だけだが、
    LightGBM は 62 列を使う（`jobs/fit_lightgbm.py`）。

    **被覆は列を絞る前に数える。** 絞ったあとの表には天気の列が無く、そこで数えると
    「絞ったから 0」と「本当に 0」が区別できない。絞る側に移したら
    `coverage.measure` が `MissingWeatherColumnsError` で止める。
    """
    tables: list[pa.Table] = []
    found: list[date] = []
    weather: dict[date, coverage.Coverage] = {}
    for day in days:
        body = _one_day(source, day, local)
        if body is None:
            continue
        table = pq.read_table(io.BytesIO(body))
        tables.append(table)
        found.append(day)
        weather[day] = coverage.measure(table)
    if not tables:
        raise NoSamplesError("学習サンプルが 1 日ぶんも見つかりません")
    return Loaded(table=concat(tables, columns), days=tuple(found), weather=weather)


def _one_day(source: SupabaseIo | None, day: date, local: Path | None) -> bytes | None:
    """1 日ぶんの学習サンプル。**プロファイルと同じ読み方**（`jobs/climate.py`）。"""
    return climate.read_bytes(source, features_path(day), local)


@dataclass(frozen=True)
class Prepared:
    """読んで、日を分け、**B2 の作り方まで決めたところ**。Storage を閉じたあとに使う。"""

    loaded: Loaded
    samples: Samples
    split: DaySplit
    climate: climatology.Source


def prepare(
    source: SupabaseIo | None,
    days: Sequence[date],
    local: Path | None,
    options: argparse.Namespace,
) -> Prepared:
    """読み込みを 1 か所にまとめる。**Storage を開いていても手元でも同じ順**で進む。"""
    loaded = load_days(source, days, local)
    samples = to_samples(loaded.table)
    split = split_days(loaded.days, options.eval_days, options.purge_days)
    return Prepared(
        loaded=loaded,
        samples=samples,
        split=split,
        climate=_climate(source, split, local, samples, options.no_profile),
    )


def _climate(
    source: SupabaseIo | None,
    split: DaySplit,
    local: Path | None,
    samples: Samples,
    no_profile: bool,
) -> climatology.Source:
    """B2 の作り方を決める。**読むのは学習期間の日だけ**（検証日を混ぜない）。"""
    if no_profile:
        return climatology.FromSamples()
    found = climate.load(source, split.fit, local, samples.ports)
    return found if found is not None else climatology.FromSamples()


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ベースライン B0〜B3 を測る")
    parser.add_argument("--from", dest="start", required=True, help="JST の暦日（含む）")
    parser.add_argument("--to", dest="end", required=True, help="JST の暦日（含む）")
    parser.add_argument("--eval-days", type=int, default=1, help="検証に使う日数")
    parser.add_argument("--purge-days", type=int, default=1, help="学習と検証の間に空ける日数")
    parser.add_argument("--local", default=None, help="Storage の代わりに読む場所")
    parser.add_argument("--out", default=None, help="Markdown の出力先。省略すると標準出力")
    parser.add_argument("--title", default="ベースライン B0〜B3（W3 段 7）")
    parser.add_argument("--note", default="", help="表の前に置く但し書き")
    parser.add_argument(
        "--no-profile", action="store_true", help="気候値を学習サンプルの行から作る（従来）"
    )
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    options = _arguments(argv)
    days = days_between(date.fromisoformat(options.start), date.fromisoformat(options.end))
    local = Path(options.local) if options.local else None

    if local is not None:
        prepared = prepare(None, days, local, options)
    else:
        with open_storage(read_storage_config()) as remote:
            prepared = prepare(remote, days, None, options)

    outcome = harness.run(prepared.samples, prepared.split, climate=prepared.climate)
    # **被覆は出すが、ここでは止めない。** ベースラインは天気の列を読まない
    # （`NEEDED_COLUMNS` に無い）ので、混ざっても B0〜B3 の数字は変わらない。
    # 止めるのは成果物を書く `fit_lightgbm` だけである（W4 プラン §6.8 の PR I）
    text = report.render_markdown(
        outcome, options.title, options.note, weather=prepared.loaded.weather
    )

    if options.out is None:
        print(text)
    else:
        Path(options.out).write_text(text, encoding="utf-8")
        print(f"書き出しました: {options.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
