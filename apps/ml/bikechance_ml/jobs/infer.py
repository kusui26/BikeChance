"""5 分毎の先回り推論（W3 プラン §5.10、開発プラン §8.1・§8.2）。

**この段の目的は精度ではなく配線**である。配るのは B3（ベースラインの混合）で、
LightGBM は W4 以降（W3-18）。**予測テーブルを埋め始め、5 分周期の運転で DB の負荷を
実測する**のがねらい。

手順（CLAUDE.md §3 の Cron ハンドラの定型）：
  `CRON_SECRET` 検証 → 冪等チェック（掴む）→ 処理 → `inference_log` に記録 → 要約 JSON

**アドバイザリロックは取らない。** PostgREST は 1 要求 1 トランザクションで、
セッションのロックは接続プールに返された後の扱いが決まらない（`jobs/compact.py` と
同じ理由。開発プラン §8.2 の記述は PostgREST 経由では成り立たない。W3 プラン §12 の 103）。
代わりに `inference_log` の `(system_id, base_observed_at)` の一意制約で掴む。
**同じ観測時刻に対する 2 度目は何もせずに 200 を返す。**

**基準時刻 `t` は「いま」**（`generated_at`）にする。学習では `t` は 5 分グリッドの点で、
特徴量は `fetched_at <= t` の観測だった。配信では最新の観測がすでに手元にあるので、
`t` を切り下げると観測のほうが新しくなって学習と形が合わない。`t = いま` なら
観測は必ず過去で、しかも学習時（HELLO で 205 秒古い）より新しい。**モデルが訓練より
不利な材料で当てることはない。**
"""

import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Final, Protocol

import numpy as np
import pyarrow as pa

from bikechance_ml.baselines import blend, climatology, conditional
from bikechance_ml.baselines.artifact import Artifact, from_bytes
from bikechance_ml.eval.dataset import TARGETS, Samples, Target
from bikechance_ml.features import build, neighbors, static
from bikechance_ml.features.arrays import Bools, Float64, Int8, Int16
from bikechance_ml.features.calendar import DOW_TYPES
from bikechance_ml.features.constants import (
    GRID_MINUTES,
    HORIZONS_MIN,
    MAX_STALENESS_S,
)
from bikechance_ml.features.grid import JST, from_epoch_ms, jst_date, to_epoch_ms
from bikechance_ml.features.reference import SystemReference
from bikechance_ml.jobs.build_features import (
    SYSTEM_IDS,
    Estimates,
    MissingReferenceError,
    read_reference_on,
)
from bikechance_ml.jobs.snapshot_table import Snapshot, StationRow, station_ids_by_idx
from bikechance_ml.jobs.snapshot_table import to_table as to_snapshot_table

#: 推論が読む観測の範囲（`build.serving_windows` が返す形）。
type Windows = Sequence[tuple[datetime, datetime]]

#: 確率を整数で持つときの倍率（0026 の `p_bike_x1000`）。
PROBABILITY_SCALE: Final[int] = 1000

#: 1 回の往復で送る予測の数。20,745 行を 6 往復に刻む。
UPSERT_BATCH: Final[int] = 4_000

#: 成果物の置き場所（0027 のバケット）。**`gbfs-parquet` には相乗りさせない**：
#: あちらは Parquet の MIME しか許さず、寿命も作り直し方も違う（W3 プラン §12 の 106）。
MODEL_BUCKET: Final[str] = "models"
MODEL_PREFIX: Final[str] = "baseline"

MS_PER_S: Final[int] = 1000
NS_PER_MS: Final[int] = 1_000_000


def model_path(model_version: str) -> str:
    """成果物のパス。**版がそのままファイル名**になる。"""
    return f"{MODEL_PREFIX}/{model_version}.json.gz"


class InferPort(Protocol):
    """入出力の口。テストはここだけを置き換える。"""

    def read_base_observed_at(self, system_id: str) -> datetime | None: ...
    def list_stations(self, system_id: str) -> tuple[StationRow, ...]: ...
    def list_snapshots(
        self, system_id: str, start: datetime, end: datetime
    ) -> tuple[Snapshot, ...]: ...
    def list_holidays(self) -> tuple[date, ...]: ...
    def download(self, bucket: str, path: str) -> bytes | None: ...
    def begin_inference(
        self, system_id: str, base_observed_at: datetime, model_version: str
    ) -> int | None: ...
    def finish_inference(
        self,
        run_id: int,
        status: str,
        n_rows: int,
        duration_ms: int,
        error: str | None = None,
        detail: Mapping[str, object] | None = None,
    ) -> None: ...
    def upsert_forecasts(self, rows: Sequence[Mapping[str, object]]) -> int: ...


