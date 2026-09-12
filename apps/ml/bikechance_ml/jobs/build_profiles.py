"""ポートプロファイルを 1 日ぶん作る（W5 プラン §6.2 の PR B）。

**ここが副作用の置き場所。** 数え方は `features/profile.py` にあり、このファイルは
「Storage から Parquet を取る」「書き出す」「`job_runs` に記録する」だけを持つ
（CLAUDE.md §3）。

**毎日 GitHub Actions が前日ぶんを作る**（`build_features` と同じワークフロー、
**その前**に走る）。順番に意味がある：PR J で `features/` が `prof_*` を読むように
なったとき、**同じ回の中で先にプロファイルが揃っている**必要がある。

**置くのは 2 つ。**

    profiles/date=YYYY-MM-DD/daily.parquet     ← その日ぶんの素の集計
    profiles/date=YYYY-MM-DD/profile.parquet   ← 直近 28 日の累計（読むのはこちら）

**28 日ぶんを毎日読み直さない**（1 回 150〜400 MB になる）。読むのは 3 つだけ——
前日の `profile`・当日の毎時 Parquet・**28 日前の `daily`**（W5-05）。`build_reference` の
`capacity_daily_max` と同じ持ち回りである。

使い方（環境変数は `.env` から読み込んでから）:

    set -a; . ./.env; set +a
    cd apps/ml
    ./.venv/bin/python -m bikechance_ml.jobs.build_profiles \\
        --date 2026-09-07 --cache .cache/parquet --out /tmp/profiles

`--date` は **JST の暦日**（省略すると昨日）。**さかのぼって作るときは古い日から順に**
——転がしが前日の版を読むためである。
"""

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final, Protocol

import pyarrow as pa
import pyarrow.parquet as pq

from bikechance_ml.config import read_storage_config
from bikechance_ml.features import profile
from bikechance_ml.features.grid import jst_yesterday, parquet_hours, profile_path
from bikechance_ml.io.supabase import PARQUET_BUCKET, open_storage
from bikechance_ml.jobs import recording
from bikechance_ml.jobs.build_features import ReadsStorage, load_snapshots, to_parquet_bytes

#: `job_runs` と `monitored_jobs` に載る名前。**毎日 1 回**（GitHub Actions）。
JOB_NAME: Final[str] = "build_profiles"


class ProfilesPort(ReadsStorage, Protocol):
    """このジョブが使う入出力ぜんぶ。`io/supabase.py` の実装が構造的に満たす。

    **参照スナップショットも天気も読まない。** 数えるのに要らないものを前提にしない
    （`features/profile.py` の `DayInputs`）。
    """

    def list_holidays(self) -> tuple[date, ...]: ...
    def upload_parquet(self, path: str, body: bytes) -> None: ...
    def job_started(self, job_name: str) -> int: ...
    def job_finished(self, run_id: int, status: str, detail: Mapping[str, object]) -> None: ...


@dataclass(frozen=True)
class Made:
    """作った 1 日ぶん。`daily` と `profile` の両方を置く。"""

    daily: pa.Table
    profile: pa.Table
    summary: dict[str, object]

    def bodies(self) -> dict[str, bytes]:
        return {
            profile.DAILY_NAME: to_parquet_bytes(self.daily),
            profile.PROFILE_NAME: to_parquet_bytes(self.profile),
        }


def read_day(
    source: ReadsStorage, day: date, cache: Path | None
) -> tuple[pa.Table, tuple[str, ...]]:
    """その日の観測を読む。**学習の 25 時間ではなく 2 時間の余白**（W5 プラン §6.2）。

    プロファイルはラベル（`t + h`）も同時刻履歴（1 日前）も使わない。要るのは
    as-of の上限（10 分）と流量の窓（60 分）を覆うぶんだけである。
    """
    hours = parquet_hours(day, profile.LOOKBACK_HOURS, 0)
    return load_snapshots(source, day, cache, hours)


