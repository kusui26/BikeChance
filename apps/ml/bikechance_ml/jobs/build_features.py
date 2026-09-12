"""学習サンプルを 1 日ぶん作る（W3 プラン §5.8）。

**ここが副作用の置き場所。** 特徴量の規則は `features/` にあり、このファイルは
「Storage から Parquet を取る」「PostgREST から参照データを読む」「書き出す」だけを
持つ（CLAUDE.md §3）。

**毎日 06:00 JST に GitHub Actions が前日ぶんを作る**（`.github/workflows/build-features.yml`、
W4 プラン §6.8 の PR J）。Vercel Cron ではないのは、**推論と同じサービスに 3 分・1.3 GB の
バッチを混ぜない**ためである（W4-27）。`--upload` を付けたときだけ `job_runs` に記録し、
`monitored_jobs` が止まりを見張る（0040）。

使い方（環境変数は `.env` から読み込んでから）:

    set -a; . ./.env; set +a
    cd apps/ml
    ./.venv/bin/python -m bikechance_ml.jobs.build_features \\
        --date 2026-09-07 --cache .cache/parquet --out /tmp/features.parquet

`--date` は **JST の暦日**。**省略すると「昨日（JST）」**になる。入力の Parquet は UTC の
時間帯なので、1 日ぶんを作るには前後にまたがる時間帯を読む（`features/grid.py`）。
`--upload` を付けると `features/date=YYYY-MM-DD/part.parquet` に置く。

**要求した時間帯が Storage に無ければ、その区間は欠損として扱われる**（補間しない）。
欠損の件数は `excluded` に出るので、まずそこを見る。
"""

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final, Protocol

import pyarrow as pa
import pyarrow.parquet as pq

from bikechance_ml.config import read_storage_config
from bikechance_ml.features import build, neighbors, static, weather
from bikechance_ml.features.constants import LOOKAHEAD_HOURS, LOOKBACK_HOURS
from bikechance_ml.features.grid import (
    features_path,
    jst_yesterday,
    parquet_hours,
    reference_path,
)
from bikechance_ml.features.reference import SystemReference
from bikechance_ml.features.reference_snapshot import (
    NEIGHBORS_NAME,
    STATIONS_NAME,
    capacity_estimates,
    to_reference,
)
from bikechance_ml.io.supabase import PARQUET_BUCKET, open_storage
from bikechance_ml.jobs import recording
from bikechance_ml.jobs.snapshot_table import COMPRESSION, has_current_schema, parquet_path
from bikechance_ml.jobs.snapshot_table import SCHEMA as SNAPSHOT_SCHEMA
from bikechance_ml.jobs.snapshot_table import read_table as read_snapshot_table

#: `job_runs` と `monitored_jobs` に載る名前。**毎日 1 回**（06:00 JST、GitHub Actions）。
JOB_NAME: Final[str] = "build_features"

#: 対象のシステム。**並び順が台帳の位置を決める**ので、固定する（`analysis/eda_01.py` と同じ）。
SYSTEM_IDS: Final[tuple[str, ...]] = ("hellocycling", "docomo-cycle")

#: 参照スナップショットの `capacity_est`（`(system_id, station_id)` → 台数）。
type Estimates = Mapping[tuple[str, str], int]


class MissingReferenceError(RuntimeError):
    """参照スナップショットが無い。**DB の「いまの値」で代用しない。**

    代用すると、同じ日を作り直したときに値が変わる状態に戻る（W3 プラン §13.2）。
    黙って別のものを読むくらいなら止めるほうがよい。
    """


class ReadsStorage(Protocol):
    """参照スナップショットを読むのに要る口だけ。**学習も推論も同じ関数を通す。**"""

    def download(self, bucket: str, path: str) -> bytes | None: ...


class FeaturesPort(ReadsStorage, Protocol):
    """このジョブが使う入出力ぜんぶ。`io/supabase.py` の実装が構造的に満たす。

    絞っておくと、上位のロジックが httpx を知らずに済み、検査で分岐を全部通せる
    （`jobs/compact.py` の `CompactPort` と同じ方針）。
    """

    def list_holidays(self) -> tuple[date, ...]: ...

    def list_weather(self, start: datetime, end: datetime) -> tuple[weather.WeatherRow, ...]: ...

    def upload_parquet(self, path: str, body: bytes) -> None: ...

    def job_started(self, job_name: str) -> int: ...

    def job_finished(self, run_id: int, status: str, detail: Mapping[str, object]) -> None: ...