def grid_time(now: datetime) -> datetime:
    """基準時刻を **5 分格子に落とす**（`GRID_MINUTES`）。

    モデルは格子の上でしか学習していない。格子から外れた時刻で特徴量を作ると、ラグも
    同時刻履歴も引けない（`Grid.shifted` が止める）。落とすのは最大 5 分ぶんで、
    そのぶん `station_forecasts.generated_at` も過去になる：**水平の起点は
    `generated_at` なので、`/v1` の側と辻褄が合う**（W4 プラン §12 の 114）。
    """
    step = GRID_MINUTES * 60 * MS_PER_S
    stamp = to_epoch_ms(now)
    return from_epoch_ms(stamp - stamp % step)


def read_observations(port: InferPort, system_id: str, windows: Windows) -> pa.Table:
    """1 システムぶんの観測を、**学習と同じ長形式**にして返す。

    配列形式のスナップショットを長形式に開くのは `jobs/snapshot_table.py` で、
    **毎時の Parquet 化と同じ関数**を通る。形が同じだから、この先の規則を学習と
    共有できる（W4 プラン §6.3）。
    """
    station_ids = station_ids_by_idx(port.list_stations(system_id))
    snapshots = [
        one for start, end in windows for one in port.list_snapshots(system_id, start, end)
    ]
    return to_snapshot_table(system_id, station_ids, snapshots)


def read_features(
    port: InferPort, system_id: str, at: datetime, holidays: frozenset[date]
) -> tuple[date, build.Ready]:
    """推論 1 回ぶんの特徴量を作る。**学習と同じ `features/` を通る。**

    読むのは 3 つ。
      1. **前日の参照スナップショット**（学習と同じ規則。W3 プラン §14.3）
      2. **自系統の 2 つの窓**（`build.serving_windows`）
      3. **他系統の直近**（近傍の集計に要るのは「いまの状態」だけ。§6.3）

    返すのは**使った参照スナップショットの日付**と組み立ての結果。日付を返すのは、
    00:00〜05:00 JST に 1 つ古い版を使うことがあるため（`read_reference_available`）。
    """
    reference_day, systems, estimates = read_reference_available(port, jst_date(at))
    facts = static.to_facts(systems, estimates)
    links = neighbors.to_links(systems, facts.station_keys())
    windows = build.serving_windows(at)
    tables = [read_observations(port, system_id, windows)]
    tables.extend(
        read_observations(port, other, (_neighbour_window(at),))
        for other in SYSTEM_IDS
        if other != system_id
    )
    ready = build.build_now(
        build.NowInputs(
            at=at,
            system_id=system_id,
            reference=build.Reference(facts=facts, links=links, holidays=holidays),
            table=pa.concat_tables(tables),
        )
    )
    return reference_day, ready


#: 参照スナップショットを何日前までさかのぼって探すか。
#: **前日の版は 05:00 JST に書かれる**ので、00:00〜05:00 の推論はまだ読めない。
REFERENCE_FALLBACK_DAYS: Final[int] = 2


def read_reference_available(
    port: InferPort, day: date
) -> tuple[date, tuple[SystemReference, ...], Estimates]:
    """読める中でいちばん新しい参照スナップショットを返す（**どの日かも返す**）。

    規則は学習と同じ「**前日の版**」だが、その版が置かれるのは **05:00 JST**
    （`/ml/reference`）である。つまり **00:00〜05:00 JST の推論は前日の版をまだ読めない**
    ので、そのあいだは 1 つ古い版を使う（W4 プラン §12 の 118）。

    **黙って古い版を使わない。** 使った日付は `inference_log.detail.reference_date` に
    残るので、あとから「どの版で出した予測か」が分かる。
    """
    for back in range(1, REFERENCE_FALLBACK_DAYS + 1):
        source_day = day - timedelta(days=back)
        try:
            systems, estimates = read_reference_on(port, source_day)
        except MissingReferenceError:
            continue
        return source_day, systems, estimates
    raise MissingReferenceError(
        f"参照スナップショットが {REFERENCE_FALLBACK_DAYS} 日ぶん見つからない（{day} 基準）"
    )


