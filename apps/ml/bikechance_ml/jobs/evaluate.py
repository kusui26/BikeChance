"""前日ぶんの実運用 Brier（W5 プラン §6.12 の PR L、開発プラン §8.5）。

**配った確率を、実際に起きたことと突き合わせる。** 読むのは `forecast-log/`（1 サイクル
1 ファイル）と `gbfs-parquet` の実測で、書くのは `model_daily_metrics`（0050）である。

**GitHub Actions で走る**（`.github/workflows/evaluate-daily.yml`）。Vercel Cron では
ない——**実測の山が 2.26 GB** で、Vercel の枠（300 秒・2 GB）に入らない（§12 の 171）。
W4-27 が `build_features` について決めたのと同じ規律である：**5 分毎の推論と同じ
サービスに重いバッチを混ぜない。**

**アドバイザリロックは取らない。** 二重起動は同じ日を 2 度測るだけで、主キーで衝突
させるので行は増えない（完了条件 2）。ワークフロー側の `concurrency` でも重ならない。

**ログの無い日は `skipped`。** 埋めない（CLAUDE.md §6「収集の欠損は補間しない」）。
予測ログは 12 か月残るので、後から `--date` で測り直せる（`workflow_dispatch` の入力）。

**読み取りは並行に行う**（`io/fanout.py`）。1 日ぶんは 2 システムで**予測ログ 576 件・
84 MB**、加えて実測の Parquet が 58 時間ぶんある。直列に積むと往復の待ちだけで
`maxDuration` の 240 秒を超える——**2026-09-17 の初回がそれで殺された**（§12 の 169）。
書き込み（`upsert`）は並行にしない：1 日に数回しかなく、速くしても効かない。

**ここは副作用の置き場所。** 突き合わせの規則は `eval/served.py` にあり、そこは
`features/labels.py`・`features/exclude.py`・`eval/metrics.py` をそのまま呼ぶ。

**メモリは 1 日ぶんで 1 GB 以内**（開発プラン §8.5）。いちばん重いのは
`(水平, ポート, サイクル)` の確率（HELLO で 85 MB × 2 ターゲット）と実測の観測で、
**測るのは水平ごと**なので作業用の配列は `(ポート, サイクル)` に収まる。
"""

import argparse
import io
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Final, Protocol

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from bikechance_ml.config import read_storage_config
from bikechance_ml.eval import served
from bikechance_ml.eval.dataset import TARGETS
from bikechance_ml.features.arrays import Int16
from bikechance_ml.features.constants import (
    GRID_MINUTES,
    GRID_POINTS_PER_DAY,
    HORIZONS_MIN,
    LOOKAHEAD_HOURS,
)
from bikechance_ml.features.grid import (
    Grid,
    build_grid,
    day_start,
    jst_yesterday,
    parquet_hours,
    to_epoch_ms,
)
from bikechance_ml.io import fanout
from bikechance_ml.io.supabase import open_storage
from bikechance_ml.jobs import recording
from bikechance_ml.jobs.build_features import SYSTEM_IDS
from bikechance_ml.jobs.snapshot_table import parquet_path
from bikechance_ml.jobs.snapshot_table import read_table as read_snapshot_table

#: `job_runs` と `monitored_jobs` に載る名前（0050）。**毎日 1 回**（06:40 JST）。
JOB_NAME: Final[str] = "evaluate_daily"

#: 格子の前の余白（時間）。**as-of は 600 秒しかさかのぼらない**ので 1 時間で足りる。
#: 学習（25 時間）と違い、ラグも同時刻履歴も引かない。
LOOKBACK_HOURS: Final[int] = 1

#: 予測ログを探す UTC の時間帯を、JST の暦日の前後にどれだけ広げるか。
#:
#: **パスは `base_observed_at` で決まるが、日を切るのは `generated_at`** である。2 つは
#: 最大 5 分ずれる（前者はそのとき最後に取れた観測、後者は 5 分格子に落とした時刻）。
#: 広げても拾いすぎない——`generated_at` で必ず絞り直す。
LOG_MARGIN_HOURS: Final[int] = 1

