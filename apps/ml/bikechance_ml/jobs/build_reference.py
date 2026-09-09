"""日次の参照スナップショットを書く（W3 プラン §14.3）。

**ここは副作用だけを持つ。** 表の作り方は `features/reference_snapshot.py` にある
（CLAUDE.md §3）。

`rebuild_geo`（pg_cron 04:30 JST）の後に走らせ、**前日ぶん**を
`reference/date=YYYY-MM-DD/` に置く。読む規則は学習も推論も「基準時刻の**前日**の
版」なので、今日の推論と明日の学習が同じ版を見る。

使い方（環境変数は `.env` から読み込んでから）:

    set -a; . ./.env; set +a
    cd apps/ml
    ./.venv/bin/python -m bikechance_ml.jobs.build_reference --date 2026-09-08 --upload

`--date` は **JST の暦日**。省略すると「昨日（JST）」になる。

**`capacity_est` は当日と前 6 版の `capacity_daily_max` の最大**である。7 日ぶんの
Parquet を毎回読み直さないための持ち回りで、読むのは当日の 48 ファイルと前 6 版の
`stations.parquet` だけで済む。前の版が無い日は数えず、`capacity_days` に出る。
"""

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Final, Protocol

import pyarrow as pa
import pyarrow.parquet as pq

from bikechance_ml.config import read_storage_config
from bikechance_ml.features.grid import JST, day_hours, reference_path
from bikechance_ml.features.reference import (
    NeighborRow,
    StationAttributeRow,
    StationGeoRow,
    SystemReference,
)
from bikechance_ml.features.reference_snapshot import (
    CAPACITY_DAYS,
    NEIGHBORS_NAME,
    STATIONS_NAME,
    STATIONS_SCHEMA,
    daily_capacity_max,
    to_daily_max,
    to_neighbors_table,
    to_stations_table,
)
from bikechance_ml.io.supabase import PARQUET_BUCKET, open_storage
from bikechance_ml.jobs.build_features import SYSTEM_IDS, to_parquet_bytes
from bikechance_ml.jobs.snapshot_table import SCHEMA as SNAPSHOT_SCHEMA
from bikechance_ml.jobs.snapshot_table import has_current_schema, parquet_path
from bikechance_ml.jobs.snapshot_table import read_table as read_snapshot_table

#: `job_runs` と `monitored_jobs` に載る名前。**毎日 1 回**（05:00 JST）。
JOB_NAME: Final[str] = "build_reference"


class ReferencePort(Protocol):
    """入出力の差し替え点。**このジョブが要る口だけ**を並べる（`compact` と同じ形）。"""

    def list_station_geo(self, system_id: str) -> tuple[StationGeoRow, ...]: ...
    def list_station_attributes(self, system_id: str) -> tuple[StationAttributeRow, ...]: ...
    def list_neighbors(self, system_id: str) -> tuple[NeighborRow, ...]: ...
    def download(self, bucket: str, path: str) -> bytes | None: ...
    def upload_parquet(self, path: str, body: bytes) -> None: ...
    def job_started(self, job_name: str) -> int: ...
    def job_finished(self, run_id: int, status: str, detail: Mapping[str, object]) -> None: ...


def read_reference(source: ReferencePort, system_id: str) -> SystemReference:
    """1 システムぶんの参照データを DB から読む。"""
    return SystemReference(
        system_id=system_id,
        geo=source.list_station_geo(system_id),
        attributes=source.list_station_attributes(system_id),
        neighbors=source.list_neighbors(system_id),
    )


def build_tables(
    source: ReferencePort, day: date, *, built_at: datetime
) -> tuple[pa.Table, pa.Table, tuple[str, ...]]:
    """その日ぶんの 2 つの表を作る。**欠けた時間帯は記録して進む**（補間しない）。"""
    systems = tuple(read_reference(source, system_id) for system_id in SYSTEM_IDS)
    snapshots, missing = load_day(source, day)
    previous = load_previous(source, day, CAPACITY_DAYS - 1)
    stations = to_stations_table(
        systems, daily_capacity_max(snapshots), previous, built_at=built_at
    )
    neighbors = to_neighbors_table(systems, built_at=built_at)
    return stations, neighbors, missing


def load_day(source: ReferencePort, day: date) -> tuple[pa.Table, tuple[str, ...]]:
    """その JST 暦日ちょうどのスナップショットを読む（24 時間 × システム）。"""
    tables: list[pa.Table] = []
    missing: list[str] = []
    for system_id in SYSTEM_IDS:
        for hour in day_hours(day):
            body = source.download(PARQUET_BUCKET, parquet_path(system_id, hour))
            label = f"{system_id} {hour:%Y-%m-%dT%H}Z"
            if body is None:
                missing.append(label)
                continue
            if not has_current_schema(body):
                # **古い形は使わない。** 静かに null で埋めると容量が過小になる（§12 の 97）
                missing.append(f"{label}（古い形）")
                continue
            tables.append(read_snapshot_table(body, label))
    table = pa.concat_tables(tables) if tables else SNAPSHOT_SCHEMA.empty_table()
    return table, tuple(missing)


