"""ベースライン B0〜B3 を測る（W3 プラン §5.9）。

**ここが副作用の置き場所。** 分割・当てはめ・評価の規則は `eval/` と `baselines/` に
あり、このファイルは「Storage から学習サンプルを取る」「Markdown を書き出す」だけを
持つ（CLAUDE.md §3）。

使い方（環境変数は `.env` から読み込んでから）:

    set -a; . ./.env; set +a
    cd apps/ml
    ./.venv/bin/python -m bikechance_ml.jobs.evaluate_baselines \\
        --from 2026-09-06 --to 2026-09-08 --eval-days 1 \\
        --local .cache/features --out ../../docs/260908_eda_02_baseline.md

`--from` / `--to` は **JST の暦日**（両端を含む）。`--local` を指すと、Storage の
代わりにそこの `features/date=…/part.parquet` を読む（`build_features --out` で
書いたものをそのまま使える）。

**日が足りなければ止まる。** 学習・パージ・検証を切り分けられないまま進むと、
何を測ったのか分からない表が出る。
"""

import argparse
import io as _io
import sys
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from bikechance_ml.config import read_storage_config
from bikechance_ml.eval import harness, report
from bikechance_ml.eval.dataset import NEEDED_COLUMNS, concat, to_samples
from bikechance_ml.eval.split import split_days
from bikechance_ml.features.grid import features_path
from bikechance_ml.io.supabase import PARQUET_BUCKET, SupabaseIo, open_storage


class NoSamplesError(RuntimeError):
    """1 日ぶんも読めなかった。"""


def days_between(start: date, end: date) -> tuple[date, ...]:
    """両端を含む JST の暦日。"""
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def load_days(
    source: SupabaseIo | None,
    days: Sequence[date],
    local: Path | None,
    columns: Sequence[str] | None = NEEDED_COLUMNS,
) -> tuple[pa.Table, tuple[date, ...]]:
    """日ごとのサンプルを読む。**無い日は飛ばし、読めた日を返す。**

    `columns` を `None` にすると**全列**を返す。ベースラインが読むのは 11 列だけだが、
    LightGBM は 62 列を使う（`jobs/fit_lightgbm.py`）。
    """
    tables: list[pa.Table] = []
    found: list[date] = []
    for day in days:
        body = _one_day(source, day, local)
        if body is None:
            continue
        tables.append(pq.read_table(_io.BytesIO(body)))
        found.append(day)
    if not tables:
        raise NoSamplesError("学習サンプルが 1 日ぶんも見つかりません")
    return concat(tables, columns), tuple(found)


def _one_day(source: SupabaseIo | None, day: date, local: Path | None) -> bytes | None:
    if local is not None:
        path = local / features_path(day)
        return path.read_bytes() if path.exists() else None
    if source is None:  # pragma: no cover - 呼ぶ側が必ずどちらかを渡す
        raise ValueError("Storage か --local のどちらかが要ります")
    return source.download(PARQUET_BUCKET, features_path(day))


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
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    options = _arguments(argv)
    days = days_between(date.fromisoformat(options.start), date.fromisoformat(options.end))
    local = Path(options.local) if options.local else None

    if local is not None:
        table, found = load_days(None, days, local)
    else:
        with open_storage(read_storage_config()) as source:
            table, found = load_days(source, days, None)

    samples = to_samples(table)
    split = split_days(found, options.eval_days, options.purge_days)
    outcome = harness.run(samples, split)
    text = report.render_markdown(outcome, options.title, options.note)

    if options.out is None:
        print(text)
    else:
        Path(options.out).write_text(text, encoding="utf-8")
        print(f"書き出しました: {options.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
