"""ふだんの傾向を `station_trend` に写す（W9 の PR B、開発プランの D-47）。

**ここは副作用だけを持つ。** 作り方は `features/trend.py`（純粋）にある（CLAUDE.md §3）。

`build-features.yml` の 00:40 JST の回（来なかった・落ちた日は 06:00 の回）で、プロファイルの
段の後に走る。写す日 P（既定は JST の昨日）の日次まで 43 日ぶん（`TrendWindows.daily_days`、
約 100 MB）を読み、系統 × ターゲットごとに校正を当てはめ、門を測り、P の版に当てて写す。

使い方（環境変数は `.env` から読み込んでから）:

    set -a; . ./.env; set +a
    cd apps/ml
    ./.venv/bin/python -m bikechance_ml.jobs.build_trend --date 2026-10-31          # 写さない
    ./.venv/bin/python -m bikechance_ml.jobs.build_trend --date 2026-10-31 --write  # 写す

**写さない回は何も書かない**（`job_runs` にも残さない。要約を出すだけ）。

**同じ日を何度写しても結果は同じ**（入力は Storage の日次だけで、窓は P で決まる）。古い日を
写しても、DB の側が新しい行を残す（0059 の `upsert_station_trend`）。
"""

import argparse
import io
import json
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final, Protocol

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from bikechance_ml.config import read_storage_config
from bikechance_ml.features import profile, trend
from bikechance_ml.features.arrays import Bools
from bikechance_ml.features.grid import jst_yesterday, profile_path
from bikechance_ml.io.supabase import PARQUET_BUCKET, open_storage
from bikechance_ml.jobs import recording

#: `job_runs` と `monitored_jobs` に載る名前（0059）。**毎日 1 回**。
JOB_NAME: Final[str] = "build_trend"

#: 1 回の往復で送る行数。1 行は 72 個 × 3 本で約 0.9 KB、1,000 行で 1 MB 弱
#: （`infer` の 4,000 行 × 10 個 × 2 本と同じくらいの大きさ）。
UPSERT_BATCH: Final[int] = 1_000

#: 日次から読む列（`profile.DAILY_SCHEMA` のうち要るものだけ）
_COLUMNS: Final[tuple[str, ...]] = (
    "system_id",
    "station_id",
    "dow_type",
    "slot15",
    "n",
    "n_bike_ok",
    "n_dock_ok",
)


class TrendPort(Protocol):
    """このジョブが触る口だけ。`SupabaseIo` が構造的に満たす。"""

    def download(self, bucket: str, path: str) -> bytes | None: ...
    def upsert_station_trend(self, rows: Sequence[Mapping[str, object]]) -> int: ...
    def latest_ok_run(self, job_name: str, date_key: str, day: date) -> datetime | None: ...
    def job_started(self, job_name: str) -> int: ...
    def job_finished(self, run_id: int, status: str, detail: Mapping[str, object]) -> None: ...


class MissingDailyError(RuntimeError):
    """写す日 P の日次が無い。**P の版を名乗る表を作らない。**"""


@dataclass(frozen=True)
class Built:
    """作った行と要約。**まだ書いていない。**"""

    rows: list[dict[str, object]]
    summary: dict[str, object]


# ── 読む ──────────────────────────────────────────────────────
def read_dailies(source: TrendPort, days: Sequence[date]) -> dict[date, bytes]:
    """日次を圧縮のまま取る。**無い日は飛ばす**（補間しない。足りないことは `n_days` に出る）。"""
    bodies: dict[date, bytes] = {}
    for day in days:
        body = source.download(PARQUET_BUCKET, profile_path(day, profile.DAILY_NAME))
        if body is not None:
            bodies[day] = body
    return bodies


def _table(body: bytes, columns: Sequence[str]) -> pa.Table:
    return pq.read_table(io.BytesIO(body), columns=list(columns))


def _row_count(body: bytes) -> int:
    """行の数（Parquet の目次だけを読む）。"""
    return int(pq.ParquetFile(io.BytesIO(body)).metadata.num_rows)


def load_counts(
    bodies: Mapping[date, bytes],
) -> tuple[trend.Vocabulary, dict[date, trend.DayCounts]]:
    """台帳を先に作り（2 列だけ開く）、**1 日ずつ開いては密な配列にして捨てる。**

    **行の無い日次（1 日まるごと観測の無い日）は、無い日と同じに飛ばす**（補間しない）。
    """
    filled = {day: body for day, body in bodies.items() if _row_count(body) > 0}
    ports = trend.vocabulary(_table(body, ("system_id", "station_id")) for body in filled.values())
    counts = {
        day: trend.day_counts(day, _table(body, _COLUMNS), ports) for day, body in filled.items()
    }
    return ports, counts


# ── 組み立てる ──────────────────────────────────────────────────
def _rows_for(
    pairs: Sequence[tuple[date, date]],
    outcomes: Mapping[date, trend.Outcome],
    hourly: Mapping[date, trend.HourlyProfile],
    target: str,
    inside: Bools,
) -> trend.CalibrationRows | None:
    """組（到着の日 → 版）の行をつなぐ。**日次の無い日は飛ばし、1 行も無ければ None。**"""
    parts = [
        trend.pair_rows(outcomes[arrival], hourly[version], target, inside)
        for arrival, version in pairs
        if arrival in outcomes and version in hourly
    ]
    if not parts:
        return None
    rows = trend.concat_rows(parts)
    return rows if len(rows) > 0 else None