def load_previous(
    source: ReferencePort, day: date, count: int
) -> tuple[Mapping[tuple[str, str], int], ...]:
    """前の版の `capacity_daily_max` を新しい順に読む。**無い日は飛ばす。**"""
    found: list[Mapping[tuple[str, str], int]] = []
    for offset in range(1, count + 1):
        body = source.download(
            PARQUET_BUCKET, reference_path(day - timedelta(days=offset), STATIONS_NAME)
        )
        if body is None:
            continue
        found.append(to_daily_max(_read(body, STATIONS_SCHEMA)))
    return tuple(found)


def _read(body: bytes, schema: pa.Schema) -> pa.Table:
    """**ファイル自身の列**で読む（`schema=` を渡して足りない列を捏造しない。§12 の 97）。"""
    table = pq.read_table(pa.BufferReader(body))
    if table.schema.names != schema.names:
        raise ValueError(f"参照スナップショットの列が違う: {table.schema.names}")
    return table


def to_summary(
    stations: pa.Table, neighbors: pa.Table, missing: Sequence[str], sizes: Mapping[str, int]
) -> dict[str, object]:
    """応答と CLI の出力。**欠けているものを数で出す。**"""
    days = stations.column("capacity_days").to_pylist()
    estimates = stations.column("capacity_est").to_pylist()
    return {
        "stations": stations.num_rows,
        "neighbors": neighbors.num_rows,
        "with_capacity_est": sum(1 for one in estimates if one is not None),
        "capacity_days_max": max(days) if days else 0,
        "capacity_days_full": sum(1 for one in days if one >= CAPACITY_DAYS),
        "missing_hours": len(missing),
        "bytes": sizes,
    }


# ── 実行 ──────────────────────────────────────────────────────
def yesterday(now: datetime) -> date:
    """JST の昨日。**05:00 JST に走らせるので、前日ぶんが揃っている。**"""
    return (now.astimezone(JST) - timedelta(days=1)).date()


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="参照スナップショットを 1 日ぶん作る")
    parser.add_argument("--date", default=None, help="JST の暦日（既定は昨日）")
    parser.add_argument("--upload", action="store_true", help="Storage に置く")
    return parser.parse_args(argv)


def _log(message: str) -> None:
    print(message, file=sys.stderr)


def _started_quietly(source: ReferencePort) -> int | None:
    """記録を始められなくても作る。**記録の不調で 1 日ぶんを落とさない**（`compact` と同じ）。"""
    try:
        return source.job_started(JOB_NAME)
    except Exception as cause:
        _log(f"job_started に失敗した: {type(cause).__name__}")
        return None


def _record_quietly(
    source: ReferencePort, run_id: int | None, status: str, detail: Mapping[str, object]
) -> None:
    """記録の失敗でジョブを落とさない。置けたことのほうが大事。"""
    if run_id is None:
        return
    try:
        source.job_finished(run_id, status, detail)
    except Exception as cause:
        _log(f"job_finished に失敗した: {type(cause).__name__}")


def build_and_upload(source: ReferencePort, day: date, now: datetime) -> dict[str, object]:
    """作って置き、`job_runs` に記録する。

    **同じパスに上書きする**ので、何度走らせても結果は変わらない。

    **記録するのは「毎日回っていること」を見張れるようにするため**（W4 プラン §12 の 116）。
    `job_runs` に書かないと `check_jobs_missing` から見えず、止まっても誰も気づかない。
    学習も推論も「基準時刻の**前日**の版」を読む設計なので、**止まった翌日に静かに壊れる**。

    **失敗も記録してから投げ直す。** 記録が無いと「動いていない」ことしか分からず、
    「動いたが失敗した」と区別できない。詰めるのは**例外の種類だけ**にする（`infer` と
    同じ。文言に接続先が混じる経路を作らない）。
    """
    run_id = _started_quietly(source)
    try:
        stations, neighbors, missing = build_tables(source, day, built_at=now)
        bodies = {
            STATIONS_NAME: to_parquet_bytes(stations),
            NEIGHBORS_NAME: to_parquet_bytes(neighbors),
        }
        for name, body in bodies.items():
            source.upload_parquet(reference_path(day, name), body)
    except Exception as cause:
        _record_quietly(
            source, run_id, "failed", {"date": day.isoformat(), "error": type(cause).__name__}
        )
        raise
    summary = {
        "ok": True,
        "date": day.isoformat(),
        **to_summary(stations, neighbors, missing, {k: len(v) for k, v in bodies.items()}),
    }
    _record_quietly(source, run_id, "ok", summary)
    return summary


def run(argv: Sequence[str] | None = None) -> int:
    options = _arguments(argv)
    day = date.fromisoformat(options.date) if options.date else yesterday(datetime.now(UTC))
    with open_storage(read_storage_config()) as source:
        if options.upload:
            summary = build_and_upload(source, day, datetime.now(UTC))
        else:
            stations, neighbors, missing = build_tables(source, day, built_at=datetime.now(UTC))
            sizes = {
                STATIONS_NAME: len(to_parquet_bytes(stations)),
                NEIGHBORS_NAME: len(to_parquet_bytes(neighbors)),
            }
            summary = {
                "ok": True,
                "date": day.isoformat(),
                **to_summary(stations, neighbors, missing, sizes),
            }
    print(json.dumps({**summary, "uploaded": options.upload}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
