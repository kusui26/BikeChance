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

from bikechance_ml.features import build, neighbors, static, weather
from bikechance_ml.features.arrays import Bools, Float64, Int16
from bikechance_ml.features.constants import (
    GRID_MINUTES,
    HORIZONS_MIN,
    MAX_STALENESS_S,
)
from bikechance_ml.features.grid import from_epoch_ms, jst_date, to_epoch_ms
from bikechance_ml.features.reference import SystemReference
from bikechance_ml.features.weather import WeatherRow
from bikechance_ml.jobs.build_features import (
    SYSTEM_IDS,
    Estimates,
    MissingReferenceError,
    read_reference_on,
)
from bikechance_ml.jobs.snapshot_table import Snapshot, StationRow, station_ids_by_idx
from bikechance_ml.jobs.snapshot_table import to_table as to_snapshot_table
from bikechance_ml.models import registry
from bikechance_ml.models.predictor import Prediction, Predictor, station_ids_of

#: 推論が読む観測の範囲（`build.serving_windows` が返す形）。
type Windows = Sequence[tuple[datetime, datetime]]

#: 確率を整数で持つときの倍率（0026 の `p_bike_x1000`）。
PROBABILITY_SCALE: Final[int] = 1000

#: 1 回の往復で送る予測の数。20,745 行を 6 往復に刻む。
UPSERT_BATCH: Final[int] = 4_000

MS_PER_S: Final[int] = 1000
NS_PER_MS: Final[int] = 1_000_000


class InferPort(Protocol):
    """入出力の口。テストはここだけを置き換える。"""

    def read_base_observed_at(self, system_id: str) -> datetime | None: ...
    def list_stations(self, system_id: str) -> tuple[StationRow, ...]: ...
    def list_snapshots(
        self, system_id: str, start: datetime, end: datetime
    ) -> tuple[Snapshot, ...]: ...
    def list_holidays(self) -> tuple[date, ...]: ...
    def list_weather(self, start: datetime, end: datetime) -> tuple[WeatherRow, ...]: ...
    def active_model(self) -> registry.Registered | None: ...
    def find_model(self, model_version: str) -> registry.Registered | None: ...
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

    読むのは 4 つ。
      1. **前日の参照スナップショット**（学習と同じ規則。W3 プラン §14.3）
      2. **自系統の 2 つの窓**（`build.serving_windows`）
      3. **他系統の直近**（近傍の集計に要るのは「いまの状態」だけ。§6.3）
      4. **`at` までに入手できた予報**（`weather.serving_window`。W4 プラン §6.4）

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
            weather=weather.to_weather(port.list_weather(*weather.serving_window(at))),
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
    #: 使えた予報の発行数。**0 なら天気の 4 列は全部 NULL**（W4 プラン §6.4）
    weather_issues: int = 0
    #: 配った版の種類（`baseline` / `lightgbm`）。**何を配ったかが後から読める**
    model_kind: str = ""
    #: その版を**当てはめたときの**特徴量の版。配信側は `FEATURE_SET`（W4 プラン §6.5）
    model_feature_set: str = ""
    #: 試し打ちで作れた予測の数（書いていないので `n_rows` は 0 になる）
    n_predicted: int = 0
    #: 試し打ちの見本 1 件
    sample: Mapping[str, object] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "skipped", "dry_run")


# ── 予測（純粋）────────────────────────────────────────────────
class RowCountError(ValueError):
    """行数が `ポート × 水平` になっていない。**畳み直せないので止める。**"""


def predict(
    predictor: Predictor, system_id: str, at: datetime, table: pa.Table, stale: bool
) -> tuple[Forecast, ...]:
    """予測を作る。**出せないポートは `build_now` が既に落としている。**

    **モデルの種類を知らない。** `Predictor` が行ごとの確率と「本来の情報で出せたか」を
    返し、ここは `(ポート, 水平)` に畳み直して確度を付けるだけである。
    """
    n_horizons = len(HORIZONS_MIN)
    if table.num_rows == 0:
        return ()
    if table.num_rows % n_horizons != 0:
        raise RowCountError(f"行数 {table.num_rows} が水平 {n_horizons} で割り切れない")
    outcome = predictor.predict(system_id, at, table)
    bike = to_x1000(outcome.probability["bike"]).reshape(-1, n_horizons)
    dock = to_x1000(outcome.probability["dock"]).reshape(-1, n_horizons)
    informed = _both_informed(outcome, n_horizons)
    # **並びは `(ポート, 水平)`。** `build_now` が station_id, h_min の昇順に並べている
    return tuple(
        Forecast(
            system_id=system_id,
            station_id=station_id,
            p_bike_x1000=tuple(int(one) for one in bike[index]),
            p_dock_x1000=tuple(int(one) for one in dock[index]),
            confidence=confidence_of(stale=stale, informed_horizons=int(informed[index].sum())),
        )
        for index, station_id in enumerate(station_ids_of(table))
    )


