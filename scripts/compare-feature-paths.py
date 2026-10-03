#!/usr/bin/env python3
"""特徴量の経路 B（W6 の PR G）の前と後を、**本番の過去のデータ**で突き合わせる（契約 40）。

**PR G は出力を 1 バイトも変えない約束である**（W6-20）。前の実装は
`apps/ml/tests/legacy_feature_path.py` に写してあり、ここではそれを差し込んだ**前の経路**と、
いまの**新しい経路**に**同じ入力**を通して比べる。入力は本番から 1 度だけ読み、両方に同じ
ものを渡す（読み直すと、その間に届いた行で食い違いうる）。

比べる 3 か所（W6 プラン §6.7 の完了条件 1）：

  1. `build_now`（推論）…… 過去の 5 時刻 × 2 系統。**日をまたぐ時刻を含める**。新しい経路は
     2 回走らせ、**参照を使い回した 2 回目**（⑤）も前と一致することを見る
  2. `build_day`（学習）…… 過去の 2 日。前の経路・新しい経路・**保存済みの `features/`**
  3. `to_table`（毎時の Parquet）…… 過去の 2 時間 × 2 系統。前の経路・新しい経路・
     **保存済みの毎時の Parquet**。デプロイ後の時間を渡せば、完了条件 4 もこれで見られる

「前 ＝ 新」が契約 40 そのもので、**前の実装が本当に呼ばれた回数**も表に出す（差し込みが
効いていなければ、新しい経路どうしを比べてしまう）。保存済みとの一致は、本番に置かれた物が
同じ作り方でできていることの確かめである。食い違ったら原因を別に調べる——PR G のせいとは
限らない（後から届いた行・その後の別の変更）。

使い方（環境変数は `.env` から読み込んでから。**読むだけで、何も置かない**）:

    set -a; . ./.env; set +a
    cd apps/ml
    ./.venv/bin/python ../../scripts/compare-feature-paths.py

省略すると、時刻は**昨日（JST）**の 00:05・07:30・12:00・18:30・23:55、日は 2 日前と
3 日前、時間は昨日の 03 時と 14 時（UTC）になる。`--at`・`--day`・`--hour` で渡せ、
`--only` で 1 か所だけを走らせられる。毎時の Parquet は `--cache`（既定 `.cache/parquet`）に
置いて使い回す。**合格しなければ終了コード 1。**
"""

import argparse
import hashlib
import importlib.util
import json
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import ModuleType
from typing import Final

import httpx
import pyarrow as pa

from bikechance_ml.config import Config, read_storage_config
from bikechance_ml.features import asof, flow
from bikechance_ml.features.grid import JST, features_path, jst_date
from bikechance_ml.io import supabase
from bikechance_ml.io.range_file import Piece
from bikechance_ml.io.supabase import (
    CONNECT_TIMEOUT_S,
    DOWNLOAD_TIMEOUT_S,
    PARQUET_BUCKET,
    SupabaseIo,
)
from bikechance_ml.jobs import infer
from bikechance_ml.jobs.build_features import SYSTEM_IDS, build_one_day
from bikechance_ml.jobs.snapshot_table import (
    parquet_path,
    station_ids_by_idx,
    to_parquet_bytes,
    to_table,
)

LEGACY_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent / "apps" / "ml" / "tests" / "legacy_feature_path.py"
)

#: 前の経路に差し替える所：（モジュール、そこでの名前、前の実装の名前）。**呼ぶ側がモジュールの
#: 属性として引く所**を差し替える。`from … import` で名前を写した所は、写した先を差し替える
#: （`supabase.as_int_list`・`infer.to_snapshot_table`）。`asof` と `flow` は、`build`・`profile` が
#: `asof.to_observations`・`flow.compute_flow` の形で引く
SWAPS: Final[tuple[tuple[ModuleType, str, str], ...]] = (
    (supabase, "as_int_list", "as_int_list"),
    (infer, "to_snapshot_table", "to_table"),
    (asof, "to_observations", "to_observations"),
    (flow, "compute_flow", "compute_flow"),
)

#: 既定の 5 時刻（**昨日の JST**）。00:05 と 23:55 は、窓が日をまたぐ
DEFAULT_TIMES: Final[tuple[time, ...]] = (
    time(0, 5),
    time(7, 30),
    time(12, 0),
    time(18, 30),
    time(23, 55),
)
#: 既定の 2 日（今日から何日前か）。**保存済みの `features/` が確実に在る日**にする
DEFAULT_DAYS_BACK: Final[tuple[int, ...]] = (3, 2)
#: 既定の 2 時間（**昨日の UTC の正時**）。昼の混む時間と、夜
DEFAULT_HOURS_UTC: Final[tuple[int, ...]] = (3, 14)

