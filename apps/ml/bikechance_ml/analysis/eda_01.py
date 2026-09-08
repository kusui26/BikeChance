"""EDA #1 の実行（W2 プラン §5.9）。

**学習が実際に読む Parquet を入力にする。** Postgres ではなく Parquet を読むのは、
学習と同じ経路を通すことでアーカイブそのものも検証できるからで、実際この形にしたことで
畳み忘れ（欠落した時間帯）がそのまま出力に現れる。

使い方（環境変数は `.env` から読み込んでから）:

    set -a; . ./.env; set +a
    cd apps/ml
    ./.venv/bin/python -m bikechance_ml.analysis.eda_01 \\
        --from 2026-09-06T06:00:00Z --to 2026-09-08T01:00:00Z \\
        --cache .cache/parquet --out ../../docs/260908_eda_01.md

`--from` / `--to` は **UTC の正時**で、区間は半開 `[from, to)`。要求した時間帯のうち
Storage に無いものは「欠落」として報告する（`status_snapshots` に行が在るなら
`GET /ml/compact?hour=...` で埋め戻せる。データ辞書 §7.3）。
"""

import argparse
import io as _io
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Protocol

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from bikechance_ml.analysis import metrics
from bikechance_ml.analysis.report import render_markdown
from bikechance_ml.config import read_storage_config
from bikechance_ml.io.supabase import open_storage
from bikechance_ml.jobs.snapshot_table import SCHEMA, parquet_path

#: 分析の対象。`systems` から読まず定数にするのは、分析の再現性を優先するため。
SYSTEM_IDS: Final[tuple[str, ...]] = ("hellocycling", "docomo-cycle")

PARQUET_BUCKET: Final[str] = "gbfs-parquet"


class ParquetSource(Protocol):
    """Parquet を 1 つ取ってくる口。無ければ None を返す。"""

    def download(self, bucket: str, path: str) -> bytes | None: ...


@dataclass(frozen=True)
class Loaded:
    """1 システムぶんの読み込み結果。"""

    system_id: str
    table: pa.Table
    found_hours: tuple[datetime, ...]
    missing_hours: tuple[datetime, ...]


@dataclass(frozen=True)
class SystemReport:
    """1 システムぶんの集計。"""

    system_id: str
    n_hours_requested: int
    n_hours_found: int
    missing_hours: tuple[datetime, ...]
    first_observed: datetime | None
    last_observed: datetime | None
    availability: metrics.Availability
    missingness: metrics.Missingness
    hourly: tuple[metrics.HourlyRow, ...]
    spread: metrics.StationSpread
    flow: metrics.FlowLoss
    rebalancing: metrics.Rebalancing
    extremes: tuple[metrics.Extreme, ...]


def hours_between(start: datetime, end: datetime) -> tuple[datetime, ...]:
    """半開区間 `[start, end)` の正時を並べる。"""
    if start >= end:
        return ()
    return tuple(
        start + timedelta(hours=offset)
        for offset in range(int((end - start).total_seconds()) // 3600)
    )


def _cached(cache: Path | None, system_id: str, hour: datetime) -> Path | None:
    if cache is None:
        return None
    return cache / parquet_path(system_id, hour)


def _read_bytes(body: bytes) -> pa.Table:
    return pq.read_table(_io.BytesIO(body))


def load_hours(
    source: ParquetSource,
    system_id: str,
    hours: Sequence[datetime],
    cache: Path | None,
) -> Loaded:
    """時間帯ごとに Parquet を取り、1 つの表にまとめる。**無い時間帯は記録して進む。**"""
    tables: list[pa.Table] = []
    found: list[datetime] = []
    missing: list[datetime] = []
    for hour in hours:
        body = _load_one(source, system_id, hour, cache)
        if body is None:
            missing.append(hour)
            continue
        tables.append(_read_bytes(body))
        found.append(hour)
    table = pa.concat_tables(tables) if tables else SCHEMA.empty_table()
    return Loaded(system_id, table, tuple(found), tuple(missing))


def _load_one(
    source: ParquetSource, system_id: str, hour: datetime, cache: Path | None
) -> bytes | None:
    """キャッシュがあれば使う。**何度走らせても同じ入力になる**ようにするため。"""
    path = _cached(cache, system_id, hour)
    if path is not None and path.exists():
        return path.read_bytes()
    downloaded = source.download(PARQUET_BUCKET, parquet_path(system_id, hour))
    if downloaded is not None and path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(downloaded)
    return downloaded


def analyse(loaded: Loaded, n_requested: int) -> SystemReport:
    """1 システムぶんを集計する。**容量は観測から推定する**（宣言値は使わない）。"""
    table = loaded.table
    seen = metrics.observed(table)
    capacity = metrics.observed_capacity(table) if table.num_rows else {}
    stamps = seen.column("observed_at")
    return SystemReport(
        system_id=loaded.system_id,
        n_hours_requested=n_requested,
        n_hours_found=len(loaded.found_hours),
        missing_hours=loaded.missing_hours,
        first_observed=_min_stamp(stamps) if seen.num_rows else None,
        last_observed=_max_stamp(stamps) if seen.num_rows else None,
        availability=metrics.availability(table),
        missingness=metrics.missingness(table),
        hourly=metrics.hourly(table),
        spread=metrics.station_spread(table),
        flow=metrics.flow_loss(table),
        rebalancing=metrics.rebalancing(table, capacity=capacity),
        extremes=metrics.extremes(table),
    )


def _min_stamp(stamps: pa.ChunkedArray) -> datetime:
    value: datetime = pc.min(stamps).as_py()
    return value


def _max_stamp(stamps: pa.ChunkedArray) -> datetime:
    value: datetime = pc.max(stamps).as_py()
    return value


def parse_hour(text: str) -> datetime:
    """`2026-09-06T06:00:00Z` を読む。**正時・タイムゾーン必須。**"""
    at = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if at.tzinfo is None:
        raise ValueError(f"タイムゾーンを付けてください: {text}")
    at = at.astimezone(UTC)
    if (at.minute, at.second, at.microsecond) != (0, 0, 0):
        raise ValueError(f"正時で指定してください: {text}")
    return at


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="EDA #1：蓄積した Parquet の分布を見る")
    parser.add_argument("--from", dest="start", required=True, help="UTC の正時（含む）")
    parser.add_argument("--to", dest="end", required=True, help="UTC の正時（含まない）")
    parser.add_argument("--cache", default=None, help="Parquet を置く場所（再実行が速くなる）")
    parser.add_argument("--out", default=None, help="Markdown の出力先。省略すると標準出力")
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    options = _arguments(argv)
    hours = hours_between(parse_hour(options.start), parse_hour(options.end))
    if not hours:
        print("対象の時間帯がありません（--from < --to にしてください）", file=sys.stderr)
        return 1
    cache = Path(options.cache) if options.cache else None

    with open_storage(read_storage_config()) as storage:
        reports = tuple(
            analyse(load_hours(storage, system_id, hours, cache), len(hours))
            for system_id in SYSTEM_IDS
        )

    text = render_markdown(reports, hours[0], hours[-1] + timedelta(hours=1))
    if options.out is None:
        print(text)
    else:
        Path(options.out).write_text(text, encoding="utf-8")
        print(f"書き出しました: {options.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
