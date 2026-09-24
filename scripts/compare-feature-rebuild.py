#!/usr/bin/env python3
"""`features/` を作り直す前と後を突き合わせる（W5 プラン §6.10 の J1 の完了条件 5）。

**作り直しは「列を足す」だけのはずである。** 版を上げて（v3 → v4）全日を作り直すとき、
**前からある列が 1 つでも動いていたら、それは列を足した以上のことをしている**。だから
作り直す前のファイルを手元に退避しておき、作り直した後のファイルと日ごとに比べる。

日によって「同じはず」の中身が違う。

  * **一様抽出の日**（2026-09-16 以降。`stratum` がすべて `uniform`）…… 前からある列
    （`feature_set` を除く）が**完全に一致する**
  * **層化抽出の日**（2026-09-15 以前）…… 行は変わる（抽出が一様になる）。**一様で作った
    手元の版**（`--uniform`）が在ればそれと完全に一致する。無ければ天気の被覆の差だけを見る

**天気の被覆**（`coverage.measure`）と、**新しく足した列の被覆**（`coverage.measure_profile`）も
並べて出す。作り直しで天気が欠けたら（`weather_hourly` の 30 日保持を過ぎた日など）、
ここで分かる。

使い方（環境変数は `.env` から読み込んでから。**読むだけで、何も置かない**）:

    cd apps/ml
    ./.venv/bin/python ../../scripts/compare-feature-rebuild.py \\
        --backup .cache/j1-backup --from 2026-09-07 --to 2026-09-23 \\
        --uniform .cache/uniform

`--backup` の下は Storage のパスそのまま（`features/date=…/part.parquet`）。`--after` を
付けると、作り直した後のファイルも Storage ではなく手元のその場所から読む（予行演習用）。
**合格しなければ終了コード 1。**
"""

import argparse
import io
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from bikechance_ml.config import read_storage_config
from bikechance_ml.features import coverage
from bikechance_ml.features.constants import FEATURE_SET, STRATUM_UNIFORM
from bikechance_ml.features.grid import features_path
from bikechance_ml.io.supabase import PARQUET_BUCKET, SupabaseIo, open_storage

#: 層化の日で、手元の一様の版が無いときに許す天気の被覆の差（パーセントポイント）。
#: **抽出が変わるので完全には一致しない。** 60 万行・被覆 99.98% の標本誤差は 0.002 ポイント
#: 程度なので、その 50 倍を置く。**実害の最小（1 時間の欠け ＝ 4.17 ポイント）よりずっと小さい。**
LAYERED_WEATHER_PP: Final[float] = 0.1


@dataclass(frozen=True)
class Compared:
    """1 日ぶんの突き合わせ。"""

    day: date
    rows_before: int
    rows_after: int
    uniform_before: bool
    feature_set_after: frozenset[str]
    #: 前からある列が一致したか（一様の日）。層化の日は None
    same_as_before: bool | None
    #: 手元の一様の版と一致したか（層化の日で、その版が在るとき）。無ければ None
    same_as_uniform: bool | None
    weather_before: float
    weather_after: float
    profile_after: float

    @property
    def passed(self) -> bool:
        if self.feature_set_after != frozenset({FEATURE_SET}):
            return False
        if self.same_as_before is not None:
            return self.same_as_before
        if self.same_as_uniform is not None:
            return self.same_as_uniform
        return abs(self.weather_after - self.weather_before) * 100 <= LAYERED_WEATHER_PP


def shared_columns(before: pa.Table, after: pa.Table) -> list[str]:
    """前からある列（**`feature_set` は除く**。版が変わるのは正しい）。"""
    return [
        name for name in before.column_names if name != "feature_set" and name in after.schema.names
    ]


def same_old_columns(before: pa.Table, after: pa.Table) -> bool:
    """前からある列が**行の並びまで**完全に一致するか。列が欠けていたら一致しない。

    **NaN どうしは等しいとみなす。** `lat` / `lon` は座標の無いポートで **NULL ではなく
    NaN** を持つ（v3 からの書き方）。`Table.equals` は NaN どうしを等しいとしないので、
    そのまま比べると**中身が同じ日を「不一致」と言う**（2026-09-24 の予行演習で 3 日）。
    """
    if any(name not in after.schema.names for name in before.column_names):
        return False
    return all(
        same_column(before.column(name), after.column(name))
        for name in shared_columns(before, after)
    )


