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

**ポートプロファイルは前日の版を、要る行群だけ Range 要求で読む**（W6 の PR D、J2）。
00:40 JST の回が前日の版を置くまで（Actions の遅れで 2 時間前後）は 1 つ古い版を読み、
**読んだ版の日付を必ず記録に残す**（`profile_date`。契約 32）。**読めなくても配信は止めない**——
B3 は `prof_*` を読まないので、配る確率は変わらない。**`prof_*` を読む版は、読めなかった周期に
配らない**（W6-05、契約 33）。

**shadow は、active を配って予測ログを置いてから歩く**（W6 の PR E、W6-06、契約 34）。特徴量は
作り直さず、**同じ表で予測して予測ログにだけ書く**——`station_forecasts` にも `inference_log` の
列にも書かない。shadow の例外は `detail.shadow` に詰め替え、**active の結果を変えない**。
`prof_*` を読む shadow は、プロファイルを読めた周期だけ歩く（契約 33）。shadow を上げ下げする
のは人である（`promote_model_version()`・`retire_model_version()`。契約 39）。

**前日の参照スナップショットは、組み立てた形で使い回す**（W6 の PR G の⑤、W6-20）。版は
1 日 1 回（05:00 JST）しか変わらないのに、5 分毎に読んで組み立て直していた。**読む版の
決め方は変えない**——新しい版から順に確かめるので、05:00 に版が置かれた次の周期から移る。
**段ごとの所要**（参照・観測・`build_now`）を `detail` に出す。
"""

import dataclasses
import resource
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Final, Protocol

import numpy as np
import pyarrow as pa

from bikechance_ml.features import build, neighbors, profile, static, weather
from bikechance_ml.features.arrays import Bools, Float64, Int16
from bikechance_ml.features.constants import (
    GRID_MINUTES,
    HORIZONS_MIN,
    MAX_STALENESS_S,
)
from bikechance_ml.features.grid import from_epoch_ms, jst_date, profile_path, to_epoch_ms
from bikechance_ml.features.weather import WeatherRow
from bikechance_ml.io import range_file
from bikechance_ml.io.supabase import PARQUET_BUCKET
from bikechance_ml.jobs import forecast_log
from bikechance_ml.jobs.build_features import (
    SYSTEM_IDS,
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

#: `getrusage` の `ru_maxrss` の単位。**本番（Linux）は KiB、macOS はバイト。**
RSS_UNIT_B: Final[int] = 1 if sys.platform == "darwin" else 1024
BYTES_PER_MB: Final[int] = 1024 * 1024


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
    def shadow_model(self) -> registry.Registered | None: ...
    def download(self, bucket: str, path: str) -> bytes | None: ...
    def download_range(
        self, bucket: str, path: str, byte_range: str
    ) -> range_file.Piece | None: ...
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
    def upload_forecast_log(self, path: str, body: bytes) -> None: ...


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


@dataclass(frozen=True)
class ProfileRead:
    """推論が読んだポートプロファイル（W6 の PR D）。**読めなければ `edition` は None。**"""

    edition: profile.Edition | None
    #: **実際に落としたバイト数**（フッタ・読み直し・無い版の確かめ・止まった読みも含む）。
    #: **1 周期 2 MB 未満**が約束（J2 の完了条件 3）
    bytes_read: int
    #: 読むのに掛かった時間（無い版を確かめる往復・読み・形の確かめを含む）
    load_ms: int
    #: 読めなかった理由。`missing`（2 日とも無い）か `failed:<例外の種類>`。読めたら None
    reason: str | None


@dataclass(frozen=True)
class Stages:
    """特徴量づくりの段ごとの所要（W6 の PR G、W6-20）。**`features_ms` の内訳**である。

    残りはプロファイル（`ProfileRead.load_ms`）と天気の読み。
    """

    #: 参照の組み立て（読み・`to_facts`・`to_links`）。**使い回せた周期はほぼ 0**
    reference_ms: int
    #: 参照を使い回せたか（`hit`）、読んで組み立てたか（`miss`）
    reference_cache: str
    #: 観測の読み（PostgREST の往復・整数配列の型検査・長形式化）
    observations_ms: int
    #: `build_now`（as-of・流量・格子・列の組み立て）
    build_ms: int


@dataclass(frozen=True)
class Features:
    """推論 1 回ぶんの特徴量と、**材料の版**（どちらも記録に出す）。

    参照スナップショットの日付を持つのは、00:00〜05:00 JST に 1 つ古い版を使うことがあるため
    （`reference_for`）。プロファイルも同じく、朝の穴では 1 つ古い版になる。
    """

    reference_day: date
    ready: build.Ready
    profile: ProfileRead
    stages: Stages


#: プロファイルを何日前までさかのぼって探すか（W6-04、契約 32）。**前日の版は 00:40 JST の回が
#: 置く**（Actions の遅れで 2 時間前後）ので、それまでの周期は 1 つ古い版を読む。
PROFILE_FALLBACK_DAYS: Final[int] = 2


def read_features(
    port: InferPort, system_id: str, at: datetime, holidays: frozenset[date]
) -> Features:
    """推論 1 回ぶんの特徴量を作る。**学習と同じ `features/` を通る。**

    読むのは 4 つ。
      1. **前日の参照スナップショット**（学習と同じ規則。W3 プラン §14.3）
      2. **自系統の 2 つの窓**（`build.serving_windows`）と、**他系統の直近**（近傍の集計に
         要るのは「いまの状態」だけ。§6.3）
      3. **`at` までに入手できた予報**（`weather.serving_window`。W4 プラン §6.4）
      4. **前日のポートプロファイルの、要る行群だけ**（W6 の PR D。`read_profile_edition`）
    """
    (reference, reused), reference_ms = _timed(lambda: reference_for(port, jst_date(at)))
    read = read_profile_edition(port, at, holidays)
    table, observations_ms = _timed(lambda: _observations(port, system_id, at))
    inputs = build.NowInputs(
        at=at,
        system_id=system_id,
        reference=build.Reference(facts=reference.facts, links=reference.links, holidays=holidays),
        table=table,
        weather=weather.to_weather(port.list_weather(*weather.serving_window(at))),
        profile=read.edition,
    )
    ready, build_ms = _timed(lambda: build.build_now(inputs))
    stages = Stages(reference_ms, "hit" if reused else "miss", observations_ms, build_ms)
    return Features(reference_day=reference.day, ready=ready, profile=read, stages=stages)


def _timed[T](work: Callable[[], T]) -> tuple[T, int]:
    """`work` を呼び、**掛かった時間（ミリ秒）**も返す。"""
    started = time.monotonic_ns()
    result = work()
    return result, _ms_since(started)


def _observations(port: InferPort, system_id: str, at: datetime) -> pa.Table:
    """自系統の 2 つの窓と、他系統の直近を 1 つの長形式にする。"""
    tables = [read_observations(port, system_id, build.serving_windows(at))]
    tables.extend(
        read_observations(port, other, (_neighbour_window(at),))
        for other in SYSTEM_IDS
        if other != system_id
    )
    return pa.concat_tables(tables)


def read_profile_edition(port: InferPort, at: datetime, holidays: frozenset[date]) -> ProfileRead:
    """**前日の版を読み、まだ無ければ 1 つ古い版**（最大 2 日前。W6-04、契約 32）。要る行群だけ。

    **どんな失敗でも止めない**（W6-05）。B3 は `prof_*` を読まないので、配る確率は変わらない。
    `prof_*` を読む版は、呼ぶ側が止める（`_refuse_without_profile`）。**握り潰しにはならない**
    ——失敗の種類は `profile_reason` に残る（Storage の不調・前の並びの版・壊れた版・知らない失敗）。
    **下がるのは無いときだけ**——在るのに読めない版を飛ばして古い版を使うと、読むはずの版と
    記録が食い違う。
    """
    started = time.monotonic_ns()
    counted = _Counted(port)
    try:
        cells = profile.serving_cells(at, holidays)
        edition = _first_edition(counted, _edition_days(at), cells)
    except Exception as cause:
        reason: str | None = f"failed:{type(cause).__name__}"
        edition = None
    else:
        reason = None if edition is not None else "missing"
    return ProfileRead(edition, sum(counted.sizes), _ms_since(started), reason)


@dataclass
class _Counted:
    """Range 要求で**実際に落としたバイト**を数える口（読み直し・無い版の確かめ・失敗も含む）。

    **並行に呼ばれる**（`io/fanout`）。数は足し込まずに `append` で残す——CPython の
    `list.append` は 1 つの操作なので、並行でも取りこぼさない。
    """

    source: range_file.FetchesRanges
    sizes: list[int] = field(default_factory=list)

    def download_range(self, bucket: str, path: str, byte_range: str) -> range_file.Piece | None:
        piece = self.source.download_range(bucket, path, byte_range)
        self.sizes.append(0 if piece is None else len(piece.body))
        return piece


def _edition_days(at: datetime) -> list[date]:
    """読みに行く版の日（新しい順）。**1 つ目は学習と同じ `source_day`**（前日）。"""
    first = profile.source_day(jst_date(at))
    return [first - timedelta(days=back) for back in range(PROFILE_FALLBACK_DAYS)]


def _first_edition(
    source: range_file.FetchesRanges, days: Sequence[date], cells: Sequence[tuple[str, int]]
) -> profile.Edition | None:
    """在る中でいちばん新しい版。**無い版の確かめは末尾の 1 往復だけ**で済む（本文は無い）。"""
    reads = (_read_edition(source, day, cells) for day in days)
    return next((one for one in reads if one is not None), None)


def _read_edition(
    source: range_file.FetchesRanges, day: date, cells: Sequence[tuple[str, int]]
) -> profile.Edition | None:
    """1 日ぶんの版の、要る行群だけを読む。**無ければ None。** 読めたら形を確かめる。"""
    read = range_file.read_row_groups(
        source,
        PARQUET_BUCKET,
        profile_path(day, profile.PROFILE_NAME),
        lambda metadata: profile.wanted_groups(metadata, cells),
    )
    if read is None:
        return None
    edition = profile.Edition(day=day, table=read.table)
    profile.check_edition(edition)
    return edition


class ProfileRequiredError(RuntimeError):
    """`prof_*` を読む版を、プロファイル無しで配ろうとした。**配らない**（W6-05、契約 29・33）。"""


def _refuse_without_profile(predictor: Predictor, read: ProfileRead) -> None:
    """**`prof_*` を読む版は、プロファイルを読めなかった周期に配らない。**

    学習では値が在り、配信では NULL のまま配ると、例外を出さずに確率だけがずれる（契約 29）。
    合成器（PR H）は全セルを B3 に落として配り、shadow（PR E）は歩かない——**LightGBM 単体を
    active にしない**のが決まりなので、ここで止まるのはその決まりが破られたときだけである。
    """
    if read.edition is None and registry.reads_profile(predictor):
        raise ProfileRequiredError(
            f"{predictor.model_version} は prof_* を読むが、この周期はプロファイルが無い"
            f"（{read.reason}）"
        )


#: 参照スナップショットを何日前までさかのぼって探すか。
#: **前日の版は 05:00 JST に書かれる**ので、00:00〜05:00 の推論はまだ読めない。
REFERENCE_FALLBACK_DAYS: Final[int] = 2


@dataclass(frozen=True)
class BuiltReference:
    """組み立てた参照（`static.to_facts`・`neighbors.to_links`）と、その版の日付。"""

    day: date
    facts: static.StationFacts
    links: neighbors.NeighborLinks


#: 組み立てた参照（版の日付 → 中身）。**持つのは最後に組み立てた 1 版だけ**（W6 の PR G の⑤）。
#: 版は 1 日 1 回（05:00 JST）しか変わらないのに、5 分毎に読んで組み立て直していた
#: （W6 プラン §13.4）。**冷えれば消えるだけで、正しさに影響しない。**
_REFERENCES: dict[date, BuiltReference] = {}

#: `_REFERENCES` の出し入れを守る（`registry._CACHE_LOCK` と同じ理由）。**組み立てる間は握らない**
#: ——2 系統が同時に組み立てても、同じ版から同じものができるだけである。
_REFERENCES_LOCK: Final = threading.Lock()


def forget_references() -> None:
    """持っている参照を捨てる。**検査が同じ日付で中身の違う版を使うときに使う。**"""
    with _REFERENCES_LOCK:
        _REFERENCES.clear()


def reference_for(port: InferPort, day: date) -> tuple[BuiltReference, bool]:
    """読める中でいちばん新しい参照を、組み立てた形で返す。**2 つ目は使い回せたか。**

    規則は学習と同じ「**前日の版**」だが、その版が置かれるのは **05:00 JST**
    （`/ml/reference`）である。つまり **00:00〜05:00 JST の推論は前日の版をまだ読めない**
    ので、そのあいだは 1 つ古い版を使う（W4 プラン §12 の 118）。**黙って古い版を使わない。**
    使った日付は `inference_log.detail.reference_date` に残る。

    **新しい版から順に確かめる**（使い回す前と同じ順・同じ往復）。持っているのが前々日の
    版でも、前日の版が置かれていないかを毎周期確かめるので、05:00 に置かれた次の周期から
    新しい版に移る。**読む版の決め方は、使い回す前と変わらない。**
    """
    for back in range(1, REFERENCE_FALLBACK_DAYS + 1):
        source_day = day - timedelta(days=back)
        held = _held_reference(source_day)
        if held is not None:
            return held, True
        built = _build_reference(port, source_day)
        if built is not None:
            return built, False
    raise MissingReferenceError(
        f"参照スナップショットが {REFERENCE_FALLBACK_DAYS} 日ぶん見つからない（{day} 基準）"
    )


def _held_reference(source_day: date) -> BuiltReference | None:
    with _REFERENCES_LOCK:
        return _REFERENCES.get(source_day)


def _build_reference(port: InferPort, source_day: date) -> BuiltReference | None:
    """その日の版を読んで組み立て、持つ。**版が無ければ None**（他の失敗はそのまま投げる）。"""
    try:
        systems, estimates = read_reference_on(port, source_day)
    except MissingReferenceError:
        return None
    facts = static.to_facts(systems, estimates)
    built = BuiltReference(source_day, facts, neighbors.to_links(systems, facts.station_keys()))
    _read_only(built.facts)
    _read_only(built.links)
    with _REFERENCES_LOCK:
        _REFERENCES.clear()
        _REFERENCES[source_day] = built
    return built


def _read_only(record: static.StationFacts | neighbors.NeighborLinks) -> None:
    """**使い回す配列を書き込めなくする。** 誰かが書き換えると、次の周期の答えが黙って変わる。

    書き換える道があれば、その場で例外になる（検査はどれも、この配列で `build_now` を通る）。
    """
    for one in dataclasses.fields(record):
        value = getattr(record, one.name)
        if isinstance(value, np.ndarray):
            value.flags.writeable = False


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
class ShadowRun:
    """shadow を歩いた 1 回（W6 の PR E）。**`detail.shadow` にそのまま出す**（列には書かない）。"""

    #: shadow の行の版。**行を引けなかったときは None**
    model_version: str | None
    #: `ok`・`skipped:no_profile`（契約 33）・`failed:<例外の種類>`
    status: str
    #: 予測ログを置けたか（`ok`・`failed:<例外の種類>`）。**歩かなかったら空**
    forecast_log: str = ""
    #: 成果物を読むのに掛かった時間（**温まっていれば 0**。版ごとに持つ。契約 31）
    model_load_ms: int = 0
    #: 予測に掛かった時間。**shadow の費用はほぼここ**（特徴量は active と共有する）
    predict_ms: int = 0

    def as_detail(self) -> dict[str, object]:
        return asdict(self)


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
    #: `features_ms` の内訳（W6 の PR G、W6-20。`Stages`）。参照は**使い回せた周期はほぼ 0**
    reference_ms: int = 0
    #: 参照を使い回せたか（`hit`）、読んで組み立てたか（`miss`）
    reference_cache: str = ""
    observations_ms: int = 0
    build_ms: int = 0
    #: 除外の内訳（理由 → ポート数）。**なぜ出せなかったかが分かる**
    excluded: Mapping[str, int] = field(default_factory=dict)
    #: 使った参照スナップショットの日付。**00:00〜05:00 JST は 1 つ古い版になる**
    reference_date: str | None = None
    #: 使えた予報の発行数。**0 なら天気の 4 列は全部 NULL**（W4 プラン §6.4）
    weather_issues: int = 0
    #: 気象格子に当たらなかったポート数。**理由は 3 つある**（W4 プラン §6.4、§12 の 119）
    n_without_weather: int = 0
    #: 参照スナップショットに未掲載のポート数。**この数は予測が出ない**（同 §6.3c、§12 の 118）
    n_unreferenced: int = 0
    #: **いま作っている**特徴量の版（`FEATURE_SET`）。`model_feature_set` とは別物
    feature_set: str = ""
    #: 配った版の種類（`baseline` / `lightgbm`）。**何を配ったかが後から読める**
    model_kind: str = ""
    #: その版を**当てはめたときの**特徴量の版。**`feature_set` と一致しなくてよい**
    #: （ベースラインが読む 6 列は v0 から v4 まで変わっていない。W4-17）
    model_feature_set: str = ""
    #: **読んだポートプロファイルの版**の日付。前日の版、朝の穴では 1 つ古い版（契約 32）。
    #: 読めなければ None で、理由は `profile_reason`
    profile_date: str | None = None
    #: プロファイルで落としたバイト数（フッタを含む）。**1 周期 2 MB 未満**（J2 の完了条件 3）
    profile_bytes: int = 0
    #: プロファイルを読むのに掛かった時間。**`features_ms` の内側**に入っている
    profile_load_ms: int = 0
    #: プロファイルを読めなかった理由（`missing` か `failed:<例外の種類>`）。読めたら None
    profile_reason: str | None = None
    #: 成果物を読むのに掛かった時間。**温まっていれば 0**（版ごとに持つ。`registry.load`）
    model_load_ms: int = 0
    #: このプロセスの**最大** RSS（MB）。周期ごとではなく起動からの山（J2 の完了条件 2・5）
    rss_mb: int = 0
    #: 予測ログ（`forecast-log/`）を置けたか。`"ok"` か `"failed:<例外の種類>"`。
    #: **置けなくても推論は落とさない**ので（D-24）、失敗はここにしか出ない。
    #: 試し打ちは何も書かないので空のままになる
    forecast_log: str = ""
    #: 試し打ちで作れた予測の数（書いていないので `n_rows` は 0 になる）
    n_predicted: int = 0
    #: 試し打ちの見本 1 件
    sample: Mapping[str, object] = field(default_factory=dict)
    #: shadow を歩いた結果（W6 の PR E）。**shadow が無ければ None で、`detail` に欄を出さない**
    shadow: ShadowRun | None = None
    #: 合成器を配った周期の行き先と、森を歩いた行の数（W6 の PR H。`registry.cycle_detail`）。
    #: **合成器でなければ None で、`detail` に欄を出さない**
    composite: Mapping[str, object] | None = None
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
    | 2 | **そのポート・その時刻の履歴が足りない**（過半の水平で気候値が引けない） |
    | 3 | 過半の水平で履歴が使えた（ふつうの状態） |

    「本来の情報」はモデルによって違う。**B3 では気候値が引けたか**、LightGBM では
    常に真（欠損は木が扱うので、引けなかったに当たる状態が無い）。

    **W5-06：2 の意味が変わった。** 気候値をポートプロファイルから作るようになり
    （W5 プラン §6.4 の PR D）、引けない理由が「**そのセルに寄与した日が
    `MIN_CELL_DAYS` に満たない**」に絞られた。それまでは抽出の薄さ（1 セル 1.62 行）で
    **全ポートが 2** になっており、確度が何も区別していなかった（§2.3）。
    **新しいポート**（成果物を作ったあとに現れた）と**その曜日種別を 3 日ぶん観測して
    いないポート**が 2 になる（**2026-09-23 に下限を 2 → 3 日にした**。D-30）。

    **2 日では足りなかった。** 下限が 2 日だったころ、祝日は 98% が 3 を名乗っていたが、
    **その中身の B2 は配らないほうが当たっていた**（Brier −7.59%。W5 プラン §12 の 177）。
    **「3」が「使えた」ではなく「信じてよい」を指すよう、線を動かした。**

    **鮮度と履歴の 2 つを 1 つの数に押し込んでいるのは、分けない**（2026-09-23、D-29）。
    鮮度切れ（1）は 2 日とも 0 件で（システム全体が止まったときにしか立たない）、
    直すべきは「3 と名乗る下限」のほうだった。**LightGBM では上の「本来の情報」が常に
    真になる**ので、v1 で「履歴」を `prof_n_days` から作り直すときに改めて決める。

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
    predictor, model_load_ms = _load(port, chosen)
    at = grid_time(now)
    features_started = time.monotonic_ns()
    features = read_features(port, system_id, at, frozenset(port.list_holidays()))
    features_ms = _ms_since(features_started)
    # **合成器は、プロファイルを読めなかった周期は全セル B3 の形になる**（契約 33）
    predictor = registry.for_cycle(predictor, features.profile.edition is not None)
    # **試し打ちでも止める。** プロファイル無しの `prof_*` で出した確率は、見ても意味が無い
    _refuse_without_profile(predictor, features.profile)
    base = port.read_base_observed_at(system_id)
    stale = base is None or (at - base).total_seconds() > MAX_STALENESS_S
    forecasts = predict(predictor, system_id, at, features.ready.table, stale)
    done = Done(started, cpu_started, features_ms, model_load_ms)
    # **書いていないので 0。** 出せた数は `predicted` に出る。予測ログも書かない（W4-18）
    summary = _completed(system_id, "dry_run", base, predictor, features, done, 0, "")
    return replace(summary, n_predicted=len(forecasts), sample=_sample_of(forecasts))


@dataclass(frozen=True)
class Done:
    """走り切った 1 回の時間の記録（`_completed` に渡す）。"""

    started: datetime
    cpu_started: int
    features_ms: int
    model_load_ms: int


def _completed(
    system_id: str,
    status: str,
    base: datetime | None,
    predictor: Predictor,
    features: Features,
    done: Done,
    n_rows: int,
    forecast_log_status: str,
) -> InferSummary:
    """走り切った 1 回の要約。**`ready.stats` から何を持ち出すかを、ここ 1 か所で決める。**

    以前は試し打ちと本番でこの組み立てを 2 回書いていた。そのため PR D で足した
    `stations_without_weather` と、PR C の `stations_unreferenced` が**どちらの側にも
    入らなかった**——計算されているのに `inference_log` に出ない状態が続いた
    （W4 プラン §8.5.8）。**写す場所を 1 つにして、次に足したものが片方だけに入る道を消す。**
    """
    stats = features.ready.stats
    return InferSummary(
        system_id=system_id,
        status=status,
        base_observed_at=base,
        model_version=predictor.model_version,
        n_stations=stats.stations,
        n_skipped=sum(stats.excluded.values()),
        n_unknown_ports=predictor.unknown_ports(system_id, features.ready.table),
        n_rows=n_rows,
        duration_ms=_elapsed_ms(done.started),
        cpu_ms=_cpu_ms(done.cpu_started),
        features_ms=done.features_ms,
        reference_ms=features.stages.reference_ms,
        reference_cache=features.stages.reference_cache,
        observations_ms=features.stages.observations_ms,
        build_ms=features.stages.build_ms,
        excluded=dict(sorted(stats.excluded.items())),
        reference_date=features.reference_day.isoformat(),
        weather_issues=stats.weather_issues,
        n_without_weather=stats.stations_without_weather,
        n_unreferenced=stats.stations_unreferenced,
        feature_set=stats.feature_set,
        model_kind=predictor.kind,
        model_feature_set=predictor.feature_set,
        profile_date=stats.profile_date,
        profile_bytes=features.profile.bytes_read,
        profile_load_ms=features.profile.load_ms,
        profile_reason=features.profile.reason,
        model_load_ms=done.model_load_ms,
        rss_mb=_rss_mb(),
        forecast_log=forecast_log_status,
        composite=registry.cycle_detail(predictor, system_id, features.ready.table),
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
    # **shadow の行は周期の初めに引く。** キャッシュに残す版（active と shadow）がここで決まる
    found = _find_shadow(port)
    _retain_serving(chosen.model_version, found)
    predictor, model_load_ms = _load(port, chosen)
    # **基準時刻は 5 分格子に落とす。** 水平の起点は `generated_at` なので、
    # 書き込む `generated_at` も同じ時刻にする（W4 プラン §12 の 114）
    at = grid_time(now)
    features_started = time.monotonic_ns()
    features = read_features(port, system_id, at, frozenset(port.list_holidays()))
    features_ms = _ms_since(features_started)
    # **合成器は、プロファイルを読めなかった周期は全セル B3 の形で配る**（契約 33。止めない）。
    # LightGBM 単体は下で止まる（単体を active にしないのが決まり）
    predictor = registry.for_cycle(predictor, features.profile.edition is not None)
    _refuse_without_profile(predictor, features.profile)
    cycle = Cycle(system_id, at, base, stale=(at - base).total_seconds() > MAX_STALENESS_S)
    served = _serve(port, predictor, features, cycle)
    # **shadow は、配って予測ログを置いてから**（契約 34）。例外は `shadow` に詰め替わる
    shadow = _walk_shadow(port, found, features, cycle, predictor)
    done = Done(started, cpu_started, features_ms, model_load_ms)
    summary = _completed(
        system_id, "ok", base, predictor, features, done, served.written, served.forecast_log
    )
    return replace(summary, shadow=shadow)


@dataclass(frozen=True)
class Cycle:
    """1 周期の時刻と、観測が古いか。**active と shadow で同じ**（予測ログの 2 つの時刻も同じ）。"""

    system_id: str
    #: 5 分格子に落とした基準時刻（`generated_at`）
    at: datetime
    #: 最後に取れた観測の時刻（`base_observed_at`）
    base: datetime
    stale: bool


@dataclass(frozen=True)
class Served:
    """active を配った結果。書いた行の数と、予測ログを置けたか。"""

    written: int
    forecast_log: str


def _serve(port: InferPort, predictor: Predictor, features: Features, cycle: Cycle) -> Served:
    """active を配り、予測ログを置く。**配ってから記録する**（ログを置けなくても配信は済む）。"""
    forecasts = predict(predictor, cycle.system_id, cycle.at, features.ready.table, cycle.stale)
    payload = to_payload(forecasts, cycle.at, cycle.base, predictor.model_version)
    written = sum(port.upsert_forecasts(chunk) for chunk in batches(payload, UPSERT_BATCH))
    logged = _record_forecasts(
        port,
        forecasts,
        system_id=cycle.system_id,
        at=cycle.at,
        base=cycle.base,
        ready=features.ready,
        chosen=predictor,
    )
    return Served(written=written, forecast_log=logged)


# ── shadow（W6 の PR E、W6-06、契約 31・33・34）───────────────────
#: プロファイルを読めなかった周期に、`prof_*` を読む shadow を歩かなかった印（契約 33）。
SHADOW_NO_PROFILE: Final[str] = "skipped:no_profile"


class ShadowSameAsActiveError(RuntimeError):
    """shadow の成果物が、active と同じ版を名乗った。**歩くと active の予測ログを上書きする。**"""


@dataclass(frozen=True)
class ShadowFound:
    """周期の初めに引いた shadow の行。**引けなかったら理由だけを持つ**（active は止めない）。"""

    row: registry.Registered | None
    failure: str | None = None


def _find_shadow(port: InferPort) -> ShadowFound:
    """いまの shadow の行（**無いのが普通**）。引けなくても active は止めない。"""
    try:
        return ShadowFound(row=port.shadow_model())
    except Exception as cause:
        _log(f"shadow の行を引けませんでした: {type(cause).__name__}")
        return ShadowFound(row=None, failure=f"failed:{type(cause).__name__}")


def _retain_serving(active_version: str, found: ShadowFound) -> None:
    """**その周期に使う版だけをキャッシュに残す**（W6-02、契約 31）。

    shadow の行を引けなかった周期は触らない——一時の不調で、温まった shadow を捨てない。
    """
    if found.failure is not None:
        return
    shadow = () if found.row is None else (found.row.model_version,)
    registry.retain({active_version, *shadow})


def _walk_shadow(
    port: InferPort, found: ShadowFound, features: Features, cycle: Cycle, active: Predictor
) -> ShadowRun | None:
    """shadow を歩く。**shadow が無ければ None**（`detail` に欄を出さない）。

    **例外はここで止め、`ShadowRun` に詰め替える**（W6-06）。active はもう配って記録して
    あるので、shadow が何で落ちても active の結果は変わらない。メモリ不足で呼び出しごと
    落ちる形だけは切り離せない——だから予行演習で測る（W6 プラン §8.6）。
    """
    if found.failure is not None:
        return ShadowRun(model_version=None, status=found.failure)
    if found.row is None:
        return None
    try:
        return _shadow_once(port, found.row, features, cycle, active)
    except Exception as cause:
        _log(f"shadow に失敗しました: {cycle.system_id} / {type(cause).__name__}")
        return ShadowRun(found.row.model_version, f"failed:{type(cause).__name__}")


def _shadow_once(
    port: InferPort,
    row: registry.Registered,
    features: Features,
    cycle: Cycle,
    active: Predictor,
) -> ShadowRun:
    """shadow の成果物で、**active と同じ特徴量の表**から予測し、予測ログにだけ書く。

    行の名前は主キーなので active と重ならない。**見るのは成果物が名乗る版**である——
    同じ版を名乗る成果物で歩くと、予測ログの置き場所が active と同じになり、上書きする。
    """
    loaded, model_load_ms = _load(port, row)
    predictor = registry.for_cycle(loaded, features.profile.edition is not None)
    if predictor.model_version == active.model_version:
        raise ShadowSameAsActiveError(f"shadow の成果物が active と同じ版です: {row.model_version}")
    if features.profile.edition is None and registry.reads_profile(predictor):
        return ShadowRun(row.model_version, SHADOW_NO_PROFILE, model_load_ms=model_load_ms)
    started = time.monotonic_ns()
    forecasts = predict(predictor, cycle.system_id, cycle.at, features.ready.table, cycle.stale)
    predict_ms = _ms_since(started)
    logged = _record_forecasts(
        port,
        forecasts,
        system_id=cycle.system_id,
        at=cycle.at,
        base=cycle.base,
        ready=features.ready,
        chosen=predictor,
    )
    return ShadowRun(row.model_version, "ok", logged, model_load_ms, predict_ms)


def _load(port: InferPort, chosen: registry.Registered) -> tuple[Predictor, int]:
    """成果物を読み、**掛かった時間**も返す（温まっていれば 0 に近い。J2 の完了条件 2）。"""
    started = time.monotonic_ns()
    predictor = registry.load(port, chosen)
    return predictor, _ms_since(started)


def _record_forecasts(
    port: InferPort,
    forecasts: Sequence[Forecast],
    *,
    system_id: str,
    at: datetime,
    base: datetime,
    ready: build.Ready,
    chosen: Predictor,
) -> str:
    """配った確率を `forecast-log/` に残す（D-24、W5 プラン §6.1）。

    **置けなくても推論は落とさない。** 配ることのほうが大事で、ログは決定的なモデルと
    保存済みの入力から作り直せる（W3-18）。ただし作り直しには `weather_hourly` の
    **30 日**が要るので、**黙って落とさない**：成否は `inference_log.detail.forecast_log`
    に残る（`monitor_jobs` は `job_runs` を見るので、ここは鳴らない）。

    **覚え書きの `feature_set` は「いま作った特徴量の版」**である（`model_feature_set`
    ではない）。あとから「どの版の特徴量で出した確率か」を辿るための欄で、当てはめた
    ときの版は `model_version` から引ける（W4-17）。
    """
    try:
        file = forecast_log.build(
            forecasts,
            system_id=system_id,
            base_observed_at=base,
            generated_at=at,
            model_version=chosen.model_version,
            feature_set=ready.stats.feature_set,
        )
        port.upload_forecast_log(file.path, file.body)
    except Exception as cause:
        _log(f"予測ログを置けませんでした: {system_id} / {type(cause).__name__}")
        return f"failed:{type(cause).__name__}"
    return "ok"


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


def _ms_since(started_ns: int) -> int:
    """`time.monotonic_ns()` で測り始めてからのミリ秒。"""
    return int((time.monotonic_ns() - started_ns) // NS_PER_MS)


def _rss_mb() -> int:
    """このプロセスの**最大** RSS（MB）。周期ごとではなく、起動からの山である。

    本番（Linux）の値は `peak memory footprint` と比べられる。**手元の macOS の `ru_maxrss` は
    圧縮したページを数えず 2.6 倍低く出る**ので、手元では比べない（W5 プランの所見 168）。
    """
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak * RSS_UNIT_B // BYTES_PER_MB)


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
        # **その内訳**（W6 の PR G、W6-20）。参照は使い回せた周期（`hit`）はほぼ 0 で、
        # 05:00 JST に新しい版が置かれた次の周期と、インスタンスが替わった最初の周期が `miss`
        "reference_ms": summary.reference_ms,
        "reference_cache": summary.reference_cache,
        "observations_ms": summary.observations_ms,
        "build_ms": summary.build_ms,
        "cpu_ms": summary.cpu_ms,
        # **予測ログを置けたか**（D-24、W5 プラン §6.1）。置けなくても推論は落とさない
        # ので、**失敗はここにしか出ない**。試し打ちは何も書かないので欄ごと出ない
        **({"forecast_log": summary.forecast_log} if summary.forecast_log else {}),
        "excluded": dict(summary.excluded),
        "reference_date": summary.reference_date,
        "weather_issues": summary.weather_issues,
        # **気象格子に当たらないポート**（W4 プラン §6.4）と、**参照スナップショットに
        # 未掲載のポート**（同 §6.3c）。どちらも毎回計算していたのに、**転送を忘れて
        # いたので記録に出ていなかった**（同 §8.5.8）。0 でない値が出るのが正常である
        "stations_without_weather": summary.n_without_weather,
        "stations_unreferenced": summary.n_unreferenced,
        # **いま作っている**特徴量の版。`model_feature_set` は**当てはめたときの**版で、
        # **一致しなくてよい**（ベースラインが読む 6 列は v0 から v4 まで変わっていない。
        # W4-17）。両方を出さないと、版を上げたことが記録から読めない
        "feature_set": summary.feature_set,
        "model_kind": summary.model_kind,
        "model_feature_set": summary.model_feature_set,
        # **読んだポートプロファイルの版**（契約 32）。前日の版、朝の穴では 1 つ古い版。
        # 読めなければ null で、理由が `profile_reason` に出る（W6 の PR D）
        "profile_date": summary.profile_date,
        "profile_bytes": summary.profile_bytes,
        "profile_load_ms": summary.profile_load_ms,
        "profile_reason": summary.profile_reason,
        # **成果物を読む時間と、プロセスの山**（J2 の完了条件 2。増分を証明するのに要る）
        "model_load_ms": summary.model_load_ms,
        "rss_mb": summary.rss_mb,
        # **shadow を歩いた結果**（W6 の PR E、契約 34）。**shadow が無ければ欄ごと出さない**——
        # shadow が登録されていない本番では、記録がデプロイの前と同じになる（完了条件 1）
        **({"shadow": summary.shadow.as_detail()} if summary.shadow is not None else {}),
        # **合成器を配った周期だけ**（W6 の PR H）。`route` が `b3_only` ならプロファイルが無く
        # 全セル B3 だった（契約 33）。`forest_rows` は森を歩いた行（費用の材料。§8.11）
        **({"composite": dict(summary.composite)} if summary.composite is not None else {}),
        # **試し打ちのときだけ載せる。** 通常の推論では 0 と空になる
        **(
            {"predicted": summary.n_predicted, "sample": dict(summary.sample)}
            if summary.status == "dry_run"
            else {}
        ),
        "error": summary.error,
    }


#: `inference_log` が**列で持っている**値（`to_detail` の鍵で書く）。
#:
#: 同じ値を jsonb にも並べると 1 日 576 行ぶん無駄に膨らむ（0030 の冒頭）。
#: `ok` は `status` から決まるので、これも列の側に数える。
IN_COLUMNS: Final[frozenset[str]] = frozenset(
    {"ok", "system", "status", "base_observed_at", "model_version", "rows", "duration_ms", "error"}
)


def to_record(summary: InferSummary) -> dict[str, object]:
    """`inference_log.detail` に残す要約。**応答から、列に在るものを落としたもの。**

    **一覧は `to_detail` の 1 つだけにする。** 以前は応答と記録で別々に欄を並べて
    いたので、**片方に足してもう片方を忘れる道**があった。実際 `cpu_ms` は記録にだけ、
    `stations_without_weather` はどちらにも無い、という食い違いが起きていた
    （W4 プラン §8.5.8）。
    """
    return {key: value for key, value in to_detail(summary).items() if key not in IN_COLUMNS}