def _neighbour_window(at: datetime) -> tuple[datetime, datetime]:
    """他系統から読む範囲。**基準時刻の as-of が 1 つ取れれば足りる。**

    `MAX_STALENESS_S`（10 分）より古い観測は除外側で落ちるので、それより手前は
    読んでも使われない。格子 1 点ぶんの余裕を足す。
    """
    span = timedelta(seconds=MAX_STALENESS_S) + timedelta(minutes=GRID_MINUTES)
    return (at - span, at + timedelta(minutes=GRID_MINUTES))


@dataclass(frozen=True)
class Forecast:
    """1 ポートぶんの予測。**水平は配列**（0026 の表と同じ形）。"""

    system_id: str
    station_id: str
    p_bike_x1000: tuple[int, ...]
    p_dock_x1000: tuple[int, ...]
    confidence: int


@dataclass(frozen=True)
class InferSummary:
    """1 回の推論の要約。`inference_log` と応答の両方に使う。"""

    system_id: str
    status: str
    base_observed_at: datetime | None
    model_version: str
    n_stations: int
    n_skipped: int
    #: 成果物に無かったポートの数。**気候値が引けず B1 だけになる**（§12 の 110）
    n_unknown_ports: int
    n_rows: int
    duration_ms: int
    #: このプロセスが使った CPU 時間。**費用は Active CPU で決まる**（開発プラン §4.5a）
    cpu_ms: int
    #: 特徴量を作るのに掛かった時間。**推論の所要の大半はここ**（W4 プラン §6.3）
    features_ms: int = 0
    #: 除外の内訳（理由 → ポート数）。**なぜ出せなかったかが分かる**
    excluded: Mapping[str, int] = field(default_factory=dict)
    #: 使った参照スナップショットの日付。**00:00〜05:00 JST は 1 つ古い版になる**
    reference_date: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "skipped")


# ── 予測（純粋）────────────────────────────────────────────────
#: 成果物に無いポート。**気候値が引けず B1 だけになる**（W3 プラン §12 の 110）。
NO_PORT: Final[int] = -1


def to_samples(artifact: Artifact, system_id: str, at: datetime, table: pa.Table) -> Samples:
    """`build_now` の出力を、ベースラインが読む形にする。**列を引き写すだけ。**

    B0〜B3 が使うのは 6 つ（システム・ポート・日・水平・日内分・目標の曜日種別）と
    台数だけである。**57 列を作ってから 6 つを引く**のは一見無駄だが、LightGBM v0
    （PR E）を入れるときに経路を変えずに済む（W4 プラン §6.3）。**除外も暦も
    学習と同じ関数が決めている**ので、ここに判断は残っていない。

    **ラベルは持たない**（`predict` は読まない）。長さ 0 の配列を入れておく。
    """
    ports = {name: index for index, name in enumerate(artifact.ports)}
    station_ids = table.column("station_id").to_pylist()
    rows = table.num_rows
    return Samples(
        systems=artifact.systems,
        n_ports=len(artifact.ports),
        system=np.full(rows, artifact.systems.index(system_id), dtype=np.int8),
        port=np.fromiter(
            (ports.get(f"{system_id}/{one}", NO_PORT) for one in station_ids),
            dtype=np.int32,
            count=rows,
        ),
        day=np.full(rows, at.astimezone(JST).date().toordinal(), dtype=np.int32),
        h_min=_int16(table, "h_min"),
        minute_of_day=_int16(table, "minute_of_day"),
        dow_type=_dow_type_index(table),
        weight=np.ones(rows, dtype=np.float32),
        labels={target.label: np.zeros(0, dtype=np.int8) for target in TARGETS},
        counts={"bikes": _int16(table, "bikes"), "docks": _int16(table, "docks")},
    )


def _int16(table: pa.Table, name: str) -> Int16:
    column = table.column(name).combine_chunks()
    return np.asarray(column.to_numpy(zero_copy_only=False), dtype=np.int16)