#: 1 回の upsert で送る行数。
UPSERT_BATCH: Final[int] = 500

#: Storage の一覧が返す上限。**達したら切り捨てられた可能性がある**ので止める。
#: 1 つの時間帯には 12 サイクル × 版の数しか置かれない。
LIST_LIMIT: Final[int] = 1000

#: 同時に開く読み取りの数（`io/fanout.py`）。
#:
#: **待ちが支配的なので、重ねるだけで効く。** 1 システム 288 件の取得が 18 巡になる。
#: **どこまで開いてよいかは `io/` 側が知っている**——接続プールの上限はそちらの
#: 持ちもので、16 はそれより十分に小さい（CLAUDE.md §3「外向きの通信は `io/` に
#: 集める」。ここに通信の相手の話を書かないのはそのため）。
#:
#: **速さより「確実に 240 秒に収まること」**で選んだ。ここを上げても縮むのは数秒で、
#: 失うのは Storage への行儀である。
IO_WORKERS: Final[int] = 16

#: `{base_epoch_s}_{model_version}.parquet` の名前。
_LOG_NAME: Final[re.Pattern[str]] = re.compile(r"\A\d+_.+\.parquet\Z")

_MS_PER_CYCLE: Final[int] = GRID_MINUTES * 60 * 1000
_NS_PER_MS: Final[int] = 1_000_000


class TruncatedListingError(RuntimeError):
    """Storage の一覧が上限に達した。**足りない一覧の上で測らない。**"""


class MissingObservationsError(RuntimeError):
    """予測ログは在るのに実測が 1 時間ぶんも無い。**0 行の成績を書かない。**"""


class RaggedProbabilitiesError(ValueError):
    """確率の配列の長さが水平の数と揃っていない。**形を直して読まない。**"""


class EvaluatePort(Protocol):
    """このジョブが使う入出力。`io/supabase.py` の実装が構造的に満たす。"""

    def list_forecast_log(self, prefix: str, limit: int) -> tuple[str, ...]: ...

    def download_forecast_log(self, path: str) -> bytes | None: ...

    def download_parquet(self, path: str) -> bytes | None: ...

    def upsert_model_daily_metrics(self, rows: Sequence[Mapping[str, object]]) -> int: ...

    def job_started(self, job_name: str) -> int: ...

    def job_finished(self, run_id: int, status: str, detail: Mapping[str, object]) -> None: ...


@dataclass(frozen=True)
class LogFile:
    """読んだ 1 サイクルぶん。**覚え書きの `generated_at` が日を決める。**"""

    path: str
    model_version: str
    generated_at: datetime
    table: pa.Table


@dataclass(frozen=True)
class SystemOutcome:
    """1 システムぶんの結果。**1 つ落ちても他は続ける。**"""

    system_id: str
    ok: bool
    n_files: int
    n_cycles: int
    n_stations: int
    n_kept: int
    n_rows: int
    model_versions: tuple[str, ...] = ()
    dropped: Mapping[str, int] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "system_id": self.system_id,
            "ok": self.ok,
            "n_files": self.n_files,
            "n_cycles": self.n_cycles,
            "n_stations": self.n_stations,
            "n_kept": self.n_kept,
            "n_rows": self.n_rows,
            "model_versions": list(self.model_versions),
            "dropped": dict(sorted(self.dropped.items())),
            **({"error": self.error} if self.error else {}),
        }


@dataclass(frozen=True)
class SystemResult:
    """報告する部分と、書き込む行。**行は要約に載せない**（280 行 × 版）。"""

    outcome: SystemOutcome
    rows: tuple[served.MetricRow, ...] = ()


