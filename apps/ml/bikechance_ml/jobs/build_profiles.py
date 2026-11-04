"""ポートプロファイルを 1 日ぶん作る（W5 プラン §6.2 の PR B）。

**ここが副作用の置き場所。** 数え方は `features/profile.py` にあり、このファイルは
「Storage から Parquet を取る」「書き出す」「`job_runs` に記録する」だけを持つ
（CLAUDE.md §3）。

**毎日 GitHub Actions が前日ぶんを作る**（`build_features` と同じワークフロー、
**その後**に走る）。**同じ回の中の順序は依存関係ではない**——`features/` が読むのは
**前日の版**（`profile.source_day`）で、それは前の回に置かれている（PR J1 から）。
順序は「取り返しがつかない順」で決めてある（`.github/workflows/build-features.yml`）。

**置くのは 2 つ。**

    profiles/date=YYYY-MM-DD/daily.parquet     ← その日ぶんの素の集計
    profiles/date=YYYY-MM-DD/profile.parquet   ← 直近 28 日の累計（読むのはこちら）

**`profile.parquet` は配信が部分読みする形で書く**（W5 の契約 28、W6 の PR C）：並びは
`dow_type, slot15, system_id, station_id`、**1 行群 ＝ 1 つの `(dow_type, slot15)`**
（`profile_bytes`）。推論は周期ごとに要る 8 枠ぶんの行群だけを落とす（W6 の PR D）。
**1 周期の読みを 2 MB 未満に保つ書き方**も `profile_bytes` の 1 か所に置く（統計は鍵の
2 列だけ、`station_id` は辞書にしない。W6 プランの所見 204）。`daily.parquet` は推論が
読まないので、前のままの書き方でよい。

**1 日に 2 回走る**（W6 の PR C、W6-04）。**00:40 JST の回はプロファイルだけ**を作り、
**06:00 JST の回は学習サンプルを作ったあと、プロファイルが無ければ作る**
（`--skip-if-exists`）。00:40 の回が作っていれば、06:00 の回は置き直さない——推論が
読んでいる最中のファイルを差し替える回数を減らす。

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

**`--prune` は古い `profile.parquet` を消す**（D-28 の ③、W8 の PR D）。**35 日より古い日**の
`profile.parquet` だけを、**1 回に 3 個まで**消す（日付を誤っても、1 回で失うのはそれだけ）。
**消すのは `profile.parquet` がまだ在ると確かめた日だけ**（W8 の PR H）——日付の階層は
`daily.parquet` のために残るので、階層の名前だけで決めると消し終えた日で枠を使い切る。
**`daily.parquet` は消さない**——`profile` は `daily` から作り直せる派生物である（`--from-dailies
--compare` が本番の 3 日でバイト一致した。2026-09-24）。記録は作る回と別の名前（`prune_profiles`）
に残す。**`--dry-run` は並べるだけ**で、消さず記録もしない（初めて消す前に一覧を見るため）。

    ./.venv/bin/python -m bikechance_ml.jobs.build_profiles --prune --dry-run

**`--from-dailies` は転がさずに作り直す**（`profile.sum_dailies` の入口。D-28、W5 プラン
§6.10 の J1）。Storage に置いてある `daily` を窓ぶん（28 日）読んで素直に足す。
**`--compare` を付けると、置いてある `profile.parquet` とバイト単位で突き合わせる**
（一致しなければ終了コード 1）。`profile` は `daily` から作り直せる派生物である——
**それを確かめるまで `profiles/` の掃除は入れない**（D-22 と同じ作法）。

    ./.venv/bin/python -m bikechance_ml.jobs.build_profiles \\
        --date 2026-09-23 --from-dailies --compare
"""

import argparse
import json
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final, Protocol

import pyarrow as pa
import pyarrow.parquet as pq

from bikechance_ml.baselines import climatology
from bikechance_ml.config import read_storage_config
from bikechance_ml.features import profile
from bikechance_ml.features.grid import jst_yesterday, parquet_hours, profile_path
from bikechance_ml.io.supabase import PARQUET_BUCKET, open_storage
from bikechance_ml.jobs import recording
from bikechance_ml.jobs.build_features import (
    ReadsStorage,
    load_snapshots,
    read_profile_table,
    to_parquet_bytes,
)
from bikechance_ml.jobs.snapshot_table import COMPRESSION

