"""天気の予報を `weather_hourly` に取り込む（W4 プラン §6.4）。

**ここは副作用だけを持つ。** バイト列の読み方は `jobs/weather_archive.py`、引き方は
`features/weather.py` にある（CLAUDE.md §3）。

毎時 :25 UTC に走る（取得は :17）。**何を取り込むかは DB が決める**：`v_weather_pending`
が「`ok` で保存されたのに、まだ `weather_hourly` に入り切っていない発行」を返すので、
古い順に処理する。途中で落ちた発行も次の実行が拾い、**同じ発行を入れ直しても結果は
変わらない**（主キーで UPSERT する）。

使い方（環境変数は `.env` から読み込んでから）:

    set -a; . ./.env; set +a
    cd apps/ml
    ./.venv/bin/python -m bikechance_ml.jobs.load_weather --since 2026-09-07 --max-issues 80

`--since` は `available_at` の下限。**既定は 48 時間前**で、これは保守の保持期間
（30 日）よりずっと短い。消えた発行が「未処理」として蘇るのを避けるための下限であって、
取りこぼしを拾う窓ではない（取りこぼしは次の実行が拾う）。
"""

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol

from bikechance_ml.config import read_storage_config
from bikechance_ml.io.supabase import open_storage
from bikechance_ml.jobs.weather_archive import (
    WEATHER_BUCKET,
    CellForecast,
    PendingIssue,
    batch_count,
    read_batch,
    to_rows,
    weather_object_path,
)

#: `job_runs` と `monitored_jobs` に載る名前。**毎時 1 回**。
JOB_NAME: Final[str] = "load_weather"

#: `v_weather_pending` に渡す下限の既定（時間）。**保持期間（30 日）より短くする。**
#: 消えた発行が未処理として蘇り、取り込みと削除を繰り返すのを避けるため（0037）。
LOOKBACK_HOURS: Final[int] = 48

#: 1 回の実行で取り込む発行の上限。1 発行 = 6 ファイル・595 行。
#: **毎時 1 つ増える**ので、6 あれば 5 時間ぶんの遅れを 1 回で取り戻せる。
MAX_ISSUES: Final[int] = 6


class WeatherPort(Protocol):
    """入出力の差し替え点。**このジョブが要る口だけ**を並べる。"""

    def list_weather_pending(self, since: datetime, limit: int) -> tuple[PendingIssue, ...]: ...
    def download(self, bucket: str, path: str) -> bytes | None: ...
    def upsert_weather_hourly(self, rows: Sequence[Mapping[str, object]]) -> int: ...
    def job_started(self, job_name: str) -> int: ...
    def job_finished(self, run_id: int, status: str, detail: Mapping[str, object]) -> None: ...


class MissingBatchError(RuntimeError):
    """保存されているはずのファイルが無い。**部分的に取り込まない。**

    `v_weather_files` が `ok` と言っている以上、6 分割すべてが Storage にある。
    無いなら食い違いなので、その発行は次の実行に回す（未処理のまま残る）。
    """


@dataclass(frozen=True)
class IssueOutcome:
    """1 発行ぶんの結果。**失敗しても次の発行は続ける。**"""

    issued_hour: str
    ok: bool
    n_cells: int
    n_written: int
    error: str | None


@dataclass(frozen=True)
class LoadSummary:
    """1 回の実行の要約。`job_runs.detail` と応答の両方に使う。"""

    ok: bool
    n_pending: int
    n_loaded: int
    n_failed: int
    n_rows: int
    duration_ms: int
    issues: tuple[IssueOutcome, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "pending": self.n_pending,
            "loaded": self.n_loaded,
            "failed": self.n_failed,
            "rows": self.n_rows,
            "duration_ms": self.duration_ms,
            # **失敗した発行だけを並べる。** 成功は数で足りる
            "failures": [
                {"issued_hour": one.issued_hour, "error": one.error}
                for one in self.issues
                if not one.ok
            ],
        }


