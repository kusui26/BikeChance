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
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final, Protocol

import numpy as np

from bikechance_ml.baselines import blend, climatology, conditional
from bikechance_ml.baselines.artifact import Artifact, from_bytes
from bikechance_ml.eval.dataset import TARGETS, Samples, Target
from bikechance_ml.features.arrays import Bools, Float64, Int16
from bikechance_ml.features.calendar import DOW_TYPES, day_type, dow_type
from bikechance_ml.features.constants import (
    FLAG_RENTING,
    FLAG_RETURNING,
    HORIZONS_MIN,
    MAX_STALENESS_S,
    MISSING,
    PHANTOM_STATIONS,
)
from bikechance_ml.features.grid import JST
from bikechance_ml.features.reference import StationStatusRow

#: 確率を整数で持つときの倍率（0026 の `p_bike_x1000`）。
PROBABILITY_SCALE: Final[int] = 1000

#: 1 回の往復で送る予測の数。20,745 行を 6 往復に刻む。
UPSERT_BATCH: Final[int] = 4_000

#: 成果物の置き場所。
MODEL_BUCKET: Final[str] = "gbfs-parquet"
MODEL_PREFIX: Final[str] = "models/baseline"

_MINUTES_PER_DAY: Final[int] = 24 * 60


def model_path(model_version: str) -> str:
    """成果物のパス。**版がそのままファイル名**になる。"""
    return f"{MODEL_PREFIX}/{model_version}.json.gz"


class InferPort(Protocol):
    """入出力の口。テストはここだけを置き換える。"""

    def read_base_observed_at(self, system_id: str) -> datetime | None: ...
    def list_station_status(self, system_id: str) -> tuple[StationStatusRow, ...]: ...
    def list_holidays(self) -> tuple[date, ...]: ...
    def download(self, bucket: str, path: str) -> bytes | None: ...
    def begin_inference(
        self, system_id: str, base_observed_at: datetime, model_version: str
    ) -> int | None: ...
    def finish_inference(
        self, run_id: int, status: str, n_rows: int, duration_ms: int, error: str | None = None
    ) -> None: ...
    def upsert_forecasts(self, rows: Sequence[Mapping[str, object]]) -> int: ...


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
    n_rows: int
    duration_ms: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "skipped")


# ── 予測（純粋）────────────────────────────────────────────────
def is_predictable(system_id: str, row: StationStatusRow) -> bool:
    """予測を出してよいポートか。**学習と同じ除外規則**（`features/exclude.py`）。

    最新スナップショットに現れていない、値が `-1`、`t` 時点で運用停止、実在しない
    ポートは出さない。**出さない**のであって、0 や 0.5 で埋めない。
    """
    if (system_id, row.station_id) in PHANTOM_STATIONS:
        return False
    if not row.is_present or row.bikes == MISSING or row.docks == MISSING:
        return False
    both = FLAG_RENTING | FLAG_RETURNING
    return row.flags >= 0 and (row.flags & both) == both


def target_dow_types(at: datetime, holidays: frozenset[date]) -> tuple[int, ...]:
    """水平ごとの、到着時刻の曜日種別の番号。**日をまたぐのは翌日まで**（最長 180 分）。"""
    local = at.astimezone(JST)
    order = tuple(sorted(DOW_TYPES))
    return tuple(
        order.index(dow_type(day_type((local + timedelta(minutes=horizon)).date(), holidays)))
        for horizon in HORIZONS_MIN
    )