def judge_groups(
    ports: trend.Vocabulary,
    counts: Mapping[date, trend.DayCounts],
    windows: trend.TrendWindows,
) -> dict[tuple[str, str], trend.Judged]:
    """系統 × ターゲットごとに当てはめて門を測る。**行の足りない組は入れない**（出さない）。"""
    hourly = {v: trend.hourly_profile(counts, v, len(ports)) for v in windows.versions()}
    outcomes = {day: trend.outcome(one) for day, one in counts.items()}
    systems = np.asarray([system_id for system_id, _ in ports.ports])
    judged: dict[tuple[str, str], trend.Judged] = {}
    for system_id in sorted(set(systems.tolist())):
        inside = np.asarray(systems == system_id, dtype=np.bool_)
        for target in trend.TARGETS:
            sets = [
                _rows_for(pairs, outcomes, hourly, target, inside)
                for pairs in (windows.serve(), windows.gate_fit(), windows.gate_test())
            ]
            serve, gate_fit, gate_test = sets
            if serve is not None and gate_fit is not None and gate_test is not None:
                judged[(system_id, target)] = trend.judge(serve, gate_fit, gate_test)
    return judged


def build(source: TrendPort, day: date) -> Built:
    """読んで、当てはめて、行を作る。**書かない**（副作用は呼ぶ側が決める）。"""
    started = time.monotonic()
    windows = trend.TrendWindows(day)
    wanted = windows.daily_days()
    bodies = read_dailies(source, wanted)
    ports, counts = load_counts(bodies)
    if day not in counts:
        raise MissingDailyError(f"{day} の日次が無いか空（プロファイルの段が通っていない）")
    judged = judge_groups(ports, counts, windows)
    rows = trend.trend_rows(trend.hourly_profile(counts, day, len(ports)), ports.ports, judged)
    summary = to_summary(day, wanted, bodies, counts, judged, rows, time.monotonic() - started)
    return Built(rows=rows, summary=summary)


def to_summary(
    day: date,
    wanted: Sequence[date],
    bodies: Mapping[date, bytes],
    counts: Mapping[date, trend.DayCounts],
    judged: Mapping[tuple[str, str], trend.Judged],
    rows: Sequence[Mapping[str, object]],
    seconds: float,
) -> dict[str, object]:
    """応答と `job_runs` の detail。**出さなかった組と欠けた日を数で出す。**"""
    names = {key: f"{key[0]}/{key[1]}" for key in sorted(judged)}
    return {
        "date": day.isoformat(),
        "rows": len(rows),
        "dailies_read": len(bodies),
        "dailies_missing": [one.isoformat() for one in wanted if one not in bodies],
        # 置いてはあるが行の無い日（1 日まるごと観測の無い日）。無い日と同じに飛ばした
        "dailies_empty": [one.isoformat() for one in bodies if one not in counts],
        "dailies_bytes": sum(len(one) for one in bodies.values()),
        "served": {name: judged[key].passes for key, name in names.items()},
        "groups": {name: judged[key].describe() for key, name in names.items()},
        "seconds": round(seconds, 1),
    }


# ── 書く ──────────────────────────────────────────────────────
def batches(
    rows: Sequence[Mapping[str, object]], size: int
) -> Iterator[Sequence[Mapping[str, object]]]:
    """まとめて送る単位に刻む。"""
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def build_and_write(source: TrendPort, day: date) -> dict[str, object]:
    """作って写し、`job_runs` に記録する。**失敗も記録してから投げ直す**（例外の種類だけ）。"""
    run_id = recording.started_quietly(source, JOB_NAME)
    try:
        built = build(source, day)
        written = sum(source.upsert_station_trend(one) for one in batches(built.rows, UPSERT_BATCH))
    except Exception as cause:
        recording.record_quietly(
            source, run_id, "failed", {"date": day.isoformat(), "error": type(cause).__name__}
        )
        raise
    # **写した行の数**。古い版で弾いた行は数えない（0059）ので、`rows` と違えば読める
    summary = {"ok": True, **built.summary, "written": written}
    recording.record_quietly(source, run_id, "ok", summary)
    return summary


# ── 実行 ──────────────────────────────────────────────────────
def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ふだんの傾向を station_trend に写す")
    parser.add_argument("--date", default=None, help="写すプロファイルの JST の暦日（既定は昨日）")
    parser.add_argument("--write", action="store_true", help="DB に写す（無ければ要約だけ）")
    parser.add_argument(
        "--skip-if-current",
        action="store_true",
        help="その日の ok が job_runs に在れば何もしない（定時の 2 回目のため）",
    )
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    options = _arguments(argv)
    day = date.fromisoformat(options.date) if options.date else jst_yesterday(datetime.now(UTC))
    with open_storage(read_storage_config()) as source:
        if options.skip_if_current and source.latest_ok_run(JOB_NAME, "date", day) is not None:
            print(json.dumps({"skipped": True, "date": day.isoformat()}, ensure_ascii=False))
            return 0
        if options.write:
            summary = build_and_write(source, day)
        else:
            summary = {"ok": True, **build(source, day).summary, "written": 0}
    print(json.dumps({**summary, "wrote": options.write}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