PARTS: Final[tuple[str, ...]] = ("now", "day", "hour")


def load_legacy() -> ModuleType:
    """前の実装（検査の中の写し）を、**ファイルの場所から**読み込む（`sys.path` を触らない）。"""
    spec = importlib.util.spec_from_file_location("legacy_feature_path", LEGACY_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"前の実装を読み込めない: {LEGACY_PATH.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── 同じ入力を 2 度渡す口 ──────────────────────────────────────
class Remembered(SupabaseIo):
    """本番から読んだものを覚え、**2 度目からは同じものを返す**口（読むだけ）。

    覚えるのは PostgREST の行（**型検査の前の JSON**。だから①は新旧の両方を通る）と、
    Storage のバイト列。**毎時の Parquet は覚えない**（1 日で 1 GB を超える）——それは
    手元のキャッシュ（`build_one_day` の `cache`）が同じ役をする。
    """

    def __init__(self, config: Config, client: httpx.Client) -> None:
        super().__init__(config, client)
        self._pages: dict[str, list[object]] = {}
        self._objects: dict[tuple[str, str], bytes | None] = {}
        self._ranges: dict[tuple[str, str, str], Piece | None] = {}

    def _paged(self, path: str, params: Mapping[str, str], page: int, where: str) -> list[object]:
        key = json.dumps([path, sorted(params.items()), page, where])
        if key not in self._pages:
            self._pages[key] = super()._paged(path, params, page, where)
        return self._pages[key]

    def download(self, bucket: str, path: str) -> bytes | None:
        if "/hour=" in path:
            return super().download(bucket, path)
        if (bucket, path) not in self._objects:
            self._objects[(bucket, path)] = super().download(bucket, path)
        return self._objects[(bucket, path)]

    def download_range(self, bucket: str, path: str, byte_range: str) -> Piece | None:
        if (bucket, path, byte_range) not in self._ranges:
            self._ranges[(bucket, path, byte_range)] = super().download_range(
                bucket, path, byte_range
            )
        return self._ranges[(bucket, path, byte_range)]


@contextmanager
def open_remembered() -> Iterator[Remembered]:
    """`open_storage` と同じ組み立て（`CRON_SECRET` を要求しない）で、覚える口を返す。"""
    storage = read_storage_config()
    config = Config(
        supabase_url=storage.supabase_url,
        supabase_secret_key=storage.supabase_secret_key,
        cron_secret="",
    )
    timeout = httpx.Timeout(DOWNLOAD_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
    with httpx.Client(timeout=timeout) as client:
        yield Remembered(config, client)


# ── 前の経路 ──────────────────────────────────────────────────
def _counted[**P, T](name: str, work: Callable[P, T], counts: dict[str, int]) -> Callable[P, T]:
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        counts[name] = counts.get(name, 0) + 1
        return work(*args, **kwargs)

    return wrapper


@contextmanager
def legacy_path(legacy: ModuleType, counts: dict[str, int]) -> Iterator[None]:
    """前の実装に差し替える（**抜けたら必ず戻す**）。呼ばれた回数を `counts` に数える。

    **参照のキャッシュも捨てる**：前の経路は 5 分毎に読んで組み立て直していた（⑤の前）。
    """
    originals = [(module, name, getattr(module, name)) for module, name, _ in SWAPS]
    try:
        for module, name, legacy_name in SWAPS:
            setattr(module, name, _counted(legacy_name, getattr(legacy, legacy_name), counts))
        infer.forget_references()
        yield
    finally:
        for module, name, original in originals:
            setattr(module, name, original)
        infer.forget_references()


# ── 比べ方 ────────────────────────────────────────────────────
def table_digest(table: pa.Table) -> str:
    """表のバイト列（Arrow IPC。**チャンクの分かれ方・NaN・NULL の位置・型**を含む）の SHA-256。"""
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return hashlib.sha256(sink.getvalue()).hexdigest()


def body_digest(body: bytes | None) -> str | None:
    return None if body is None else hashlib.sha256(body).hexdigest()


@dataclass(frozen=True)
class NowCompared:
    """1 時刻 × 1 系統の `build_now`。"""

    at: datetime
    system_id: str
    rows: int
    same: bool
    #: 参照を使い回した 2 回目（⑤）も前と一致したか
    same_reused: bool
    caches: tuple[str, str]
    legacy_calls: Mapping[str, int] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        legacy_ran = all(self.legacy_calls.get(name, 0) > 0 for _, _, name in SWAPS)
        return self.same and self.same_reused and legacy_ran and self.caches == ("miss", "hit")


def compare_now(
    port: Remembered, legacy: ModuleType, system_id: str, at: datetime, holidays: frozenset[date]
) -> NowCompared:
    counts: dict[str, int] = {}
    with legacy_path(legacy, counts):
        old = infer.read_features(port, system_id, at, holidays)
    infer.forget_references()
    new = infer.read_features(port, system_id, at, holidays)
    again = infer.read_features(port, system_id, at, holidays)
    expected = (table_digest(old.ready.table), old.ready.stats)
    return NowCompared(
        at=at,
        system_id=system_id,
        rows=new.ready.table.num_rows,
        same=(table_digest(new.ready.table), new.ready.stats) == expected,
        same_reused=(table_digest(again.ready.table), again.ready.stats) == expected,
        caches=(new.stages.reference_cache, again.stages.reference_cache),
        legacy_calls=dict(counts),
    )


@dataclass(frozen=True)
class DayCompared:
    """1 日ぶんの `build_day`（**書き出したバイト列**で比べる）。"""

    day: date
    bytes_new: int
    same: bool
    #: 保存済みの `features/` と一致したか。**無ければ None**
    same_as_stored: bool | None
    legacy_calls: Mapping[str, int] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        # 学習は Parquet を読むので、①②は通らない。③④が前の実装で走ったことを見る
        legacy_ran = all(
            self.legacy_calls.get(name, 0) > 0 for name in ("to_observations", "compute_flow")
        )
        return self.same and self.same_as_stored is True and legacy_ran


def compare_day(port: Remembered, legacy: ModuleType, day: date, cache: Path) -> DayCompared:
    counts: dict[str, int] = {}
    with legacy_path(legacy, counts):
        old = build_one_day(port, day, cache).body
    new = build_one_day(port, day, cache).body
    stored = port.download(PARQUET_BUCKET, features_path(day))
    return DayCompared(
        day=day,
        bytes_new=len(new),
        same=body_digest(old) == body_digest(new),
        same_as_stored=None if stored is None else body_digest(stored) == body_digest(new),
        legacy_calls=dict(counts),
    )


@dataclass(frozen=True)
class HourCompared:
    """1 時間 × 1 系統の `to_table`（**毎時の Parquet と同じ書き方**のバイト列で比べる）。"""

    hour: datetime
    system_id: str
    rows: int
    same: bool
    same_as_stored: bool | None

    @property
    def passed(self) -> bool:
        return self.same and self.same_as_stored is True


def compare_hour(
    port: Remembered, legacy: ModuleType, system_id: str, hour: datetime
) -> HourCompared:
    station_ids = station_ids_by_idx(port.list_stations(system_id))
    snapshots = port.list_snapshots(system_id, hour, hour + timedelta(hours=1))
    new = to_table(system_id, station_ids, snapshots)
    old = legacy.to_table(system_id, station_ids, snapshots)
    body = to_parquet_bytes(new)
    stored = port.download(PARQUET_BUCKET, parquet_path(system_id, hour))
    return HourCompared(
        hour=hour,
        system_id=system_id,
        rows=new.num_rows,
        same=body_digest(to_parquet_bytes(old)) == body_digest(body),
        same_as_stored=None if stored is None else body_digest(stored) == body_digest(body),
    )


# ── 出力 ──────────────────────────────────────────────────────
def mark(value: bool | None) -> str:
    return "—（無い）" if value is None else ("○" if value else "**×**")


def render_now(results: Sequence[NowCompared]) -> list[str]:
    rows = [
        "### 1. `build_now`（推論）",
        "",
        "| 時刻（JST） | 系統 | 行 | 前 ＝ 新 | 前 ＝ 新（使い回し） | 参照 | 前の実装の呼び出し |",
        "|---|---|---:|---|---|---|---|",
    ]
    for one in results:
        calls = "・".join(f"{name} {count}" for name, count in sorted(one.legacy_calls.items()))
        rows.append(
            f"| {one.at.astimezone(JST):%m-%d %H:%M} | {one.system_id} | {one.rows:,} "
            f"| {mark(one.same)} | {mark(one.same_reused)} | {' → '.join(one.caches)} | {calls} |"
        )
    return rows


def render_day(results: Sequence[DayCompared]) -> list[str]:
    rows = [
        "### 2. `build_day`（学習）",
        "",
        "| 日 | バイト | 前 ＝ 新 | 新 ＝ 保存済み | 前の実装の呼び出し |",
        "|---|---:|---|---|---|",
    ]
    for one in results:
        calls = "・".join(f"{name} {count}" for name, count in sorted(one.legacy_calls.items()))
        rows.append(
            f"| {one.day} | {one.bytes_new:,} | {mark(one.same)} "
            f"| {mark(one.same_as_stored)} | {calls} |"
        )
    return rows


def render_hour(results: Sequence[HourCompared]) -> list[str]:
    rows = [
        "### 3. `to_table`（毎時の Parquet）",
        "",
        "| 時間（UTC） | 系統 | 行 | 前 ＝ 新 | 新 ＝ 保存済み |",
        "|---|---|---:|---|---|",
    ]
    for one in results:
        rows.append(
            f"| {one.hour:%m-%d %H}時 | {one.system_id} | {one.rows:,} "
            f"| {mark(one.same)} | {mark(one.same_as_stored)} |"
        )
    return rows


# ── 実行 ──────────────────────────────────────────────────────
def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="特徴量の経路 B の前と後を突き合わせる")
    parser.add_argument("--at", action="append", default=[], help="推論の時刻（ISO、5 分格子）")
    parser.add_argument("--day", action="append", default=[], help="学習の日（JST の暦日）")
    parser.add_argument("--hour", action="append", default=[], help="毎時の時間（ISO、UTC 正時）")
    parser.add_argument("--only", action="append", choices=PARTS, default=[], help="走らせる所")
    parser.add_argument("--cache", default=".cache/parquet", help="毎時の Parquet の置き場")
    return parser.parse_args(argv)


def chosen_times(texts: Sequence[str], today: date) -> list[datetime]:
    if texts:
        return [datetime.fromisoformat(one).astimezone(UTC) for one in texts]
    yesterday = today - timedelta(days=1)
    return [datetime.combine(yesterday, one, tzinfo=JST).astimezone(UTC) for one in DEFAULT_TIMES]


def chosen_days(texts: Sequence[str], today: date) -> list[date]:
    if texts:
        return [date.fromisoformat(one) for one in texts]
    return [today - timedelta(days=back) for back in DEFAULT_DAYS_BACK]


def chosen_hours(texts: Sequence[str], now: datetime) -> list[datetime]:
    if texts:
        return [datetime.fromisoformat(one).astimezone(UTC) for one in texts]
    yesterday = (now - timedelta(days=1)).date()
    return [datetime.combine(yesterday, time(one), tzinfo=UTC) for one in DEFAULT_HOURS_UTC]


@dataclass
class Report:
    now: list[NowCompared] = field(default_factory=list)
    day: list[DayCompared] = field(default_factory=list)
    hour: list[HourCompared] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        flags = [
            *(one.passed for one in self.now),
            *(one.passed for one in self.day),
            *(one.passed for one in self.hour),
        ]
        return bool(flags) and all(flags)


def run_parts(options: argparse.Namespace, port: Remembered, legacy: ModuleType) -> Report:
    now = datetime.now(UTC)
    parts = set(options.only or PARTS)
    report = Report()
    if "now" in parts:
        holidays = frozenset(port.list_holidays())
        for at in chosen_times(options.at, jst_date(now)):
            for system_id in SYSTEM_IDS:
                report.now.append(compare_now(port, legacy, system_id, at, holidays))
    if "day" in parts:
        for day in chosen_days(options.day, jst_date(now)):
            report.day.append(compare_day(port, legacy, day, Path(options.cache)))
    if "hour" in parts:
        for hour in chosen_hours(options.hour, now):
            report.hour.extend(compare_hour(port, legacy, one, hour) for one in SYSTEM_IDS)
    return report


def run(argv: Sequence[str] | None = None) -> int:
    options = _arguments(argv)
    legacy = load_legacy()
    with open_remembered() as port:
        report = run_parts(options, port, legacy)
    sections = [
        render_now(report.now) if report.now else [],
        render_day(report.day) if report.day else [],
        render_hour(report.hour) if report.hour else [],
    ]
    lines = [line for section in sections if section for line in [*section, ""]]
    lines.append(f"**{'合格' if report.passed else '不合格'}**（契約 40）")
    print("\n".join(lines))
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(run())