#: `job_runs` と `monitored_jobs` に載る名前。**1 日に 2 回まで**（GitHub Actions の 00:40 と
#: 06:00。06:00 の回は、00:40 の回が作っていれば何もせず、記録も残さない）。
JOB_NAME: Final[str] = "build_profiles"

#: 有無を確かめるときに並べる数の上限。**その日の階層には 2 つしか無い**（`daily`・`profile`）。
EXISTS_LIST_LIMIT: Final[int] = 10

#: **`profile.parquet` を残す日数**（D-28 の ③：28 日の窓と余白）。`daily.parquet` は消さない。
PROFILE_KEEP_DAYS: Final[int] = 35

#: 1 回に消す数の上限。**ふつうは 1 日 1 個**古くなるので、3 あれば 1 回の取りこぼしを
#: 翌日に取り返せる。**日付の計算を誤っても、1 回で失うのはこれだけ**にする。
PRUNE_MAX_PER_RUN: Final[int] = 3

#: `profiles/` の下を並べるときの上限（日付の階層の数。1 日 1 つ増える）。**名前の昇順**で
#: 並ぶので、上限に当たっても切れるのは新しい側で、消す側（古い日）は先に並ぶ。
PROFILES_LIST_LIMIT: Final[int] = 1000

#: **`profile.parquet` の無い日がこの数だけ続いたら、それより古い日は確かめない**（W8 の PR H）。
#: 消すのは古い順なので、残っている `profile.parquet` はいつも新しい側に連なる。新しい日から
#: 確かめて無い日が続けば、その先は消し終えている。**3 にするのは、作れなかった日（穴）が
#: 1〜2 日あっても、その先に残ったものを見落とさないため**。ふだんの回は「消す 1 日と無い 3 日」の
#: 4 回の確かめで終わる（消してよい日は 1 日 1 つ増え、日付の階層は `daily.parquet` のために残る）。
PRUNE_MISSING_STREAK: Final[int] = 3

#: 掃除の `job_runs` の名前。**作る回（`build_profiles`）とは分ける**——掃除の失敗が
#: 「プロファイルを作れなかった」と混ざらないように。失敗は `job_failed:prune_profiles` で鳴る。
PRUNE_JOB_NAME: Final[str] = "prune_profiles"

#: `profile_path` の頭（`features/grid.py`）。**一覧はこの 1 階層を並べる。**
PROFILES_PREFIX: Final[str] = "profiles/"

#: `profiles/` の下の日付の階層の名前（`date=2026-09-07`）。**この形でない名前は拾わない。**
_FOLDER: Final[re.Pattern[str]] = re.compile(r"date=(\d{4}-\d{2}-\d{2})")

#: `profile.parquet` で**辞書にせず、差分で書く列**（W6 プランの所見 204）。
#:
#: 1 行群はほぼ全ポート（約 2 万 1 千）を含むので、`station_id` を辞書にすると**行群ごとに
#: 2 万語の辞書を持ち直す**——既定の書き方では、1 周期の読み（フッタを含む）の 26% がこれ
#: だった。行群の中は `system_id, station_id` の順に並んでいて前の行との一致が長いので、
#: 差分（DELTA_BYTE_ARRAY）で書くと 1 行群 66 → 13 KB になる（2026-09-27、本番の
#: `profile(09-26)` で測った）。
DELTA_ENCODED: Final[Mapping[str, str]] = {"station_id": "DELTA_BYTE_ARRAY"}

#: `profile.parquet` で**統計（最小と最大）を書く列**。**行群を選ぶ鍵の 2 列だけ**。
#:
#: 配信（W6 の PR D）はこの 2 列の最小と最大で要る行群を選ぶ。ほかの列の統計は誰も読まず、
#: 3,744 個（288 行群 × 13 列）の断片ごとにフッタを太らせる——配信は周期ごとにフッタを
#: 読むので、そのまま下りになる（フッタ 359 → 263 KB。所見 204）。
STATISTICS_COLUMNS: Final[tuple[str, ...]] = profile.ROW_GROUP_KEY