def read_table(source: ReadsStorage, day: date, name: str, schema: pa.Schema) -> pa.Table | None:
    """置いてある版を読む。**無ければ None**（初日と、欠けた日のため）。"""
    body = source.download(PARQUET_BUCKET, profile_path(day, name))
    if body is None:
        return None
    table = pq.read_table(pa.BufferReader(body))
    profile.require_schema(table, schema)
    return table


def build_one_day(source: ProfilesPort, day: date, cache: Path | None = None) -> Made:
    """1 日ぶんを作る。**置かない**（副作用は呼ぶ側が決める）。"""
    table, missing = read_day(source, day, cache)
    daily = profile.build_day(
        profile.DayInputs(day=day, table=table, holidays=frozenset(source.list_holidays()))
    )
    previous = read_table(source, day - _ONE_DAY, profile.PROFILE_NAME, profile.PROFILE_SCHEMA)
    expired = read_table(
        source, day - _ONE_DAY * profile.PROFILE_DAYS, profile.DAILY_NAME, profile.DAILY_SCHEMA
    )
    rolled = profile.roll(previous, daily, expired)
    return Made(daily=daily, profile=rolled, summary=_summary(daily, rolled, missing, previous))


_ONE_DAY: Final[timedelta] = timedelta(days=1)


def _summary(
    daily: pa.Table, rolled: pa.Table, missing: Sequence[str], previous: pa.Table | None
) -> dict[str, object]:
    """応答と CLI の出力。**足りないものを数で出す。**"""
    return {
        "daily_cells": daily.num_rows,
        "missing_hours": len(missing),
        # **前日の版が無ければ当日だけで作っている**（さかのぼるときに効く）
        "carried": previous is not None,
        **profile.summarize(rolled),
    }


def build_and_upload(source: ProfilesPort, day: date, cache: Path | None = None) -> Made:
    """作って置き、`job_runs` に記録する。

    **同じパスに上書きする**ので、同じ日を何度作っても結果は変わらない——ただし
    **`profile` は前日の版に依存する**ので、さかのぼるときは古い日から順に回す。

    **失敗も記録してから投げ直す**（`build_features` と同じ。記録が無いと「動いて
    いない」と「動いたが失敗した」が区別できない）。詰めるのは**例外の種類だけ**。
    """
    run_id = recording.started_quietly(source, JOB_NAME)
    try:
        made = build_one_day(source, day, cache)
        bodies = made.bodies()
        for name, body in bodies.items():
            source.upload_parquet(profile_path(day, name), body)
    except Exception as cause:
        recording.record_quietly(
            source, run_id, "failed", {"date": day.isoformat(), "error": type(cause).__name__}
        )
        raise
    detail = {
        "ok": True,
        "date": day.isoformat(),
        **made.summary,
        "bytes": {name: len(body) for name, body in bodies.items()},
    }
    recording.record_quietly(source, run_id, "ok", detail)
    return made


# ── 実行 ──────────────────────────────────────────────────────
def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ポートプロファイルを 1 日ぶん作る")
    parser.add_argument("--date", default=None, help="JST の暦日（既定は昨日）")
    parser.add_argument("--cache", default=None, help="毎時 Parquet の置き場")
    parser.add_argument("--out", default=None, help="書き出し先のディレクトリ")
    parser.add_argument("--upload", action="store_true", help="Storage に置く")
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    day = date.fromisoformat(args.date) if args.date else jst_yesterday(datetime.now(UTC))
    with open_storage(read_storage_config()) as source:
        made = (
            build_and_upload(source, day, _path(args.cache))
            if args.upload
            else build_one_day(source, day, _path(args.cache))
        )
    _write(args.out, made)
    print(json.dumps({"date": day.isoformat(), **made.summary}, ensure_ascii=False))
    return 0


def _path(value: str | None) -> Path | None:
    return None if value is None else Path(value)


def _write(out: str | None, made: Made) -> None:
    """`--out` のディレクトリに 2 つ書く。**置くときと同じ名前**にする。"""
    if out is None:
        return
    directory = Path(out)
    directory.mkdir(parents=True, exist_ok=True)
    for name, body in made.bodies().items():
        (directory / f"{name}.parquet").write_bytes(body)
        print(f"書き出した: {directory / f'{name}.parquet'}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(run())