def read_reference(
    source: ReadsStorage, day: date
) -> tuple[tuple[SystemReference, ...], Estimates]:
    """**基準時刻の前日**の参照スナップショットを読む（W3 プラン §14.3）。

    学習も推論も同じ規則で「前日の版」を読む。**「学習は前日、推論は最新」にすると、
    そこが新しい train/serve skew になる。**

    以前は `stations` / `station_attributes` / `station_neighbors` を DB から直に
    読んでいた。それだと過去の日を作り直すたびに値が変わる（§13.2）。
    """
    return read_reference_on(source, day - timedelta(days=1))


def read_reference_on(
    source: ReadsStorage, source_day: date
) -> tuple[tuple[SystemReference, ...], Estimates]:
    """**その日付の版**をそのまま読む。無ければ `MissingReferenceError`。"""
    tables = {}
    for name in (STATIONS_NAME, NEIGHBORS_NAME):
        path = reference_path(source_day, name)
        body = source.download(PARQUET_BUCKET, path)
        if body is None:
            raise MissingReferenceError(
                f"参照スナップショットが無い: {path}"
                "（`python -m bikechance_ml.jobs.build_reference --date"
                f" {source_day:%Y-%m-%d} --upload` を先に走らせる）"
            )
        tables[name] = pq.read_table(pa.BufferReader(body))
    found = {
        one.system_id: one for one in to_reference(tables[STATIONS_NAME], tables[NEIGHBORS_NAME])
    }
    missing = tuple(one for one in SYSTEM_IDS if one not in found)
    if missing:
        raise MissingReferenceError(f"参照スナップショットにシステムが無い: {missing}")
    # **`SYSTEM_IDS` の順に並べ直す。** `to_reference` は `system_id` の昇順で返すが、
    # 台帳の位置（＝出力の行順）はこの並びが決める。順を変えると行順が黙って入れ替わる
    return tuple(found[system_id] for system_id in SYSTEM_IDS), capacity_estimates(
        tables[STATIONS_NAME]
    )


def load_snapshots(
    source: ReadsStorage, day: date, cache: Path | None, hours: Sequence[datetime] | None = None
) -> tuple[pa.Table, tuple[str, ...]]:
    """全システムの Parquet を読んで 1 つの表にする。**無い時間帯は記録して進む。**

    `hours` を渡すとその時間帯だけを読む。**プロファイル（`jobs/build_profiles.py`）は
    ラベルも同時刻履歴も要らないので、学習の 25 時間ではなく 2 時間で足りる**
    （W5 プラン §6.2）。既定は学習の窓。
    """
    wanted = parquet_hours(day, LOOKBACK_HOURS, LOOKAHEAD_HOURS) if hours is None else hours
    tables: list[pa.Table] = []
    missing: list[str] = []
    for system_id in SYSTEM_IDS:
        for hour in wanted:
            body = _one_hour(source, system_id, hour, cache)
            if body is None:
                missing.append(f"{system_id} {hour:%Y-%m-%dT%H}Z")
                continue
            tables.append(read_snapshot_table(body, f"{system_id} {hour:%Y-%m-%dT%H}Z"))
    table = pa.concat_tables(tables) if tables else SNAPSHOT_SCHEMA.empty_table()
    return table, tuple(missing)


def _one_hour(
    source: ReadsStorage, system_id: str, hour: datetime, cache: Path | None
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
    estimates: Estimates,
    holidays: frozenset[date],
    table: pa.Table,
    forecast: weather.Weather,
) -> build.DayInputs:
    """参照データと Parquet を、組み立ての入力に直す。"""
    facts = static.to_facts(systems, estimates)
    links = neighbors.to_links(systems, facts.station_keys())
    return build.DayInputs(
        day=day,
        reference=build.Reference(facts=facts, links=links, holidays=holidays),
        table=table,
        weather=forecast,
    )