def _dow_type_index(table: pa.Table) -> Int8:
    """目標時刻の曜日種別を、成果物と同じ並びの番号にする。"""
    order = tuple(sorted(DOW_TYPES))
    values = table.column("target_dow_type").to_pylist()
    return np.fromiter((order.index(one) for one in values), dtype=np.int8, count=len(values))


class RowCountError(ValueError):
    """行数が `ポート × 水平` になっていない。**畳み直せないので止める。**"""


def predict(
    artifact: Artifact, system_id: str, at: datetime, table: pa.Table, stale: bool
) -> tuple[Forecast, ...]:
    """予測を作る。**出せないポートは `build_now` が既に落としている。**"""
    n_horizons = len(HORIZONS_MIN)
    if table.num_rows == 0:
        return ()
    if table.num_rows % n_horizons != 0:
        raise RowCountError(f"行数 {table.num_rows} が水平 {n_horizons} で割り切れない")
    samples = to_samples(artifact, system_id, at, table)
    columns = {target.name: _probabilities(artifact, samples, target) for target in TARGETS}
    bike = to_x1000(columns["bike"][0]).reshape(-1, n_horizons)
    dock = to_x1000(columns["dock"][0]).reshape(-1, n_horizons)
    used = (columns["bike"][1] & columns["dock"][1]).reshape(-1, n_horizons)
    # **並びは `(ポート, 水平)`。** `build_now` が station_id, h_min の昇順に並べている
    station_ids = table.column("station_id").to_pylist()[::n_horizons]
    return tuple(
        Forecast(
            system_id=system_id,
            station_id=station_id,
            p_bike_x1000=tuple(int(one) for one in bike[index]),
            p_dock_x1000=tuple(int(one) for one in dock[index]),
            confidence=confidence_of(stale=stale, climatology_horizons=int(used[index].sum())),
        )
        for index, station_id in enumerate(station_ids)
    )


def unknown_ports(artifact: Artifact, system_id: str, table: pa.Table) -> int:
    """成果物に無かったポートの数（W3 プラン §12 の 110）。

    **ポート単位で数える**（行は水平のぶんだけあるので、そのまま数えると 10 倍になる）。
    """
    n_horizons = len(HORIZONS_MIN)
    ports = set(artifact.ports)
    station_ids = table.column("station_id").to_pylist()[::n_horizons]
    return sum(1 for one in station_ids if f"{system_id}/{one}" not in ports)


def _probabilities(artifact: Artifact, samples: Samples, target: Target) -> tuple[Float64, Bools]:
    """B3 の確率と、気候値が使えたかどうか。**学習と同じ関数を呼ぶ。**"""
    model = artifact.targets[target.name]
    b1, _ = conditional.predict(model.b1, samples, target)
    b2 = climatology.predict(model.b2, samples, b1)
    used = np.asarray(b2.probability != b1, dtype=np.bool_)
    b3 = blend.predict(model.b3, blend.design(b1, b2.probability, samples.h_min))
    return b3, used


def to_x1000(values: Float64) -> Int16:
    """確率を 0〜1000 の整数にする。**丸めで範囲を出さない。**"""
    return np.clip(np.rint(values * PROBABILITY_SCALE), 0, PROBABILITY_SCALE).astype(np.int16)


def confidence_of(*, stale: bool, climatology_horizons: int) -> int:
    """確度（0026 の `confidence`）。

    | 値 | 意味 |
    |---|---|
    | 1 | フィードが古い（`t − base_observed_at > 600 秒`） |
    | 2 | 気候値が半分以下の水平でしか効いていない |
    | 3 | 気候値が過半の水平で効いた |

    **0 は使わない**（B1 は必ず出るので「参考値」以下にはならない）。
    """
    if stale:
        return 1
    return 3 if climatology_horizons * 2 > len(HORIZONS_MIN) else 2


def to_payload(
    forecasts: Sequence[Forecast],
    generated_at: datetime,
    base_observed_at: datetime,
    model_version: str,
) -> list[dict[str, object]]:
    """`upsert_forecasts` に渡す形にする。"""
    return [
        {
            "system_id": one.system_id,
            "station_id": one.station_id,
            "generated_at": generated_at.isoformat(),
            "base_observed_at": base_observed_at.isoformat(),
            "model_version": model_version,
            "horizons_min": list(HORIZONS_MIN),
            "p_bike_x1000": list(one.p_bike_x1000),
            "p_dock_x1000": list(one.p_dock_x1000),
            "confidence": one.confidence,
        }
        for one in forecasts
    ]