@dataclass(frozen=True)
class EvaluateSummary:
    ok: bool
    status: str
    metric_date: str
    n_written: int
    duration_ms: int
    systems: tuple[SystemOutcome, ...]
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "status": self.status,
            "metric_date": self.metric_date,
            "n_written": self.n_written,
            "duration_ms": self.duration_ms,
            "systems": [one.as_dict() for one in self.systems],
            **({"error": self.error} if self.error else {}),
        }


# ── 予測ログを探して読む ──────────────────────────────────────
def log_hours(day: date) -> tuple[datetime, ...]:
    """予測ログを探す **UTC の正時**。JST の暦日の前後に余白を足す。"""
    first = _floor_hour(day_start(day)) - timedelta(hours=LOG_MARGIN_HOURS)
    last = _floor_hour(day_start(day) + timedelta(hours=24)) + timedelta(hours=LOG_MARGIN_HOURS)
    count = int((last - first).total_seconds()) // 3600
    return tuple(first + timedelta(hours=offset) for offset in range(count))


def log_prefix(system_id: str, hour: datetime) -> str:
    """一覧を取る接頭辞。`jobs/forecast_log.py` の `log_path` と同じ組み立て。"""
    at = hour.astimezone(UTC)
    return f"{system_id}/date={at:%Y-%m-%d}/hour={at:%H}/"


def names_at(port: EvaluatePort, system_id: str, hour: datetime) -> tuple[str, ...]:
    """1 時間帯ぶんの一覧。**上限に達したら止める。**"""
    prefix = log_prefix(system_id, hour)
    names = port.list_forecast_log(prefix, LIST_LIMIT)
    if len(names) >= LIST_LIMIT:
        raise TruncatedListingError(f"{prefix} の一覧が上限 {LIST_LIMIT} に達しました")
    return tuple(f"{prefix}{name}" for name in names if _LOG_NAME.match(name))


def list_logs(port: EvaluatePort, system_id: str, day: date) -> tuple[str, ...]:
    """その日の候補になるファイルの一覧。**26 時間ぶんを並行に取る。**

    **並べ替えて返すのは元のまま。** 一覧の順を Storage の都合に委ねない。
    """
    found = fanout.gather(
        lambda hour: names_at(port, system_id, hour), log_hours(day), workers=IO_WORKERS
    )
    return tuple(sorted(name for names in found for name in names))


def read_log(body: bytes, path: str) -> LogFile:
    """1 サイクルぶんを読む。**覚え書きから `generated_at` を拾う。**

    `concat_tables` すると覚え書きは消えるので、**1 ファイルずつ読むここでしか
    拾えない**（W4 プラン §6.8 は「結合に使わないものだけを覚え書きに」と決めたが、
    **水平の起点は結合に使う**）。
    """
    table = pq.read_table(io.BytesIO(body))
    if table.num_rows == 0:
        raise ValueError(f"{path} に行がありません")
    metadata = {
        key.decode(): value.decode() for key, value in (table.schema.metadata or {}).items()
    }
    if "generated_at" not in metadata:
        raise KeyError(f"{path} の覚え書きに generated_at がありません")
    served.check_horizons(table.column("horizons_min")[0].as_py(), path)
    return LogFile(
        path=path,
        model_version=str(table.column("model_version")[0].as_py()),
        generated_at=datetime.fromisoformat(metadata["generated_at"]).astimezone(UTC),
        table=table,
    )


def within(day: date, at: datetime) -> bool:
    """その時刻が JST の暦日 `day` の中か。"""
    start = day_start(day)
    return start <= at < start + timedelta(hours=24)


def fetch_log(port: EvaluatePort, path: str) -> LogFile | None:
    """1 サイクルぶんを取って読む。**無ければ None**（消えていても止めない）。"""
    body = port.download_forecast_log(path)
    return None if body is None else read_log(body, path)