def read_weather(source: FeaturesPort, day: date) -> weather.Weather:
    """その日の基準時刻で使える予報を読む（W4 プラン §6.4）。

    **2026-09-07 15:17 UTC より前の日は 1 件も返らない。** 天気アーカイブが
    そこから始まっているので、それより前のサンプルは天気の 4 列が NULL になる（§5.4）。
    """
    start, end = weather.training_window(day)
    return weather.to_weather(source.list_weather(start, end))


def to_parquet_bytes(table: pa.Table) -> bytes:
    """表を Parquet のバイト列にする。**同じ表からは同じバイト列が出る。**"""
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression=COMPRESSION)
    return bytes(sink.getvalue())


@dataclass(frozen=True)
class Made:
    """作った 1 日ぶんと、その内訳。"""

    body: bytes
    summary: dict[str, object]


def build_one_day(source: FeaturesPort, day: date, cache: Path | None = None) -> Made:
    """1 日ぶんを作る。**置かない**（副作用は呼ぶ側が決める）。"""
    systems, estimates = read_reference(source, day)
    holidays = frozenset(source.list_holidays())
    table, missing = load_snapshots(source, day, cache)
    forecast = read_weather(source, day)
    built = build.build_day(to_inputs(day, systems, estimates, holidays, table, forecast))
    body = to_parquet_bytes(built.table)
    return Made(
        body=body,
        summary={**built.stats.as_dict(), "bytes": len(body), "missing_hours": list(missing)},
    )


def build_and_upload(source: FeaturesPort, day: date, cache: Path | None = None) -> Made:
    """作って置き、`job_runs` に記録する（W4 プラン §6.8 の PR J）。

    **同じパスに上書きする**ので、何度走らせても結果は変わらない。**冪等でないと、
    GitHub Actions の二重起動や手動の再実行が怖くて使えない。**

    **記録するのは「毎日回っていること」を見張れるようにするため。** `job_runs` に
    書かないと `check_jobs_missing` から見えず、止まっても誰も気づかない——そして
    **止まったことに 30 日気づかないと、天気が保持期間で消えて作り直せなくなる**
    （`weather_hourly` は 30 日保持。W4 プラン §8.5.2）。

    **失敗も記録してから投げ直す**（`build_reference` と同じ）。記録が無いと
    「動いていない」ことしか分からず、「動いたが失敗した」と区別できない。詰めるのは
    **例外の種類だけ**にする（`jobs/recording.py`）。
    """
    run_id = recording.started_quietly(source, JOB_NAME)
    try:
        made = build_one_day(source, day, cache)
        source.upload_parquet(features_path(day), made.body)
    except Exception as cause:
        recording.record_quietly(
            source, run_id, "failed", {"date": day.isoformat(), "error": type(cause).__name__}
        )
        raise
    recording.record_quietly(source, run_id, "ok", {"ok": True, **made.summary})
    return made


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="学習サンプルを 1 日ぶん作る")
    parser.add_argument("--date", default=None, help="JST の暦日（既定は昨日）")
    parser.add_argument("--cache", default=None, help="Parquet を置く場所（再実行が速くなる）")
    parser.add_argument("--out", default=None, help="書き出し先のファイル")
    parser.add_argument("--upload", action="store_true", help="Storage に置き、job_runs に記録する")
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    options = _arguments(argv)
    # **既定は前日ぶん。** 06:00 JST に走らせるので、`LOOKAHEAD_HOURS = 3` の先まで
    # 観測が揃っている（前日 21:00 JST の基準時刻が要求する最も先の観測が当日 03:00 JST）
    day = date.fromisoformat(options.date) if options.date else jst_yesterday(datetime.now(UTC))
    cache = Path(options.cache) if options.cache else None

    with open_storage(read_storage_config()) as source:
        # **記録するのは置いたときだけ。** 手元の試し打ちで `job_runs` を汚さない
        made = (
            build_and_upload(source, day, cache)
            if options.upload
            else build_one_day(source, day, cache)
        )

    if options.out:
        Path(options.out).write_bytes(made.body)
    print(json.dumps({**made.summary, "uploaded": options.upload}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