class ProfilesPort(ReadsStorage, Protocol):
    """このジョブが使う入出力ぜんぶ。`io/supabase.py` の実装が構造的に満たす。

    **参照スナップショットも天気も読まない。** 数えるのに要らないものを前提にしない
    （`features/profile.py` の `DayInputs`）。
    """

    def list_holidays(self) -> tuple[date, ...]: ...
    def list_objects(self, bucket: str, prefix: str, limit: int) -> tuple[str, ...]: ...
    def upload_parquet(self, path: str, body: bytes) -> None: ...
    def job_started(self, job_name: str) -> int: ...
    def job_finished(self, run_id: int, status: str, detail: Mapping[str, object]) -> None: ...


class ListsObjects(Protocol):
    """Storage の 1 階層を並べる口だけ（有無の確かめ。`profile_exists`）。"""

    def list_objects(self, bucket: str, prefix: str, limit: int) -> tuple[str, ...]: ...


class PrunesProfiles(Protocol):
    """掃除（`--prune`）が使う入出力。**一覧と削除と記録だけ**（作る道の口は要らない）。"""

    def list_objects(self, bucket: str, prefix: str, limit: int) -> tuple[str, ...]: ...
    def remove_objects(self, bucket: str, paths: Sequence[str]) -> tuple[str, ...]: ...
    def job_started(self, job_name: str) -> int: ...
    def job_finished(self, run_id: int, status: str, detail: Mapping[str, object]) -> None: ...


@dataclass(frozen=True)
class Made:
    """作った 1 日ぶん。`daily` と `profile` の両方を置く。"""

    daily: pa.Table
    profile: pa.Table
    summary: dict[str, object]

    def bodies(self) -> dict[str, bytes]:
        """置く 2 つ。**`profile` だけ契約 28 の形で書く**（`profile_bytes`）。"""
        return {
            profile.DAILY_NAME: to_parquet_bytes(self.daily),
            profile.PROFILE_NAME: profile_bytes(self.profile),
        }


def profile_bytes(table: pa.Table) -> bytes:
    """`profile.parquet` のバイト列（**W5 の契約 28**）。**1 行群 ＝ 1 つの `(dow_type, slot15)`。**

    **行数ではなく鍵の変わり目で切る**（`profile.row_groups`。並びが崩れていれば書く前に止まる）。
    行群ごとに `write_table` を 1 回呼び、その行群の行数ちょうどを上限に渡すので、1 回が
    1 行群になる。**統計は鍵の 2 列だけ、`station_id` は差分で書く**（`STATISTICS_COLUMNS`・
    `DELTA_ENCODED`。既定のままだと本番の 1 周期が 2 MB を超えた。所見 204）。
    **同じ表からは同じバイト列が出る**（圧縮は `to_parquet_bytes` と同じ）。
    """
    bounds = profile.row_groups(table)
    sink = pa.BufferOutputStream()
    with pq.ParquetWriter(
        sink,
        table.schema,
        compression=COMPRESSION,
        write_statistics=list(STATISTICS_COLUMNS),
        use_dictionary=[one for one in table.schema.names if one not in DELTA_ENCODED],
        column_encoding=dict(DELTA_ENCODED),
    ) as writer:
        for start, stop in bounds:
            writer.write_table(table.slice(start, stop - start), row_group_size=stop - start)
    return bytes(sink.getvalue())


def profile_exists(source: ListsObjects, day: date) -> bool:
    """その日の `profile.parquet` が Storage に在るか。**一覧で確かめる**（`list_objects`）。"""
    folder, _, name = profile_path(day, profile.PROFILE_NAME).rpartition("/")
    return name in source.list_objects(PARQUET_BUCKET, f"{folder}/", EXISTS_LIST_LIMIT)


def read_day(
    source: ReadsStorage, day: date, cache: Path | None
) -> tuple[pa.Table, tuple[str, ...]]:
    """その日の観測を読む。**学習の 25 時間ではなく 2 時間の余白**（W5 プラン §6.2）。

    プロファイルはラベル（`t + h`）も同時刻履歴（1 日前）も使わない。要るのは
    as-of の上限（10 分）と流量の窓（60 分）を覆うぶんだけである。
    """
    hours = parquet_hours(day, profile.LOOKBACK_HOURS, 0)
    return load_snapshots(source, day, cache, hours)


