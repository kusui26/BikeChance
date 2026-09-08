"""学習サンプルを 1 日ぶん作る（W3 プラン §5.8）。

**ここが副作用の置き場所。** 特徴量の規則は `features/` にあり、このファイルは
「Storage から Parquet を取る」「PostgREST から参照データを読む」「書き出す」だけを
持つ（CLAUDE.md §3）。

使い方（環境変数は `.env` から読み込んでから）:

    set -a; . ./.env; set +a
    cd apps/ml
    ./.venv/bin/python -m bikechance_ml.jobs.build_features \\
        --date 2026-09-07 --cache .cache/parquet --out /tmp/features.parquet

`--date` は **JST の暦日**。入力の Parquet は UTC の時間帯なので、1 日ぶんを作るには
前後にまたがる時間帯を読む（`features/grid.py`）。`--upload` を付けると
`features/date=YYYY-MM-DD/part.parquet` に置く。

**要求した時間帯が Storage に無ければ、その区間は欠損として扱われる**（補間しない）。
欠損の件数は `excluded` に出るので、まずそこを見る。
"""

import argparse
import json
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq

from bikechance_ml.config import read_storage_config
from bikechance_ml.features import build, neighbors, static
from bikechance_ml.features.constants import LOOKAHEAD_HOURS, LOOKBACK_HOURS
from bikechance_ml.features.grid import features_path, parquet_hours
from bikechance_ml.features.reference import SystemReference
from bikechance_ml.io.supabase import PARQUET_BUCKET, SupabaseIo, open_storage
from bikechance_ml.jobs.snapshot_table import COMPRESSION, has_current_schema, parquet_path
from bikechance_ml.jobs.snapshot_table import SCHEMA as SNAPSHOT_SCHEMA
from bikechance_ml.jobs.snapshot_table import read_table as read_snapshot_table

#: 対象のシステム。**並び順が台帳の位置を決める**ので、固定する（`analysis/eda_01.py` と同じ）。
SYSTEM_IDS: Final[tuple[str, ...]] = ("hellocycling", "docomo-cycle")


def read_reference(source: SupabaseIo, system_id: str) -> SystemReference:
    """1 システムぶんの参照データを読む。"""
    return SystemReference(
        system_id=system_id,
        geo=source.list_station_geo(system_id),
        attributes=source.list_station_attributes(system_id),
        neighbors=source.list_neighbors(system_id),
    )


def load_snapshots(
    source: SupabaseIo, day: date, cache: Path | None
) -> tuple[pa.Table, tuple[str, ...]]:
    """全システムの Parquet を読んで 1 つの表にする。**無い時間帯は記録して進む。**"""
    tables: list[pa.Table] = []
    missing: list[str] = []
    for system_id in SYSTEM_IDS:
        for hour in parquet_hours(day, LOOKBACK_HOURS, LOOKAHEAD_HOURS):
            body = _one_hour(source, system_id, hour, cache)
            if body is None:
                missing.append(f"{system_id} {hour:%Y-%m-%dT%H}Z")
                continue
            tables.append(read_snapshot_table(body, f"{system_id} {hour:%Y-%m-%dT%H}Z"))
    table = pa.concat_tables(tables) if tables else SNAPSHOT_SCHEMA.empty_table()
    return table, tuple(missing)


def _one_hour(
    source: SupabaseIo, system_id: str, hour: datetime, cache: Path | None
) -> bytes | None:
    """キャッシュがあれば使う。**何度走らせても同じ入力になる**ようにするため。

    ただし**古い形のファイルは捨てて取り直す**。畳み直し（W2 の PR B）で同じパスの
    中身が変わるので、パスで引くだけでは前の形が残る（W3 プラン §12 の 96）。
    """
    path = None if cache is None else cache / parquet_path(system_id, hour)
    if path is not None and path.exists():
        cached = path.read_bytes()
        if has_current_schema(cached):
            return cached
        path.unlink()
    body = source.download(PARQUET_BUCKET, parquet_path(system_id, hour))
    if body is not None and path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return body


def to_inputs(
    day: date,
    systems: Sequence[SystemReference],
    holidays: frozenset[date],
    table: pa.Table,
) -> build.DayInputs:
    """参照データと Parquet を、組み立ての入力に直す。"""
    facts = static.to_facts(systems)
    links = neighbors.to_links(systems, facts.station_keys())
    return build.DayInputs(
        day=day,
        reference=build.Reference(facts=facts, links=links, holidays=holidays),
        table=table,
    )


def to_parquet_bytes(table: pa.Table) -> bytes:
    """表を Parquet のバイト列にする。**同じ表からは同じバイト列が出る。**"""
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression=COMPRESSION)
    return bytes(sink.getvalue())


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="学習サンプルを 1 日ぶん作る")
    parser.add_argument("--date", required=True, help="JST の暦日（YYYY-MM-DD）")
    parser.add_argument("--cache", default=None, help="Parquet を置く場所（再実行が速くなる）")
    parser.add_argument("--out", default=None, help="書き出し先のファイル")
    parser.add_argument("--upload", action="store_true", help="Storage に置く")
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    options = _arguments(argv)
    day = date.fromisoformat(options.date)
    cache = Path(options.cache) if options.cache else None

    with open_storage(read_storage_config()) as source:
        systems = tuple(read_reference(source, system_id) for system_id in SYSTEM_IDS)
        holidays = frozenset(source.list_holidays())
        table, missing = load_snapshots(source, day, cache)
        built = build.build_day(to_inputs(day, systems, holidays, table))
        body = to_parquet_bytes(built.table)
        if options.upload:
            source.upload_parquet(features_path(day), body)

    if options.out:
        Path(options.out).write_bytes(body)
    summary = {**built.stats.as_dict(), "bytes": len(body), "missing_hours": list(missing)}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