def read_logs(port: EvaluatePort, system_id: str, day: date) -> tuple[LogFile, ...]:
    """その日の予測ログ。**`generated_at` で絞り直す**（パスは基準観測の時刻）。

    **取得と Parquet の展開をまとめて並行にする。** 展開は `pyarrow` の中で GIL を
    放すので、往復の待ちと重なる。**絞り込みは戻ってから**——`within` を並行の中に
    入れると、読んだ数と残った数の差が追えなくなる。
    """
    paths = list_logs(port, system_id, day)
    found = fanout.gather(lambda path: fetch_log(port, path), paths, workers=IO_WORKERS)
    return tuple(one for one in found if one is not None and within(day, one.generated_at))


# ── 「配った確率」を格子に載せる ──────────────────────────────
def station_keys(logs: Sequence[LogFile]) -> tuple[str, ...]:
    """ポートの並び。**ログに現れたポートだけ**（＝ その日に確率を配ったポート）。

    実測にしか無いポートは**配っていない**ので測る対象ではない。逆に、ログに在って
    実測に無いポートは as-of が引けず `no_asof_at_t` で落ちる——**落ちたことが内訳に
    出る**のが大事で、黙って無かったことにしない。
    """
    names: set[str] = set()
    for one in logs:
        names.update(one.table.column("station_id").to_pylist())
    return tuple(sorted(names))


def cycle_of(day: date, generated_at: datetime) -> int:
    """`generated_at` が当日の何番目の基準時刻か。**格子の上に無ければ止める。**"""
    offset = to_epoch_ms(generated_at) - to_epoch_ms(day_start(day))
    if offset % _MS_PER_CYCLE != 0:
        raise ValueError(f"generated_at が {GRID_MINUTES} 分格子の上にありません: {generated_at}")
    return offset // _MS_PER_CYCLE


def probabilities(table: pa.Table, target: str) -> Int16:
    """`p_bike_x1000` / `p_dock_x1000` を `(行, 水平)` の行列にする。"""
    flat = pc.list_flatten(table.column(f"p_{target}_x1000"))
    values = np.asarray(flat.to_numpy(zero_copy_only=False), dtype=np.int16)
    if values.size != table.num_rows * len(HORIZONS_MIN):
        raise RaggedProbabilitiesError(f"p_{target}_x1000 の長さが {values.size} で揃っていません")
    return values.reshape(table.num_rows, len(HORIZONS_MIN))


def to_served(
    system_id: str, model_version: str, day: date, stations: Sequence[str], logs: Sequence[LogFile]
) -> served.Served:
    """1 版ぶんのログを `(水平, ポート, サイクル)` の行列に載せる。

    **サイクルの軸は当日の 288 点そのもの。** 欠けたサイクルは列がまるごと
    「居なかった」になる（`present` が偽）ので、数え間違えようがない。
    """
    index = {name: position for position, name in enumerate(stations)}
    shape = (len(HORIZONS_MIN), len(stations), GRID_POINTS_PER_DAY)
    values = {target.name: np.zeros(shape, dtype=np.int16) for target in TARGETS}
    present = np.zeros((len(stations), GRID_POINTS_PER_DAY), dtype=np.bool_)
    for one in logs:
        cycle = cycle_of(day, one.generated_at)
        rows = np.fromiter(
            (index[name] for name in one.table.column("station_id").to_pylist()),
            dtype=np.int64,
            count=one.table.num_rows,
        )
        present[rows, cycle] = True
        for target in TARGETS:
            values[target.name][:, rows, cycle] = probabilities(one.table, target.name).T
    return served.Served(
        system_id=system_id,
        model_version=model_version,
        day=day,
        stations=tuple(stations),
        present=present,
        p_x1000=values,
        n_cycles=len(logs),
    )


# ── 実測を読む ────────────────────────────────────────────────
def fetch_hour(port: EvaluatePort, system_id: str, hour: datetime) -> pa.Table | None:
    """1 時間帯ぶんの実測。**無ければ None**（畳んでいないだけ）。"""
    path = parquet_path(system_id, hour)
    body = port.download_parquet(path)
    return None if body is None else read_snapshot_table(body, path)