def read_issue(source: WeatherPort, issue: PendingIssue) -> tuple[CellForecast, ...]:
    """1 発行ぶんの 6 ファイルを読んで 1 つに並べる。**1 つでも欠ければ止める。**"""
    forecasts: list[CellForecast] = []
    for batch in range(batch_count(issue.n_cells)):
        path = weather_object_path(issue.hour_epoch_s, batch)
        body = source.download(WEATHER_BUCKET, path)
        if body is None:
            raise MissingBatchError(f"分割が Storage に無い: {path}")
        forecasts.extend(read_batch(body, issue.issued_hour))
    return tuple(forecasts)


def load_issue(source: WeatherPort, issue: PendingIssue) -> IssueOutcome:
    """1 発行を取り込む。**例外は種類だけを詰め替える**（文言に接続先が混じらない）。"""
    label = issue.issued_hour.isoformat()
    try:
        forecasts = read_issue(source, issue)
        written = source.upsert_weather_hourly(
            to_rows(forecasts, issue.issued_hour, issue.available_at)
        )
    except Exception as cause:
        # **種類だけを残す**（文言に接続先が混じる経路を作らない。`infer` と同じ）。
        # 期待している失敗は `ArchiveShapeError` と `MissingBatchError` の 2 つだが、
        # 取りこぼしても次の実行が拾えるので、ここでは区別せずに次の発行へ進む
        return IssueOutcome(label, False, issue.n_cells, 0, type(cause).__name__)
    return IssueOutcome(label, True, issue.n_cells, written, None)


def load_pending(
    source: WeatherPort, since: datetime, max_issues: int, started: datetime
) -> LoadSummary:
    """未処理の発行を古い順に取り込む。**1 つ落ちても残りは続ける。**"""
    pending = source.list_weather_pending(since, max_issues)
    outcomes = tuple(load_issue(source, issue) for issue in pending)
    failed = sum(1 for one in outcomes if not one.ok)
    return LoadSummary(
        ok=failed == 0,
        n_pending=len(pending),
        n_loaded=len(outcomes) - failed,
        n_failed=failed,
        n_rows=sum(one.n_written for one in outcomes),
        duration_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
        issues=outcomes,
    )


# ── 実行 ──────────────────────────────────────────────────────
def _log(message: str) -> None:
    print(message, file=sys.stderr)


def _started_quietly(source: WeatherPort) -> int | None:
    """記録を始められなくても取り込む（`build_reference` と同じ）。"""
    try:
        return source.job_started(JOB_NAME)
    except Exception as cause:
        _log(f"job_started に失敗した: {type(cause).__name__}")
        return None


def _record_quietly(
    source: WeatherPort, run_id: int | None, status: str, detail: Mapping[str, object]
) -> None:
    try:
        if run_id is not None:
            source.job_finished(run_id, status, detail)
    except Exception as cause:
        _log(f"job_finished に失敗した: {type(cause).__name__}")


def run_load(
    source: WeatherPort, now: datetime, since: datetime | None = None, max_issues: int = MAX_ISSUES
) -> LoadSummary:
    """取り込んで `job_runs` に記録する。

    **記録は半分の目的**である。書かないと `check_jobs_missing` から見えず、
    止まっても誰も気づかない（W4 プラン §12 の 116）。
    """
    run_id = _started_quietly(source)
    lower = since if since is not None else now - timedelta(hours=LOOKBACK_HOURS)
    try:
        summary = load_pending(source, lower, max_issues, now)
    except Exception as cause:
        _record_quietly(source, run_id, "failed", {"error": type(cause).__name__})
        raise
    _record_quietly(source, run_id, "ok" if summary.ok else "failed", summary.as_dict())
    return summary


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="天気の予報を weather_hourly に取り込む")
    parser.add_argument("--since", default=None, help="available_at の下限（YYYY-MM-DD）")
    parser.add_argument("--max-issues", type=int, default=MAX_ISSUES, help="1 回に取り込む発行数")
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    options = _arguments(argv)
    since = datetime.fromisoformat(options.since).replace(tzinfo=UTC) if options.since else None
    with open_storage(read_storage_config()) as source:
        summary = run_load(source, datetime.now(UTC), since, options.max_issues)
    print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2))
    return 0 if summary.ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