def build_one_day(source: ProfilesPort, day: date, cache: Path | None = None) -> Made:
    """1 日ぶんを作る。**置かない**（副作用は呼ぶ側が決める）。"""
    table, missing = read_day(source, day, cache)
    daily = profile.build_day(
        profile.DayInputs(day=day, table=table, holidays=frozenset(source.list_holidays()))
    )
    previous = read_profile_table(
        source, day - _ONE_DAY, profile.PROFILE_NAME, profile.PROFILE_SCHEMA
    )
    expired = read_profile_table(
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
        # **使えるセルは当てはめと同じ下限で数える**（持ち主は読む側。W5-03・D-37）
        **profile.summarize(rolled, serve_days=climatology.SERVE_DAYS),
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


# ── `daily` から作り直す（`profile.sum_dailies` の入口。D-28）──────────
@dataclass(frozen=True)
class Summed:
    """`daily` を足して作り直した版と、**実際に足した日**。"""

    profile: pa.Table
    days: tuple[date, ...]

    def body(self) -> bytes:
        """置いてある `profile.parquet` と同じ書き方（契約 28）。**`--compare` はこれと比べる。**"""
        return profile_bytes(self.profile)


def window_days(day: date) -> tuple[date, ...]:
    """`profile(day)` に入る日（`day - 27 … day`）。**転がしの窓と同じ**（`PROFILE_DAYS`）。"""
    return tuple(day - _ONE_DAY * back for back in range(profile.PROFILE_DAYS - 1, -1, -1))


def sum_from_dailies(source: ReadsStorage, day: date) -> Summed:
    """**置いてある `daily` だけから** `profile(day)` を作り直す。毎時の観測は読まない。

    **無い日は飛ばす**（転がしと同じ。収集を始める前の日には `daily` が無い）。足した日は
    `days` に残るので、窓に穴があれば数で分かる。**読んだそばから畳む**（`sum_dailies` に
    生成器を渡す）ので、28 日ぶんを一度に抱えない。
    """
    found: list[date] = []

    def dailies() -> Iterator[pa.Table]:
        for one in window_days(day):
            table = read_profile_table(source, one, profile.DAILY_NAME, profile.DAILY_SCHEMA)
            if table is not None:
                found.append(one)
                yield table

    summed = profile.sum_dailies(dailies())
    return Summed(profile=summed, days=tuple(found))


def compare_with_stored(source: ReadsStorage, day: date, summed: Summed) -> dict[str, object]:
    """作り直した版を、**置いてある `profile.parquet` とバイト単位で**突き合わせる。

    **読み直した表どうしではなく、置いてあるバイト列そのもの**と比べる——同じ表から
    同じバイト列が出ること（`profile_bytes`）まで含めて確かめるためである。

    **比べられるのは契約 28 の書き方で置いた版だけ**（W6 の PR C のマージの後に作った日から）。
    それより前の版は、中身が同じでも並びと書き方が違うので一致しない。
    """
    stored = source.download(PARQUET_BUCKET, profile_path(day, profile.PROFILE_NAME))
    body = summed.body()
    return {
        "identical": stored is not None and stored == body,
        "stored_bytes": None if stored is None else len(stored),
        "summed_bytes": len(body),
    }


# ── 古い `profile.parquet` を消す（D-28 の ③。W8 の PR D）──────────
@dataclass(frozen=True)
class Pruned:
    """1 回の掃除の結果。**まだ在る日・この回に消す名前・実際に消えた名前**を分ける。"""

    day: date
    #: 消してよい日のうち、`profile.parquet` がまだ在った日（古い順）
    found: tuple[date, ...]
    #: 有無を確かめた日の数（新しい日から。`PRUNE_MISSING_STREAK` で打ち切る）
    checked: int
    targets: tuple[str, ...]
    removed: tuple[str, ...]

    def summary(self) -> dict[str, object]:
        """記録と CLI の出力。**`pending` が 0 でなければ、翌日以降の回が続きを消す。**"""
        return {
            "date": self.day.isoformat(),
            "cutoff": prune_cutoff(self.day).isoformat(),
            "checked": self.checked,
            "found": [one.isoformat() for one in self.found],
            "removed": list(self.removed),
            "pending": len(self.found) - len(self.targets),
        }


def prune_cutoff(day: date) -> date:
    """`day` を基準に、**この日以前**の `profile.parquet` を消してよい（35 日を越えたもの）。"""
    return day - _ONE_DAY * PROFILE_KEEP_DAYS


def folder_day(name: str) -> date | None:
    """`date=2026-09-07` → その日。**形の違う名前・在り得ない日付は None**（拾わない）。"""
    matched = _FOLDER.fullmatch(name)
    if matched is None:
        return None
    try:
        return date.fromisoformat(matched.group(1))
    except ValueError:
        return None


def days_to_prune(folders: Sequence[str], day: date) -> tuple[date, ...]:
    """消してよい日（**古い順・重複なし**）。`prune_cutoff(day)` 以前の日だけ。"""
    cutoff = prune_cutoff(day)
    found = {one for one in map(folder_day, folders) if one is not None and one <= cutoff}
    return tuple(sorted(found))


def find_profiles(source: ListsObjects, due: Sequence[date]) -> tuple[tuple[date, ...], int]:
    """消してよい日のうち、**`profile.parquet` がまだ在る日**（古い順）と、確かめた日の数。

    **日付の階層の名前だけでは決めない**（W8 の PR H）。階層は `daily.parquet` のために残るので、
    消し終えた日も一覧に出る——それで決めると、1 回の枠を消し終えた日に使い切り、何も消えない
    （10/28 の定時の回がそうだった）。**新しい日から確かめ、無い日が `PRUNE_MISSING_STREAK` 日
    続いたら打ち切る**（その先は消し終えている）。
    """
    found: list[date] = []
    missing_streak = 0
    checked = 0
    for one in reversed(due):
        if missing_streak >= PRUNE_MISSING_STREAK:
            break
        checked += 1
        present = profile_exists(source, one)
        found += [one] if present else []
        missing_streak = 0 if present else missing_streak + 1
    return tuple(sorted(found)), checked


def prune_profiles(
    source: PrunesProfiles, day: date, *, limit: int = PRUNE_MAX_PER_RUN, dry_run: bool = False
) -> Pruned:
    """古い `profile.parquet` を**古い順に `limit` 個まで**消す。**`daily.parquet` には触らない。**

    消す名前は日付から組み立てる（`profile_path`）——一覧に出た名前をそのまま消さないので、
    `daily.parquet` や形の違う名前が紛れ込む道が無い。**消すのはまだ在ると確かめた日だけ。**
    """
    folders = source.list_objects(PARQUET_BUCKET, PROFILES_PREFIX, PROFILES_LIST_LIMIT)
    found, checked = find_profiles(source, days_to_prune(folders, day))
    targets = tuple(profile_path(one, profile.PROFILE_NAME) for one in found[:limit])
    removed = () if dry_run or not targets else source.remove_objects(PARQUET_BUCKET, targets)
    return Pruned(day=day, found=found, checked=checked, targets=targets, removed=removed)


def prune_and_record(source: PrunesProfiles, day: date, limit: int) -> Pruned:
    """消して、`job_runs` の `prune_profiles` に記録する。**失敗も記録してから投げ直す。**"""
    run_id = recording.started_quietly(source, PRUNE_JOB_NAME)
    try:
        pruned = prune_profiles(source, day, limit=limit)
    except Exception as cause:
        recording.record_quietly(
            source, run_id, "failed", {"date": day.isoformat(), "error": type(cause).__name__}
        )
        raise
    recording.record_quietly(source, run_id, "ok", pruned.summary())
    return pruned


# ── 実行 ──────────────────────────────────────────────────────
def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ポートプロファイルを 1 日ぶん作る")
    parser.add_argument("--date", default=None, help="JST の暦日（既定は昨日）")
    parser.add_argument("--cache", default=None, help="毎時 Parquet の置き場")
    parser.add_argument("--out", default=None, help="書き出し先のディレクトリ")
    parser.add_argument("--upload", action="store_true", help="Storage に置く")
    parser.add_argument(
        "--skip-if-exists",
        action="store_true",
        help="その日の profile.parquet が Storage に在れば作らない（定時の回。--upload と使う）",
    )
    parser.add_argument(
        "--from-dailies",
        action="store_true",
        help="転がさず、置いてある daily を 28 日ぶん足して作り直す（置かない）",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="--from-dailies の結果を置いてある profile.parquet とバイト単位で比べる",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help=f"{PROFILE_KEEP_DAYS} 日より古い profile.parquet を消す（作らない。daily は消さない）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="--prune で、消すものを並べるだけ（消さず記録もしない）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=f"--prune で 1 回に消す数の上限（既定 {PRUNE_MAX_PER_RUN}）",
    )
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    refused = _refused(args)
    if refused is not None:
        print(refused, file=sys.stderr)
        return 2
    yesterday = jst_yesterday(datetime.now(UTC))
    day = date.fromisoformat(args.date) if args.date else yesterday
    if args.prune:
        return _run_prune(args, day, yesterday)
    if args.from_dailies:
        return _run_from_dailies(args, day)
    with open_storage(read_storage_config()) as source:
        made = _build(args, source, day)
    if made is None:
        print(json.dumps({"date": day.isoformat(), "skipped": "exists"}, ensure_ascii=False))
        return 0
    _write(args.out, made)
    print(json.dumps({"date": day.isoformat(), **made.summary}, ensure_ascii=False))
    return 0


def _refused(args: argparse.Namespace) -> str | None:
    """一緒に使えない組み合わせなら、その理由。**Storage を開く前に止める。**"""
    pruning = _refused_pruning(args)
    if pruning is not None:
        return pruning
    if args.compare and not args.from_dailies:
        return "--compare は --from-dailies と一緒に使います"
    if args.from_dailies and args.upload:
        return "--from-dailies は置きません（作り直した版を置くのは掃除を入れるときに決める。D-28）"
    if args.skip_if_exists and not args.upload:
        return "--skip-if-exists は --upload と一緒に使います（置く回が、在れば作らないためのもの）"
    return None


def _refused_pruning(args: argparse.Namespace) -> str | None:
    """`--prune` の組み合わせ。**掃除は作らない**ので、作る道の指定とは一緒に使えない。"""
    if not args.prune:
        if args.dry_run or args.limit is not None:
            return "--dry-run と --limit は --prune と一緒に使います"
        return None
    building = (args.upload, args.skip_if_exists, args.from_dailies, args.compare, args.out)
    if any(building) or args.cache:
        return "--prune は作りません（消すだけ）。--upload などの作る道の指定とは一緒に使えません"
    if args.limit is not None and args.limit < 1:
        return "--limit は 1 以上です"
    return None


def _run_prune(args: argparse.Namespace, day: date, yesterday: date) -> int:
    """`--prune` の道。**昨日より新しい日を基準にしない**（先の日付で、残すべき版を消さない）。"""
    if day > yesterday:
        print(f"--prune の --date は昨日（{yesterday}）以前です", file=sys.stderr)
        return 2
    limit = PRUNE_MAX_PER_RUN if args.limit is None else args.limit
    with open_storage(read_storage_config()) as source:
        pruned = (
            prune_profiles(source, day, limit=limit, dry_run=True)
            if args.dry_run
            else prune_and_record(source, day, limit)
        )
    print(json.dumps({**pruned.summary(), "dry_run": args.dry_run}, ensure_ascii=False))
    return 0


def _build(args: argparse.Namespace, source: ProfilesPort, day: date) -> Made | None:
    """日次の回。**`--skip-if-exists` で在れば作らずに None。**

    **`job_runs` にも残さない。** 1 行は「作った回」で、見張り（`check_jobs_missing`）は
    `ok` の行が来ているかだけを見る。00:40 の回が作った日は、その回の `ok` が残っている。
    """
    if args.skip_if_exists and profile_exists(source, day):
        return None
    cache = _path(args.cache)
    return (
        build_and_upload(source, day, cache) if args.upload else build_one_day(source, day, cache)
    )


def _run_from_dailies(args: argparse.Namespace, day: date) -> int:
    """`--from-dailies` の道。**置かない**——作り直した版を置くのは掃除を入れるときに決める。"""
    with open_storage(read_storage_config()) as source:
        summed = sum_from_dailies(source, day)
        compared = compare_with_stored(source, day, summed) if args.compare else {}
    if args.out:
        directory = Path(args.out)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{profile.PROFILE_NAME}.parquet").write_bytes(summed.body())
    summary = {
        "date": day.isoformat(),
        "from_dailies": True,
        "dailies": len(summed.days),
        "first_day": summed.days[0].isoformat() if summed.days else None,
        **profile.summarize(summed.profile, serve_days=climatology.SERVE_DAYS),
        **compared,
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if args.compare and not compared.get("identical") else 0


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