def read_observations(port: EvaluatePort, system_id: str, day: date) -> pa.Table:
    """当日ぶんと前後の余白の実測。**無い時間帯は飛ばす**（畳んでいないだけ）。

    **順を保ったまま並行に取る。** 戻る順が走るたびに変われば `concat_tables` の
    行の並びも変わる。値そのものは `served.to_truth` が時刻で引き直すので変わらないが、
    **同じ入力から同じ表**が出ることに頼れるほうがよい。
    """
    hours = parquet_hours(day, LOOKBACK_HOURS, LOOKAHEAD_HOURS)
    read = fanout.gather(lambda hour: fetch_hour(port, system_id, hour), hours, workers=IO_WORKERS)
    tables = [one for one in read if one is not None]
    if not tables:
        raise MissingObservationsError(f"{system_id} の {day} に実測の Parquet がありません")
    return pa.concat_tables(tables)


# ── 1 システムぶん ────────────────────────────────────────────
def evaluate_system(port: EvaluatePort, system_id: str, day: date, grid: Grid) -> SystemResult:
    """1 システムを測る。**版ごとに分けて測る**（`active` と `shadow` が同じ日に並ぶ）。"""
    logs = read_logs(port, system_id, day)
    if not logs:
        return SystemResult(SystemOutcome(system_id, True, 0, 0, 0, 0, 0))
    observations = read_observations(port, system_id, day)
    stations = station_keys(logs)
    truth = served.to_truth(observations, [(system_id, name) for name in stations], grid)
    rows: list[served.MetricRow] = []
    dropped: dict[str, int] = {}
    kept = 0
    for model_version in sorted({one.model_version for one in logs}):
        part = [one for one in logs if one.model_version == model_version]
        one = to_served(system_id, model_version, day, stations, part)
        outcome = served.evaluate(one, truth, grid)
        rows.extend(outcome.rows)
        kept += outcome.n_kept
        for name, number in outcome.dropped.items():
            dropped[name] = dropped.get(name, 0) + number
    return SystemResult(
        SystemOutcome(
            system_id=system_id,
            ok=True,
            n_files=len(logs),
            n_cycles=len({one.generated_at for one in logs}),
            n_stations=len(stations),
            n_kept=kept,
            n_rows=len(rows),
            model_versions=tuple(sorted({one.model_version for one in logs})),
            dropped=dropped,
        ),
        tuple(rows),
    )


def _failed(system_id: str, cause: Exception) -> SystemResult:
    """**例外の種類だけ**を残す。文言は接続先を抱えうる（CLAUDE.md §5）。"""
    return SystemResult(SystemOutcome(system_id, False, 0, 0, 0, 0, 0, error=type(cause).__name__))


def _safe_system(port: EvaluatePort, system_id: str, day: date, grid: Grid) -> SystemResult:
    try:
        return evaluate_system(port, system_id, day, grid)
    except Exception as cause:  # 1 システムの失敗で他を止めない
        return _failed(system_id, cause)


def batches(
    rows: Sequence[Mapping[str, object]], size: int
) -> list[Sequence[Mapping[str, object]]]:
    return [rows[start : start + size] for start in range(0, len(rows), size)]


def measure_all(port: EvaluatePort, day: date) -> tuple[SystemResult, ...]:
    """全システムを測る。**1 システムの失敗で他を止めない。**"""
    grid = build_grid(day, LOOKBACK_HOURS, LOOKAHEAD_HOURS)
    return tuple(_safe_system(port, system_id, day, grid) for system_id in SYSTEM_IDS)


def write_all(port: EvaluatePort, results: Sequence[SystemResult]) -> int:
    """測れた行を書く。**刻んで送る**（1 日 1 版あたり 280 行 × システム）。"""
    rows = [one.as_row() for result in results for one in result.rows]
    return sum(port.upsert_model_daily_metrics(chunk) for chunk in batches(rows, UPSERT_BATCH))