def batches(
    rows: Sequence[Mapping[str, object]], size: int
) -> Iterator[Sequence[Mapping[str, object]]]:
    """まとめて送る単位に刻む。"""
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


# ── 実行（副作用）──────────────────────────────────────────────
def _log(message: str) -> None:
    print(message, file=sys.stderr)


def run_inference(
    port: InferPort, system_id: str, model_version: str, now: datetime
) -> InferSummary:
    """1 システムぶんの推論。**掴めなければ何もしない。**"""
    started = datetime.now(UTC)
    cpu_started = time.process_time_ns()
    base = port.read_base_observed_at(system_id)
    if base is None:
        return _skipped(
            system_id, model_version, None, started, cpu_started, "観測がまだありません"
        )

    run_id = port.begin_inference(system_id, base, model_version)
    if run_id is None:
        return _skipped(system_id, model_version, base, started, cpu_started, None)

    try:
        summary = _produce(port, system_id, model_version, now, base, started, cpu_started)
    except Exception as cause:
        elapsed = _elapsed_ms(started)
        port.finish_inference(run_id, "failed", 0, elapsed, type(cause).__name__)
        _log(f"推論に失敗しました: {system_id} / {type(cause).__name__}")
        return InferSummary(
            system_id=system_id,
            status="failed",
            base_observed_at=base,
            model_version=model_version,
            n_stations=0,
            n_skipped=0,
            n_unknown_ports=0,
            n_rows=0,
            duration_ms=elapsed,
            cpu_ms=_cpu_ms(cpu_started),
            error=type(cause).__name__,
        )
    port.finish_inference(
        run_id, "ok", summary.n_rows, summary.duration_ms, None, to_record(summary)
    )
    return summary