def to_samples(
    artifact: Artifact,
    system_id: str,
    rows: Sequence[StationStatusRow],
    at: datetime,
    holidays: frozenset[date],
) -> Samples:
    """ポート × 水平の行を、学習と同じ `Samples` の形にする。

    **ラベルは持たない**（`predict` は読まない）。持たせるとゼロが答えに見えてしまうので、
    長さ 0 の配列を入れておく。
    """
    ports = {name: index for index, name in enumerate(artifact.ports)}
    n_horizons = len(HORIZONS_MIN)
    system = artifact.systems.index(system_id)
    port = np.array(
        [ports.get(f"{system_id}/{row.station_id}", -1) for row in rows], dtype=np.int32
    )
    minute = at.astimezone(JST).hour * 60 + at.astimezone(JST).minute
    return Samples(
        systems=artifact.systems,
        n_ports=len(artifact.ports),
        system=np.full(len(rows) * n_horizons, system, dtype=np.int8),
        port=np.repeat(port, n_horizons),
        day=np.full(len(rows) * n_horizons, at.astimezone(JST).date().toordinal(), dtype=np.int32),
        h_min=np.tile(np.array(HORIZONS_MIN, dtype=np.int16), len(rows)),
        minute_of_day=np.full(len(rows) * n_horizons, minute, dtype=np.int16),
        dow_type=np.tile(np.array(target_dow_types(at, holidays), dtype=np.int8), len(rows)),
        weight=np.ones(len(rows) * n_horizons, dtype=np.float32),
        labels={target.label: np.zeros(0, dtype=np.int8) for target in TARGETS},
        counts={
            "bikes": np.repeat(np.array([row.bikes for row in rows], dtype=np.int16), n_horizons),
            "docks": np.repeat(np.array([row.docks for row in rows], dtype=np.int16), n_horizons),
        },
    )


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
    """0 参考 / 1 低 / 2 中 / 3 高（0026 の `confidence`）。

    | 値 | 意味 |
    |---|---|
    | 1 | フィードが古い（`t − base_observed_at > 600 秒`） |
    | 2 | 参照表の再校正だけ。**そのポートの履歴が足りていない** |
    | 3 | **過半の水平で気候値が効いた**（そのポート・その曜日種別・その時刻の実績がある） |

    **「全水平で」にしない。** 気候値のセルは水平ごとに違う 15 分枠を引くので、
    10 個そろうのは蓄積が十分に長くなってからになる。実測（3 日ぶん）では
    14,663 ポート中 40 しか揃わず、信号として使えなかった。

    蓄積が伸びるほど 3 が増える。**いま 3 がほとんど出ないのは、そのとおりの状態を
    表している。**
    """
    if stale:
        return 1
    return 3 if climatology_horizons * 2 > len(HORIZONS_MIN) else 2


def predict(
    artifact: Artifact,
    system_id: str,
    rows: Sequence[StationStatusRow],
    at: datetime,
    base_observed_at: datetime,
    holidays: frozenset[date],
) -> tuple[Forecast, ...]:
    """予測を作る。**出せないポートは行を作らない。**"""
    usable = [row for row in rows if is_predictable(system_id, row)]
    if not usable:
        return ()
    samples = to_samples(artifact, system_id, usable, at, holidays)
    stale = (at - base_observed_at).total_seconds() > MAX_STALENESS_S
    n_horizons = len(HORIZONS_MIN)
    columns = {target.name: _probabilities(artifact, samples, target) for target in TARGETS}
    bike = to_x1000(columns["bike"][0]).reshape(len(usable), n_horizons)
    dock = to_x1000(columns["dock"][0]).reshape(len(usable), n_horizons)
    used = (columns["bike"][1] & columns["dock"][1]).reshape(len(usable), n_horizons)
    return tuple(
        Forecast(
            system_id=system_id,
            station_id=row.station_id,
            p_bike_x1000=tuple(int(one) for one in bike[index]),
            p_dock_x1000=tuple(int(one) for one in dock[index]),
            confidence=confidence_of(stale=stale, climatology_horizons=int(used[index].sum())),
        )
        for index, row in enumerate(usable)
    )


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
    base = port.read_base_observed_at(system_id)
    if base is None:
        return _skipped(system_id, model_version, None, started, "観測がまだありません")

    run_id = port.begin_inference(system_id, base, model_version)
    if run_id is None:
        return _skipped(system_id, model_version, base, started, None)

    try:
        summary = _produce(port, system_id, model_version, now, base, started)
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
            n_rows=0,
            duration_ms=elapsed,
            error=type(cause).__name__,
        )
    port.finish_inference(run_id, "ok", summary.n_rows, summary.duration_ms)
    return summary


def _produce(
    port: InferPort,
    system_id: str,
    model_version: str,
    now: datetime,
    base: datetime,
    started: datetime,
) -> InferSummary:
    artifact = load_artifact(port, model_version)
    holidays = frozenset(port.list_holidays())
    rows = port.list_station_status(system_id)
    forecasts = predict(artifact, system_id, rows, now, base, holidays)
    payload = to_payload(forecasts, now, base, artifact.model_version)
    written = sum(port.upsert_forecasts(chunk) for chunk in batches(payload, UPSERT_BATCH))
    return InferSummary(
        system_id=system_id,
        status="ok",
        base_observed_at=base,
        model_version=artifact.model_version,
        n_stations=len(rows),
        n_skipped=len(rows) - len(forecasts),
        n_rows=written,
        duration_ms=_elapsed_ms(started),
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
    reason: str | None,
) -> InferSummary:
    return InferSummary(
        system_id=system_id,
        status="skipped",
        base_observed_at=base,
        model_version=model_version,
        n_stations=0,
        n_skipped=0,
        n_rows=0,
        duration_ms=_elapsed_ms(started),
        error=reason,
    )


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
        "rows": summary.n_rows,
        "duration_ms": summary.duration_ms,
        "error": summary.error,
    }
