"""毎時の Parquet 化（W2 プラン §5.6、PR D）。

**目的は Postgres の 60 日保持から独立した学習用アーカイブを作り始めること。**
生 gzip JSON が一次ソースであることは変わらない（開発プラン D-03）。Parquet は
そこからも Postgres からも作り直せる派生物で、学習の読み出しを速くするために置く。

手順（CLAUDE.md §3 の Cron ハンドラの定型）：
  `CRON_SECRET` 検証 → 冪等チェック → 処理 → `job_runs` に記録 → 要約 JSON

**アドバイザリロックは取らない。** PostgREST は 1 要求 1 トランザクションなので、
`pg_try_advisory_xact_lock` を RPC 越しに取っても戻った時点で解放される。代わりに
**構造で冪等にする**：同じ時間帯は同じパスに写像し、内容ごと上書きする。二重起動は
同じバイト列を 2 回書くだけで、結果は変わらない。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol

from bikechance_ml.jobs import recording
from bikechance_ml.jobs.snapshot_table import (
    Snapshot,
    StationRow,
    count_missing,
    hour_window,
    parquet_path,
    station_ids_by_idx,
    to_parquet_bytes,
    to_table,
)

JOB_NAME: Final[str] = "compact_parquet"

MS_PER_S: Final[int] = 1000


class CompactPort(Protocol):
    """このジョブが使う入出力。`io/supabase.py` の実装が構造的に満たす。

    絞っておくと、上位のロジックが httpx を知らずに済み、テストで分岐を全部通せる。
    """

    def list_active_systems(self) -> tuple[str, ...]: ...

    def list_stations(self, system_id: str) -> tuple[StationRow, ...]: ...

    def list_snapshots(
        self, system_id: str, start: datetime, end: datetime
    ) -> tuple[Snapshot, ...]: ...

    def upload_parquet(self, path: str, body: bytes) -> None: ...

    def job_started(self, job_name: str) -> int: ...

    def job_finished(self, run_id: int, status: str, detail: Mapping[str, object]) -> None: ...


@dataclass(frozen=True)
class SystemOutcome:
    """1 システム 1 時間ぶんの結果。1 つ落ちても他は続ける。"""

    system_id: str
    ok: bool
    n_snapshots: int
    n_stations: int
    n_rows: int
    n_missing: int
    bytes: int
    path: str | None
    error: str | None


@dataclass(frozen=True)
class CompactSummary:
    ok: bool
    hour_start: datetime
    n_systems: int
    n_failed: int
    n_empty: int
    n_rows: int
    bytes: int
    duration_ms: int
    systems: tuple[SystemOutcome, ...]
    error: str | None


def _failed(system_id: str, error: str) -> SystemOutcome:
    return SystemOutcome(system_id, False, 0, 0, 0, 0, 0, None, error)


def resolve_window(now: datetime, hour: datetime | None) -> tuple[datetime, datetime]:
    """畳む時間帯を決める。指定が無ければ直前の 1 時間。

    明示指定は再実行と取りこぼしの埋め戻しに使う。**必ず正時**でなければならない。
    ずれた時刻を許すと、区間が重なった Parquet が同じパスに書かれる。
    """
    if hour is None:
        return hour_window(now)
    at = hour.astimezone(UTC)
    if (at.minute, at.second, at.microsecond) != (0, 0, 0):
        raise ValueError("時間帯は正時で指定してください")
    if at >= hour_window(now)[1]:
        raise ValueError("まだ終わっていない時間帯は畳めません")
    return at, at + timedelta(hours=1)


def compact_system(
    port: CompactPort, system_id: str, start: datetime, end: datetime
) -> SystemOutcome:
    """1 システムを畳む。**行が 0 でもファイルを作らない。**

    観測が無い時間帯に空のファイルを置くと「収集していない」と「畳んでいない」を
    見分けられなくなる。収集の欠落は `monitor_feeds` が別途通知する（W1 プラン §6.7）。
    """
    # 観測を先に見る。空なら台帳（14,900 行）を読む必要がない
    snapshots = port.list_snapshots(system_id, start, end)
    if not snapshots:
        return SystemOutcome(system_id, True, 0, 0, 0, 0, 0, None, None)

    station_ids = station_ids_by_idx(port.list_stations(system_id))
    table = to_table(system_id, station_ids, snapshots)
    body = to_parquet_bytes(table)
    path = parquet_path(system_id, start)
    port.upload_parquet(path, body)
    return SystemOutcome(
        system_id=system_id,
        ok=True,
        n_snapshots=len(snapshots),
        n_stations=len(station_ids),
        n_rows=table.num_rows,
        n_missing=count_missing(table),
        bytes=len(body),
        path=path,
        error=None,
    )


def _run_all(port: CompactPort, start: datetime, end: datetime) -> tuple[SystemOutcome, ...]:
    outcomes: list[SystemOutcome] = []
    for system_id in port.list_active_systems():
        # 1 システムの失敗で他を巻き込まない。片方だけでも畳めたほうがよい
        try:
            outcomes.append(compact_system(port, system_id, start, end))
        except Exception as cause:
            outcomes.append(_failed(system_id, f"{type(cause).__name__}: {cause}"))
    return tuple(outcomes)


def _summarize(
    start: datetime,
    outcomes: Sequence[SystemOutcome],
    duration_ms: int,
    error: str | None,
) -> CompactSummary:
    n_failed = sum(1 for one in outcomes if not one.ok)
    return CompactSummary(
        ok=error is None and n_failed == 0 and len(outcomes) > 0,
        hour_start=start,
        n_systems=len(outcomes),
        n_failed=n_failed,
        n_empty=sum(1 for one in outcomes if one.ok and one.n_snapshots == 0),
        n_rows=sum(one.n_rows for one in outcomes),
        bytes=sum(one.bytes for one in outcomes),
        duration_ms=duration_ms,
        systems=tuple(outcomes),
        error=error,
    )


def to_detail(summary: CompactSummary) -> dict[str, object]:
    """`job_runs.detail` と HTTP 応答に共通で使う要約。JSON にできる型だけを入れる。"""
    return {
        "ok": summary.ok,
        "hour_start": f"{summary.hour_start:%Y-%m-%dT%H:%M:%S}Z",
        "n_systems": summary.n_systems,
        "n_failed": summary.n_failed,
        "n_empty": summary.n_empty,
        "n_rows": summary.n_rows,
        "bytes": summary.bytes,
        "duration_ms": summary.duration_ms,
        "systems": [
            {
                "system_id": one.system_id,
                "ok": one.ok,
                "n_snapshots": one.n_snapshots,
                "n_stations": one.n_stations,
                "n_rows": one.n_rows,
                "n_missing": one.n_missing,
                "bytes": one.bytes,
                "path": one.path,
                "error": one.error,
            }
            for one in summary.systems
        ],
        **({} if summary.error is None else {"error": summary.error}),
    }


def compact_hour(port: CompactPort, now: datetime, hour: datetime | None = None) -> CompactSummary:
    """指定（既定は直前）の 1 時間を、システムごとに 1 ファイルへ畳む。"""
    started = datetime.now(UTC)
    start, end = resolve_window(now, hour)

    # 記録を始められなくても処理は行う。記録の不調で 1 時間ぶんを落とさない
    run_id = recording.started_quietly(port, JOB_NAME)

    # システム一覧の取得など、全体が落ちた場合。要約に理由を残して 500 を返す
    try:
        outcomes = _run_all(port, start, end)
        error: str | None = None
    except Exception as cause:
        outcomes = ()
        error = f"{type(cause).__name__}: {cause}"

    elapsed_ms = int((datetime.now(UTC) - started).total_seconds() * MS_PER_S)
    summary = _summarize(start, outcomes, elapsed_ms, error)
    recording.record_quietly(port, run_id, "ok" if summary.ok else "failed", to_detail(summary))
    return summary