def _produce(
    port: InferPort,
    system_id: str,
    model_version: str,
    now: datetime,
    base: datetime,
    started: datetime,
    cpu_started: int,
) -> InferSummary:
    artifact = load_artifact(port, model_version)
    holidays = frozenset(port.list_holidays())
    # **基準時刻は 5 分格子に落とす。** 水平の起点は `generated_at` なので、
    # 書き込む `generated_at` も同じ時刻にする（W4 プラン §12 の 114）
    at = grid_time(now)
    features_started = time.monotonic_ns()
    reference_day, ready = read_features(port, system_id, at, holidays)
    features_ms = int((time.monotonic_ns() - features_started) // NS_PER_MS)
    stale = (at - base).total_seconds() > MAX_STALENESS_S
    forecasts = predict(artifact, system_id, at, ready.table, stale)
    payload = to_payload(forecasts, at, base, artifact.model_version)
    written = sum(port.upsert_forecasts(chunk) for chunk in batches(payload, UPSERT_BATCH))
    return InferSummary(
        system_id=system_id,
        status="ok",
        base_observed_at=base,
        model_version=artifact.model_version,
        n_stations=ready.stats.stations,
        n_skipped=sum(ready.stats.excluded.values()),
        n_unknown_ports=unknown_ports(artifact, system_id, ready.table),
        n_rows=written,
        duration_ms=_elapsed_ms(started),
        cpu_ms=_cpu_ms(cpu_started),
        features_ms=features_ms,
        excluded=dict(sorted(ready.stats.excluded.items())),
        reference_date=reference_day.isoformat(),
    )


class MissingArtifactError(RuntimeError):
    """成果物が Storage に無い。**代わりの値をでっち上げない。**"""


#: 読み込んだ成果物。**版が変わるまで取り直さない**（開発プラン §8.2）。
#: 成果物は 3.2 MB あり、5 分毎に取り直すと 1 日 900 MB の転送になる。
#: Fluid compute の温まったインスタンスではこれが効く。冷えれば消えるだけで、
#: 正しさには影響しない（同じ版からは同じ値が読める）。
_CACHE: dict[str, Artifact] = {}


def load_artifact(port: InferPort, model_version: str) -> Artifact:
    """成果物を読む。**無ければ止める**（`fit_baseline` で作る）。"""
    cached = _CACHE.get(model_version)
    if cached is not None:
        return cached
    body = port.download(MODEL_BUCKET, model_path(model_version))
    if body is None:
        raise MissingArtifactError(f"成果物がありません: {model_version}")
    artifact = from_bytes(body)
    # 版が変わったら古いものは要らない。1 つだけ持つ
    _CACHE.clear()
    _CACHE[model_version] = artifact
    return artifact


def forget_artifacts() -> None:
    """キャッシュを捨てる。**テストが版をまたぐときに使う。**"""
    _CACHE.clear()


def _skipped(
    system_id: str,
    model_version: str,
    base: datetime | None,
    started: datetime,
    cpu_started: int,
    reason: str | None,
) -> InferSummary:
    return InferSummary(
        system_id=system_id,
        status="skipped",
        base_observed_at=base,
        model_version=model_version,
        n_stations=0,
        n_skipped=0,
        n_unknown_ports=0,
        n_rows=0,
        duration_ms=_elapsed_ms(started),
        cpu_ms=_cpu_ms(cpu_started),
        error=reason,
    )


def _cpu_ms(started_ns: int) -> int:
    """このプロセスが使った CPU 時間（ミリ秒）。

    `duration_ms`（実時間）は Storage の取得と PostgREST の往復を含む。**費用は実時間
    ではなく Active CPU で決まる**（開発プラン §4.5a）ので、計算に使った時間を分けて
    記録する。1 サイクル 3.5 秒のうちどれだけが計算かが分かる。

    **プロセス単位で測る。** Fluid compute は 1 つのプロセスで複数の要求を捌きうるので、
    同時に走っていれば他の要求ぶんも混ざる。推論は 5 分周期で 2 系統を 3 分ずらして
    あるため、実際にはほぼ重ならない。
    """
    return (time.process_time_ns() - started_ns) // 1_000_000


def _elapsed_ms(started: datetime) -> int:
    return int((datetime.now(UTC) - started).total_seconds() * 1000)


def to_detail(summary: InferSummary) -> dict[str, object]:
    """応答の JSON。**秘密を含めない**（例外は種別だけ）。"""
    return {
        "ok": summary.ok,
        "system": summary.system_id,
        "status": summary.status,
        "base_observed_at": None
        if summary.base_observed_at is None
        else summary.base_observed_at.isoformat(),
        "model_version": summary.model_version,
        "stations": summary.n_stations,
        "skipped": summary.n_skipped,
        "unknown_ports": summary.n_unknown_ports,
        "rows": summary.n_rows,
        "duration_ms": summary.duration_ms,
        # **所要の大半は特徴量づくり**（W4 プラン §6.3 の完了条件）。分けて出さないと、
        # 遅くなったときに読み込みなのか組み立てなのかが分からない
        "features_ms": summary.features_ms,
        "cpu_ms": summary.cpu_ms,
        "excluded": dict(summary.excluded),
        "reference_date": summary.reference_date,
        "error": summary.error,
    }


def to_record(summary: InferSummary) -> dict[str, object]:
    """`inference_log.detail` に残す要約。

    **列に在るものは入れない。** `status` / `n_rows` / `duration_ms` / `error` /
    `model_version` / `base_observed_at` は列で持っているので、同じ値を jsonb にも
    並べると 1 日 576 行ぶん無駄に膨らむ（0030 の冒頭）。
    """
    return {
        "stations": summary.n_stations,
        "skipped": summary.n_skipped,
        "unknown_ports": summary.n_unknown_ports,
        # **`cpu_ms` は列にしない。** 開発プラン §5.3 の DDL には在るが、実装では
        # `detail` に入れる（列を足さずに済み、0030 でその口を作った）
        "cpu_ms": summary.cpu_ms,
        # 推論の所要の大半は特徴量づくり。**分けて見えないと、遅くなったときに
        # どこが遅いか分からない**（W4 プラン §6.3 の完了条件）
        "features_ms": summary.features_ms,
        "excluded": dict(summary.excluded),
        # **どの版の参照データで出した予測か。** 00:00〜05:00 JST は 1 つ古い版になる
        "reference_date": summary.reference_date,
    }