def _summarise(
    results: Sequence[SystemResult], written: int, error: str | None, day: date, started: int
) -> EvaluateSummary:
    """要約を作る。**書く道と書かない道で同じ形にする**（読む場所を 2 つにしない）。"""
    return EvaluateSummary(
        ok=error is None and all(result.outcome.ok for result in results),
        status=_status(results, error),
        metric_date=day.isoformat(),
        n_written=written,
        duration_ms=int((time.monotonic_ns() - started) // _NS_PER_MS),
        systems=tuple(result.outcome for result in results),
        error=error,
    )


def measure_only(port: EvaluatePort, day: date) -> EvaluateSummary:
    """測るだけ。**1 行も書かず、`job_runs` にも残さない**（試し打ち用）。

    `build_features` の `--upload` を付けないときと同じ作法である（W4 プラン §6.8）
    ——**手元で確かめたことが本番の記録を汚さない**。
    """
    started = time.monotonic_ns()
    try:
        results, error = measure_all(port, day), None
    except Exception as cause:
        # **例外の種類だけ**を残す。文言は接続先を抱えうる（CLAUDE.md §5）
        results, error = (), type(cause).__name__
    return _summarise(results, 0, error, day, started)


def run_evaluation(port: EvaluatePort, day: date) -> EvaluateSummary:
    """前日ぶんを測って書く。**測れなかった日は `skipped`**（失敗にしない）。

    **全体が落ちても `job_runs` は必ず終わる。** 書き込みで落ちたまま `running` を
    残すと、見張りが「止まった」ではなく「遅い」と読む（`jobs/compact.py` と同じ形）。
    **プロセスごと殺されたときだけは残る**——2026-09-17 と 09-18 がそれで、`running` の
    まま 4 行残った（§12 の 171）。
    """
    started = time.monotonic_ns()
    run_id = recording.started_quietly(port, JOB_NAME)
    try:
        results = measure_all(port, day)
        written, error = write_all(port, results), None
    except Exception as cause:
        results, written, error = (), 0, type(cause).__name__
    summary = _summarise(results, written, error, day, started)
    recording.record_quietly(port, run_id, summary.status, summary.as_dict())
    return summary


def _status(results: Sequence[SystemResult], error: str | None = None) -> str:
    """**ログが 1 つも無ければ `skipped`**（完了条件 5）。"""
    if error is not None or not all(result.outcome.ok for result in results):
        return "failed"
    if all(result.outcome.n_files == 0 for result in results):
        return "skipped"
    return "ok"


def to_detail(summary: EvaluateSummary) -> dict[str, object]:
    """外に出す本文。**記録と同じ形**（見る場所を 2 つにしない）。"""
    return summary.as_dict()


# ── 実行（GitHub Actions から）──────────────────────────────────
def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="配った確率の実運用 Brier を 1 日ぶん測る")
    parser.add_argument("--date", default=None, help="JST の暦日（既定は昨日）")
    parser.add_argument(
        "--write", action="store_true", help="model_daily_metrics に書き、job_runs に記録する"
    )
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    """**`--write` を付けたときだけ書く。** 付けなければ測って表示するだけ。

    返り値は終了コード。**測れなかった日（`skipped`）は 0 で返す**——埋めるものが
    無いだけで、こちらの不調ではない（完了条件 5）。**失敗は 1**で、ワークフローが
    赤くなる。
    """
    options = _arguments(argv)
    day = date.fromisoformat(options.date) if options.date else jst_yesterday(datetime.now(UTC))
    with open_storage(read_storage_config()) as source:
        summary = run_evaluation(source, day) if options.write else measure_only(source, day)
    print(json.dumps({**to_detail(summary), "wrote": options.write}, ensure_ascii=False, indent=2))
    return 0 if summary.ok else 1


def _floor_hour(at: datetime) -> datetime:
    return at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


# **入口はファイルのいちばん最後に置く。** `python -m` はモジュールを `__main__` として
# **上から順に実行する**ので、途中に置くと**まだ定義されていない名前**を掴む
# （2026-09-18 に `_floor_hour` でそうなった。W5 プラン §12 の 172）。
if __name__ == "__main__":
    raise SystemExit(run())