def same_column(left: pa.ChunkedArray, right: pa.ChunkedArray) -> bool:
    """1 列が一致するか。**NULL の位置と、NaN の位置も含めて**同じなら一致。"""
    if left.equals(right):
        return True
    if len(left) != len(right) or not pa.types.is_floating(left.type):
        return False
    equal = pc.fill_null(pc.equal(left, right), False)
    both_nan = pc.fill_null(pc.and_(pc.is_nan(left), pc.is_nan(right)), False)
    both_null = pc.and_(pc.is_null(left), pc.is_null(right))
    return bool(pc.all(pc.or_(pc.or_(equal, both_nan), both_null)).as_py())


def compare_day(day: date, before: pa.Table, after: pa.Table, uniform: pa.Table | None) -> Compared:
    uniform_before = set(before.column("stratum").unique().to_pylist()) == {STRATUM_UNIFORM}
    return Compared(
        day=day,
        rows_before=before.num_rows,
        rows_after=after.num_rows,
        uniform_before=uniform_before,
        feature_set_after=frozenset(
            str(one) for one in after.column("feature_set").unique().to_pylist()
        ),
        same_as_before=same_old_columns(before, after) if uniform_before else None,
        same_as_uniform=(
            None if uniform_before or uniform is None else same_old_columns(uniform, after)
        ),
        weather_before=coverage.measure(before).ratio,
        weather_after=coverage.measure(after).ratio,
        profile_after=coverage.measure_profile(after).ratio,
    )


def _local(root: Path, day: date) -> pa.Table | None:
    path = root / features_path(day)
    return pq.read_table(path) if path.exists() else None


def _stored(source: SupabaseIo, day: date) -> pa.Table | None:
    body = source.download(PARQUET_BUCKET, features_path(day))
    return None if body is None else pq.read_table(io.BytesIO(body))


def _verdict(one: Compared) -> str:
    if one.same_as_before is not None:
        how = "前と一致" if one.same_as_before else "**前と不一致**"
    elif one.same_as_uniform is not None:
        how = "手元の一様の版と一致" if one.same_as_uniform else "**一様の版と不一致**"
    else:
        how = f"被覆の差 {abs(one.weather_after - one.weather_before) * 100:.3f} pt"
    return f"{'○' if one.passed else '×'} {how}"


def render(results: Sequence[Compared]) -> str:
    rows = [
        "| 日 | 前の抽出 | 行（前 → 後） | 版（後） | 天気の被覆（前 → 後） "
        "| `prof_*` の被覆 | 判定 |",
        "|---|---|---:|---|---:|---:|---|",
    ]
    for one in results:
        rows.append(
            f"| {one.day} | {'一様' if one.uniform_before else '層化'} "
            f"| {one.rows_before:,} → {one.rows_after:,} "
            f"| {', '.join(sorted(one.feature_set_after))} "
            f"| {one.weather_before:.4%} → {one.weather_after:.4%} | {one.profile_after:.4%} "
            f"| {_verdict(one)} |"
        )
    passed = sum(1 for one in results if one.passed)
    rows.append("")
    rows.append(f"**{passed} / {len(results)} 日が合格**")
    return "\n".join(rows)


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="features/ の作り直しの前後を突き合わせる")
    parser.add_argument("--backup", required=True, help="作り直す前のファイルの置き場（手元）")
    parser.add_argument("--from", dest="start", required=True, help="JST の暦日（含む）")
    parser.add_argument("--to", dest="end", required=True, help="JST の暦日（含む）")
    parser.add_argument("--uniform", default=None, help="層化の日を一様で作った手元の版")
    parser.add_argument("--after", default=None, help="作り直した後も手元から読む（予行演習）")
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    options = _arguments(argv)
    backup = Path(options.backup)
    uniform = Path(options.uniform) if options.uniform else None
    after_root = Path(options.after) if options.after else None
    days = _days(date.fromisoformat(options.start), date.fromisoformat(options.end))
    results: list[Compared] = []
    with open_storage(read_storage_config()) as source:
        for day in days:
            before = _local(backup, day)
            after = _local(after_root, day) if after_root else _stored(source, day)
            if before is None or after is None:
                found = f"前 {before is not None} / 後 {after is not None}"
                print(f"{day}: 前か後のファイルが無い（{found}）", file=sys.stderr)
                return 1
            reference = None if uniform is None else _local(uniform, day)
            results.append(compare_day(day, before, after, reference))
    print(render(results))
    return 0 if all(one.passed for one in results) else 1


if __name__ == "__main__":
    raise SystemExit(run())