def _both_informed(outcome: Prediction, n_horizons: int) -> Bools:
    """両方のターゲットで本来の情報が使えた水平。**厳しいほうに寄せる。**"""
    both = outcome.informed["bike"] & outcome.informed["dock"]
    return np.asarray(both.reshape(-1, n_horizons), dtype=np.bool_)


def to_x1000(values: Float64) -> Int16:
    """確率を 0〜1000 の整数にする。**丸めで範囲を出さない。**"""
    return np.clip(np.rint(values * PROBABILITY_SCALE), 0, PROBABILITY_SCALE).astype(np.int16)


def confidence_of(*, stale: bool, informed_horizons: int) -> int:
    """確度（0026 の `confidence`）。

    | 値 | 意味 |
    |---|---|
    | 1 | フィードが古い（`t − base_observed_at > 600 秒`） |
    | 2 | モデルが本来の情報で出せた水平が半分以下 |
    | 3 | 過半の水平で本来の情報が使えた |

    「本来の情報」はモデルによって違う。**B3 では気候値が引けたか**、LightGBM では
    常に真（欠損は木が扱うので、引けなかったに当たる状態が無い）。**確度の意味づけは
    W5 の校正で見直す**：いまは「配信の鮮度」と「ベースラインの当てはまり」の 2 つを
    1 つの数に押し込んでいる。

    **0 は使わない**（B1 は必ず出るので「参考値」以下にはならない）。
    """
    if stale:
        return 1
    return 3 if informed_horizons * 2 > len(HORIZONS_MIN) else 2


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
    port: InferPort, system_id: str, now: datetime, model_version: str | None = None
) -> InferSummary:
    """1 システムぶんの推論。**掴めなければ何もしない。**

    配る版は **`model_versions` の `active`** を引く（W4-08）。`model_version` を渡すと
    その版を名指しで使う——候補を手で試すときだけの口で、Cron は渡さない。

    **`active` でない版は「試し打ち」になる**（`status != 'active'`）。掴まず、
    `station_forecasts` にも `inference_log` にも書かない。理由は 2 つある。

      * `station_forecasts` は **1 ポート 1 行**で、書けば次の 5 分周期まで**利用者に
        候補の確率が出る**。配信を切り替えるのは `promote_model_version()` の仕事で、
        人が確認して行う（CLAUDE.md §6）。手で叩いた 1 回がそれを迂回してはいけない
      * `inference_log` は `(system_id, base_observed_at)` で掴む。試し打ちが掴むと、
        **その観測に対する本物の推論が「二重」として飛ばされる**

    **版を引くのは掴む前。** `inference_log` に「どの版で掴んだか」を残すため、そして
    登録簿が読めない状態で行だけ作らないためである。
    """
    started = datetime.now(UTC)
    cpu_started = time.process_time_ns()
    chosen = registry.active(port) if model_version is None else registry.named(port, model_version)
    if chosen.status != "active":
        return _dry_run(port, system_id, chosen, now, started, cpu_started)
    base = port.read_base_observed_at(system_id)
    if base is None:
        return _skipped(
            system_id, chosen.model_version, None, started, cpu_started, "観測がまだありません"
        )

    run_id = port.begin_inference(system_id, base, chosen.model_version)
    if run_id is None:
        return _skipped(system_id, chosen.model_version, base, started, cpu_started, None)

    try:
        summary = _produce(port, system_id, chosen, now, base, started, cpu_started)
    except Exception as cause:
        elapsed = _elapsed_ms(started)
        port.finish_inference(run_id, "failed", 0, elapsed, type(cause).__name__)
        _log(f"推論に失敗しました: {system_id} / {type(cause).__name__}")
        return InferSummary(
            system_id=system_id,
            status="failed",
            base_observed_at=base,
            model_version=chosen.model_version,
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


def _dry_run(
    port: InferPort,
    system_id: str,
    chosen: registry.Registered,
    now: datetime,
    started: datetime,
    cpu_started: int,
) -> InferSummary:
    """**候補の試し打ち。** 予測まで作り、どこにも書かずに要約だけ返す。

    確かめられるのは「特徴量 → モデル → 確率」までで、`upsert_forecasts` は通らない。
    そこは `active` の版が 5 分毎に通している経路と**同じ 1 本**である
    （`to_payload` → `batches` → `upsert_forecasts`）。
    """
    predictor = registry.load(port, chosen)
    holidays = frozenset(port.list_holidays())
    at = grid_time(now)
    features_started = time.monotonic_ns()
    reference_day, ready = read_features(port, system_id, at, holidays)
    features_ms = int((time.monotonic_ns() - features_started) // NS_PER_MS)
    base = port.read_base_observed_at(system_id)
    stale = base is None or (at - base).total_seconds() > MAX_STALENESS_S
    forecasts = predict(predictor, system_id, at, ready.table, stale)
    return InferSummary(
        system_id=system_id,
        status="dry_run",
        base_observed_at=base,
        model_version=predictor.model_version,
        n_stations=ready.stats.stations,
        n_skipped=sum(ready.stats.excluded.values()),
        n_unknown_ports=predictor.unknown_ports(system_id, ready.table),
        # **書いていないので 0。** 出せた数は `predictable` に出る
        n_rows=0,
        duration_ms=_elapsed_ms(started),
        cpu_ms=_cpu_ms(cpu_started),
        features_ms=features_ms,
        excluded=dict(sorted(ready.stats.excluded.items())),
        reference_date=reference_day.isoformat(),
        weather_issues=ready.stats.weather_issues,
        model_kind=predictor.kind,
        model_feature_set=predictor.feature_set,
        n_predicted=len(forecasts),
        sample=_sample_of(forecasts),
    )


def _sample_of(forecasts: Sequence[Forecast]) -> dict[str, object]:
    """試し打ちの応答に載せる 1 件。**確率が本当に出ているか**を目で見るため。"""
    if not forecasts:
        return {}
    one = forecasts[0]
    return {
        "station_id": one.station_id,
        "p_bike_x1000": list(one.p_bike_x1000),
        "p_dock_x1000": list(one.p_dock_x1000),
        "confidence": one.confidence,
    }


def _produce(
    port: InferPort,
    system_id: str,
    chosen: registry.Registered,
    now: datetime,
    base: datetime,
    started: datetime,
    cpu_started: int,
) -> InferSummary:
    predictor = registry.load(port, chosen)
    holidays = frozenset(port.list_holidays())
    # **基準時刻は 5 分格子に落とす。** 水平の起点は `generated_at` なので、
    # 書き込む `generated_at` も同じ時刻にする（W4 プラン §12 の 114）
    at = grid_time(now)
    features_started = time.monotonic_ns()
    reference_day, ready = read_features(port, system_id, at, holidays)
    features_ms = int((time.monotonic_ns() - features_started) // NS_PER_MS)
    stale = (at - base).total_seconds() > MAX_STALENESS_S
    forecasts = predict(predictor, system_id, at, ready.table, stale)
    payload = to_payload(forecasts, at, base, predictor.model_version)
    written = sum(port.upsert_forecasts(chunk) for chunk in batches(payload, UPSERT_BATCH))
    return InferSummary(
        system_id=system_id,
        status="ok",
        base_observed_at=base,
        model_version=predictor.model_version,
        n_stations=ready.stats.stations,
        n_skipped=sum(ready.stats.excluded.values()),
        n_unknown_ports=predictor.unknown_ports(system_id, ready.table),
        n_rows=written,
        duration_ms=_elapsed_ms(started),
        cpu_ms=_cpu_ms(cpu_started),
        features_ms=features_ms,
        excluded=dict(sorted(ready.stats.excluded.items())),
        reference_date=reference_day.isoformat(),
        weather_issues=ready.stats.weather_issues,
        model_kind=predictor.kind,
        model_feature_set=predictor.feature_set,
    )


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
        "weather_issues": summary.weather_issues,
        "model_kind": summary.model_kind,
        "model_feature_set": summary.model_feature_set,
        # **試し打ちのときだけ載せる。** 通常の推論では 0 と空になる
        **(
            {"predicted": summary.n_predicted, "sample": dict(summary.sample)}
            if summary.status == "dry_run"
            else {}
        ),
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
        # **天気が乗っているか。** 0 なら取り込みが止まっている（`load_weather`）
        "weather_issues": summary.weather_issues,
        # **何を配ったか。** 版は列に在るが、種類と特徴量の版は在らない
        "model_kind": summary.model_kind,
        "model_feature_set": summary.model_feature_set,
    }
