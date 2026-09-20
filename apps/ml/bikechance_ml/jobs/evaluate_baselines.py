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
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from bikechance_ml.baselines import climatology
from bikechance_ml.config import read_storage_config
from bikechance_ml.eval import harness, report
from bikechance_ml.eval.dataset import Samples
from bikechance_ml.eval.split import DaySplit, split_days
from bikechance_ml.io.supabase import SupabaseIo, open_storage
from bikechance_ml.jobs import climate
from bikechance_ml.jobs.window import Window, day_reader, days_between, read_window, samples_of


@dataclass(frozen=True)
class Prepared:
    """読んで、日を分け、**B2 の作り方まで決めたところ**。Storage を閉じたあとに使う。"""

    window: Window
    samples: Samples
    split: DaySplit
    climate: climatology.Source


def prepare(
    source: SupabaseIo | None,
    days: Sequence[date],
    local: Path | None,
    options: argparse.Namespace,
) -> Prepared:
    """読み込みを 1 か所にまとめる。**Storage を開いていても手元でも同じ順**で進む。

    **窓は表を持たない**（`jobs/window.py`）。日ごとに開いてサンプルにするので、
    ベースラインでも「全日を 1 つの表につないでから開く」をしない。
    """
    window = read_window(day_reader(source, local), days)
    samples = samples_of(window)
    split = split_days(window.days, options.eval_days, options.purge_days)
    return Prepared(
        window=window,
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
    """B2 の作り方を決める。**読むのは学習期間の日だけ**（検証日を混ぜない）。

    **決め方そのものは `jobs/climate.py` に 1 つだけ置く**（W5 プラン §12 の 166）。
    ここで同じ条件を書き直すと、**片方だけ直したときに静かに食い違う**——実際に
    `fit_lightgbm` がそうなっていた。
    """
    return climate.source_for(source, split.fit, local, samples.ports, from_profiles=not no_profile)


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
        outcome, options.title, options.note, weather=prepared.window.weather
    )

    if options.out is None:
        print(text)
    else:
        Path(options.out).write_text(text, encoding="utf-8")
        print(f"書き出しました: {options.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
