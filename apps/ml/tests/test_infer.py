"""先回り推論（`jobs/infer.py`、W3 プラン §5.10、W4 プラン §6.3）。

**この 1 ファイルの主題は 3 つ。**

  * **同じ観測時刻に対する 2 度目は何もしない**（Vercel Cron の二重起動を無害にする）
  * **出せないポートは行を作らない**（0 や 0.5 で埋めない。**学習と同じ `features/exclude.py`**）
  * 学習した成果物と**同じ関数**で確率を出す

PR C から、除外も暦も**学習と同じ経路**（`build_now`）が決める。だからここには
「推論だけの除外規則」は無く、仕込んだ観測がそのまま効くかどうかを見る。
"""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Final

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from bikechance_ml.api import build_app
from bikechance_ml.baselines.artifact import Artifact, artifact_path, to_bytes
from bikechance_ml.eval.dataset import to_samples
from bikechance_ml.features.build import NowStats
from bikechance_ml.features.constants import FEATURE_SET, HORIZONS_MIN, MAX_STALENESS_S
from bikechance_ml.features.grid import JST
from bikechance_ml.features.weather import WeatherRow
from bikechance_ml.jobs.fit_baseline import build_artifact
from bikechance_ml.jobs.infer import (
    IN_COLUMNS,
    Forecast,
    InferSummary,
    batches,
    confidence_of,
    grid_time,
    predict,
    read_features,
    run_inference,
    to_detail,
    to_payload,
    to_record,
    to_x1000,
)
from bikechance_ml.jobs.snapshot_table import Snapshot, StationRow
from bikechance_ml.models import registry
from bikechance_ml.models.predictor import BaselinePredictor
from bikechance_ml.models.registry import (
    MODEL_BUCKET,
    FeatureSetMismatchError,
    MissingArtifactError,
    Registered,
    UnknownModelError,
    forget,
)
from tests import eval_fixture as fixture
from tests import infer_fixture as serving


@pytest.fixture(autouse=True)
def _clean_cache() -> Iterator[None]:
    """**成果物のキャッシュはモジュールに残る。** 検査ごとに捨てる。

    本番では版が変われば入れ替わるので問題にならないが、検査は同じ版名で
    中身の違う口を使うので、持ち越すと前の検査の答えが出る。
    """
    forget()
    yield
    forget()


OPEN, SUSPENDED, MISSING = 7, 1, -1
NOW = datetime(2026, 9, 9, 10, 4, tzinfo=JST).astimezone(UTC)
#: 基準時刻は **5 分格子に落ちる**（`grid_time`）。特徴量も予測もこの時刻で作る。
AT = grid_time(NOW)
BASE = AT - timedelta(minutes=2)

STATIONS = serving.STATIONS
STATION_IDS = tuple(one[0] for one in STATIONS)


def scenario() -> list[dict[str, object]]:
    return [
        fixture.row(day, system, station, horizon, bikes, 9 - bikes, 1 if bikes else 0, 1)
        for day in fixture.DAYS
        for system in ("hellocycling", "docomo-cycle")
        for horizon in HORIZONS_MIN
        for station, bikes in (("a", 0), ("b", 1), ("c", 7))
    ]


def artifact() -> Artifact:
    rows = scenario()
    ports = tuple(sorted({f"{one['system_id']}/{one['station_id']}" for one in rows}))
    return build_artifact(to_samples(fixture.to_table(rows)), ports, fixture.DAYS)


ARTIFACT = artifact()


# ── 予測 ──────────────────────────────────────────────────────
def features(port: "FakePort", system_id: str = "hellocycling") -> pa.Table:
    """**本番と同じ経路**で特徴量を作る（参照スナップショットと 2 つの窓）。"""
    return read_features(port, system_id, AT, frozenset())[1].table


def forecasts(
    rows: Sequence[tuple[str, int, int, int]] = STATIONS,
    *,
    base: datetime = BASE,
    system_id: str = "hellocycling",
) -> tuple[Forecast, ...]:
    port = ready_port(rows)
    stale = (AT - base).total_seconds() > MAX_STALENESS_S
    return predict(BaselinePredictor(ARTIFACT), system_id, AT, features(port, system_id), stale)


def test_one_row_per_port_with_all_horizons() -> None:
    result = forecasts()
    assert len(result) == len(STATIONS)
    for one in result:
        assert len(one.p_bike_x1000) == len(HORIZONS_MIN)
        assert len(one.p_dock_x1000) == len(HORIZONS_MIN)


def test_probabilities_are_integers_in_range() -> None:
    """0026 の検査（0〜1000）に落ちない値だけを出す。"""
    for one in forecasts():
        assert all(0 <= value <= 1000 for value in one.p_bike_x1000)
        assert all(isinstance(value, int) for value in one.p_bike_x1000)


def test_more_bikes_means_a_higher_chance_of_renting() -> None:
    """**バケツで条件づけているので単調になる。** 逆転したら参照表の引き方を疑う。"""
    result = {one.station_id: one for one in forecasts()}
    assert result["c"].p_bike_x1000[0] >= result["b"].p_bike_x1000[0]
    assert result["b"].p_bike_x1000[0] >= result["a"].p_bike_x1000[0]


def test_unobserved_ports_have_no_row() -> None:
    """**観測されなかったポートは行を作らない**（`-1` を 0 と読まない）。"""
    rows = (*STATIONS, ("d", MISSING, MISSING, OPEN))
    assert [one.station_id for one in forecasts(rows)] == ["a", "b", "c"]


def test_suspended_ports_have_no_row() -> None:
    """**貸出も返却も止まっていれば出さない**（学習の除外規則そのまま）。"""
    rows = (*STATIONS, ("d", 3, 3, SUSPENDED))
    assert [one.station_id for one in forecasts(rows)] == ["a", "b", "c"]


def test_the_phantom_station_has_no_row() -> None:
    """実在しないポート（ドコモ `5753`）。**名前ではなく ID で弾く。**"""
    rows = (*STATIONS, ("5753", 3, 3, OPEN))
    assert "5753" not in [one.station_id for one in forecasts(rows, system_id="docomo-cycle")]
    # HELLO の同じ ID は落とさない（ID だけで弾くのは 1 系統ぶん）
    assert "5753" in [one.station_id for one in forecasts(rows)]


def test_no_usable_port_gives_no_forecast() -> None:
    assert forecasts((("a", 3, 3, SUSPENDED),)) == ()


def test_stale_feed_lowers_the_confidence() -> None:
    """鮮度が落ちたら confidence を下げる（0026 の列）。"""
    old = AT - timedelta(seconds=MAX_STALENESS_S + 1)
    assert {one.confidence for one in forecasts(base=old)} == {1}


def test_confidence_rules() -> None:
    """**過半の水平で本来の情報が使えたら 3**（B3 では気候値が効いた水平）。"""
    half = len(HORIZONS_MIN) // 2
    assert confidence_of(stale=True, informed_horizons=len(HORIZONS_MIN)) == 1
    assert confidence_of(stale=False, informed_horizons=0) == 2
    assert confidence_of(stale=False, informed_horizons=half) == 2
    assert confidence_of(stale=False, informed_horizons=half + 1) == 3


def test_probability_rounding_stays_in_range() -> None:
    import numpy as np

    values = np.array([-0.1, 0.0, 0.0004, 0.5, 0.9999, 1.0, 1.2])
    assert to_x1000(values).tolist() == [0, 0, 0, 500, 1000, 1000, 1000]


# ── 書き出す形 ────────────────────────────────────────────────
def test_payload_matches_the_table() -> None:
    payload = to_payload(forecasts(), AT, BASE, "m1")
    assert len(payload) == len(STATIONS)
    first = payload[0]
    assert first["horizons_min"] == list(HORIZONS_MIN)
    assert first["model_version"] == "m1"
    # **書き込む `generated_at` は 5 分格子の基準時刻**（水平の起点。§12 の 114）
    assert first["generated_at"] == AT.isoformat()
    assert first["base_observed_at"] == BASE.isoformat()


def test_batches_split_without_losing_rows() -> None:
    rows = [{"n": index} for index in range(2500)]
    chunks = list(batches(rows, 1000))
    assert [len(one) for one in chunks] == [1000, 1000, 500]
    assert [one for chunk in chunks for one in chunk] == rows


def test_model_path_uses_the_version() -> None:
    """**バケットは `models`**（`gbfs-parquet` は Parquet の MIME しか許さない）。"""
    assert MODEL_BUCKET == "models"
    assert artifact_path("baseline-b3-v0-20260909") == "baseline/baseline-b3-v0-20260909.json.gz"


# ── 実行 ──────────────────────────────────────────────────────
@dataclass
class FakePort:
    """`InferPort` の代役。**掴んだ回数と書いた行を覚える。**

    読ませるものは本番と同じ 3 つ：成果物・**前日の参照スナップショット**・観測の窓。
    """

    base: datetime | None = BASE
    rows: Sequence[tuple[str, int, int, int]] = STATIONS
    claims: list[tuple[str, datetime]] = field(default_factory=list)
    finished: list[tuple[int, str, int]] = field(default_factory=list)
    details: list[Mapping[str, object] | None] = field(default_factory=list)
    written: list[Mapping[str, object]] = field(default_factory=list)
    reads: list[tuple[str, datetime, datetime]] = field(default_factory=list)
    weather_reads: list[tuple[datetime, datetime]] = field(default_factory=list)
    downloads: list[str] = field(default_factory=list)
    body: bytes | None = None
    next_id: int = 1
    #: `model_versions` の `active` の行。**推論はここから版を引く**
    registered: Registered | None = field(
        default_factory=lambda: Registered(
            model_version=ARTIFACT.model_version,
            kind="baseline",
            feature_set=ARTIFACT.feature_set,
            artifact_path=artifact_path(ARTIFACT.model_version),
            status="active",
        )
    )
    #: 候補（`?model=` で名指しするときだけ引かれる）
    candidate: Registered | None = None
    #: LightGBM の成果物（`lightgbm/` のパスに置かれたことにする）
    lightgbm_body: bytes | None = None

    def read_base_observed_at(self, system_id: str) -> datetime | None:
        return self.base

    def list_stations(self, system_id: str) -> tuple[StationRow, ...]:
        return serving.ledger([one[0] for one in self.rows])

    def list_snapshots(
        self, system_id: str, start: datetime, end: datetime
    ) -> tuple[Snapshot, ...]:
        """**頼まれた窓を埋めて返す。** どの窓を読みに来たかは `reads` に残る。"""
        self.reads.append((system_id, start, end))
        return serving.fill(start, end, self.rows)

    def list_holidays(self) -> tuple[date, ...]:
        return ()

    def active_model(self) -> Registered | None:
        """**登録簿がいまの版を返す**（W4-08。環境変数からの移行）。"""
        return self.registered

    def find_model(self, model_version: str) -> Registered | None:
        if self.registered is not None and self.registered.model_version == model_version:
            return self.registered
        return (
            self.candidate
            if (self.candidate is not None and self.candidate.model_version == model_version)
            else None
        )

    def list_weather(self, start: datetime, end: datetime) -> tuple[WeatherRow, ...]:
        """**頼まれた窓だけ**返す（どの窓を読みに来たかは `weather_reads` に残る）。"""
        self.weather_reads.append((start, end))
        return serving.weather_within(serving.weather_rows(AT), start, end)

    def download(self, bucket: str, path: str) -> bytes | None:
        if bucket == MODEL_BUCKET:
            return self.lightgbm_body if path.startswith("lightgbm/") else self.body
        # **頼まれた日の版**を返す（どの日を読みに来たかは `downloads` に残る）
        self.downloads.append(path)
        return serving.reference_files(serving.day_of(path), self.rows).get(path)

    def begin_inference(
        self, system_id: str, base_observed_at: datetime, model_version: str
    ) -> int | None:
        if (system_id, base_observed_at) in self.claims:
            return None
        self.claims.append((system_id, base_observed_at))
        self.next_id += 1
        return self.next_id

    def finish_inference(
        self,
        run_id: int,
        status: str,
        n_rows: int,
        duration_ms: int,
        error: str | None = None,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        self.finished.append((run_id, status, n_rows))
        self.details.append(detail)

    def upsert_forecasts(self, rows: Sequence[Mapping[str, object]]) -> int:
        self.written.extend(rows)
        return len(rows)


def ready_port(rows: Sequence[tuple[str, int, int, int]] = STATIONS) -> FakePort:
    return FakePort(body=to_bytes(ARTIFACT), rows=rows)


def test_a_full_cycle_writes_one_row_per_port() -> None:
    port = ready_port()
    summary = run_inference(port, "hellocycling", NOW)
    assert summary.status == "ok"
    assert summary.n_rows == len(STATIONS)
    assert len(port.written) == len(STATIONS)
    assert port.finished == [(2, "ok", len(STATIONS))]


def test_the_same_observation_is_not_inferred_twice() -> None:
    """**Vercel Cron の二重起動は無害**（`inference_log` の一意制約で掴む）。"""
    port = ready_port()
    run_inference(port, "hellocycling", NOW)
    again = run_inference(port, "hellocycling", NOW)
    assert again.status == "skipped"
    assert again.n_rows == 0
    assert len(port.written) == len(STATIONS)


def test_a_new_observation_is_inferred() -> None:
    port = ready_port()
    run_inference(port, "hellocycling", NOW)
    port.base = BASE + timedelta(minutes=5)
    later = run_inference(port, "hellocycling", NOW)
    assert later.status == "ok"


def test_no_observation_yet_is_skipped_without_claiming() -> None:
    port = FakePort(base=None, body=to_bytes(ARTIFACT))
    summary = run_inference(port, "hellocycling", NOW)
    assert summary.status == "skipped"
    assert port.claims == []


def test_a_missing_artifact_is_recorded_as_failed() -> None:
    """**代わりの値をでっち上げない。** 失敗として記録する。

    登録簿には在るが Storage に無い、という食い違い。掴んだあとで落ちるので、
    `inference_log` に `failed` が残る。
    """
    port = FakePort(body=None)
    summary = run_inference(port, "hellocycling", NOW)
    assert summary.status == "failed"
    assert summary.error == MissingArtifactError.__name__
    assert port.finished == [(2, "failed", 0)]


def test_an_unknown_version_is_refused_before_claiming() -> None:
    """**登録簿に無い版は掴む前に止める**（`inference_log` に行を作らない）。"""
    port = ready_port()
    with pytest.raises(UnknownModelError):
        run_inference(port, "hellocycling", NOW, "知らない版")
    assert port.claims == []


def test_detail_is_json_safe() -> None:
    port = ready_port()
    detail = to_detail(run_inference(port, "hellocycling", NOW))
    assert detail["ok"] is True
    assert detail["system"] == "hellocycling"
    assert isinstance(detail["base_observed_at"], str)


def test_detail_carries_only_the_exception_name() -> None:
    """**例外の文言は要求 URL を抱えていることがある**（CLAUDE.md §5）。"""
    detail = to_detail(run_inference(FakePort(body=None), "hellocycling", NOW))
    assert detail["error"] == "MissingArtifactError"
    assert "http" not in str(detail).lower()


def test_unknown_port_still_gets_a_baseline() -> None:
    """成果物に無いポートは**気候値だけ引けない**。B1 には落とせる。"""
    result = forecasts((("zzz", 2, 7, OPEN),))
    assert len(result) == 1
    assert result[0].confidence == 2


def test_unknown_port_does_not_borrow_another_ports_climatology() -> None:
    """**知らないポートは `port = -1` になり、負の添字は表の末尾に回り込む**（§12 の 110）。

    末尾は `hellocycling/c`＝いつも 7 台あるポートなので、直す前は**空のポートが
    「41.7% の確率で借りられる」**と言われていた。気候値の層が外れれば、同じ 0 台の
    既知ポート `a` とまったく同じ値になる。

    **上の検査（`confidence == 2`）はこれを捕まえられなかった。** 信頼度は「過半の水平で
    気候値が効いたか」しか見ておらず、**引いた先が別のポートでも 2 のまま**だったからで
    ある。確率そのものを見ないと分からない。
    """
    known = forecasts((("a", 0, 9, OPEN),))
    fresh = forecasts((("zzz", 0, 9, OPEN),))
    assert fresh[0].p_bike_x1000 == known[0].p_bike_x1000
    assert fresh[0].p_dock_x1000 == known[0].p_dock_x1000
    assert max(fresh[0].p_bike_x1000) < 100  # 直す前は 417


# ── 成果物のドリフトを数える（§12 の 110、§14.2 の 3）─────────
def test_unknown_ports_counts_only_what_is_predicted() -> None:
    """**成果物に無い、予測対象のポート**だけを数える。

    止まっているポートや幻のポートは、そもそも成果物に無くてよい。数に入れると
    「ドリフトが増えた」と誤読する。
    """
    rows = (
        ("zzz", 2, 7, OPEN),  # 予測対象で成果物に無い ← 数える
        ("a", 2, 7, OPEN),  # 成果物に在る
        ("yyy", 2, 7, SUSPENDED),  # 止まっている ← 行が作られないので数えない
    )
    table = features(ready_port(rows))
    assert BaselinePredictor(ARTIFACT).unknown_ports("hellocycling", table) == 1


def test_the_phantom_station_is_not_counted_as_drift() -> None:
    """幻のポート（ドコモ `5753`）は成果物に無くて当たり前である。"""
    table = features(ready_port((("5753", 2, 7, OPEN),)), "docomo-cycle")
    assert BaselinePredictor(ARTIFACT).unknown_ports("docomo-cycle", table) == 0


def test_no_drift_when_every_port_is_in_the_artifact() -> None:
    predictor = BaselinePredictor(ARTIFACT)
    assert predictor.unknown_ports("hellocycling", features(ready_port())) == 0


def test_the_count_reaches_the_response_and_the_record() -> None:
    """**応答にも記録にも出す。** 数えても出さなければ気づけない。"""
    port = ready_port()
    summary = run_inference(port, "hellocycling", NOW)
    assert summary.n_unknown_ports == 0
    assert to_detail(summary)["unknown_ports"] == 0
    assert len(port.details) == 1
    recorded = port.details[0]
    assert recorded is not None
    assert recorded["stations"] == 3
    assert recorded["skipped"] == 0
    assert recorded["unknown_ports"] == 0


def test_the_record_does_not_repeat_the_columns() -> None:
    """`inference_log` に列で在るものを jsonb にも並べない（0030 の冒頭）。

    `cpu_ms` だけは列にしない側に置く。開発プラン §5.3 の DDL には在るが、実装では
    `detail` に入れる（列を足さずに済む。§13.7 の判断）。
    """
    assert set(to_record(_summary(cpu_ms=42))) == {
        "stations",
        "skipped",
        "unknown_ports",
        "cpu_ms",
        "features_ms",
        "excluded",
        "reference_date",
        "weather_issues",
        "stations_without_weather",
        "stations_unreferenced",
        "feature_set",
        "model_kind",
        "model_feature_set",
    }
    assert to_record(_summary(cpu_ms=42))["cpu_ms"] == 42


def test_the_record_is_the_response_minus_the_columns() -> None:
    """**一覧は 1 つだけ。** 記録は応答から列に在るものを落としたものである。

    以前は応答と記録で別々に欄を並べていたので、**片方に足してもう片方を忘れる道**が
    あった（W4 プラン §8.5.8）。ここで「射影である」ことを固定しておけば、
    次に欄を足したときも両方に入る。
    """
    summary = _summary(cpu_ms=42)
    detail = to_detail(summary)
    assert set(to_record(summary)) == set(detail) - IN_COLUMNS
    assert set(detail) >= IN_COLUMNS, "列の側の鍵が応答に無い（名前を変えたら両方直す）"


def _summary(*, cpu_ms: int) -> InferSummary:
    return InferSummary(
        system_id="hellocycling",
        status="ok",
        base_observed_at=BASE,
        model_version="m1",
        n_stations=10,
        n_skipped=1,
        n_unknown_ports=2,
        n_rows=9,
        duration_ms=123,
        cpu_ms=cpu_ms,
    )


# ── 所要 CPU（§13.7 の判断）─────────────────────────────────
def test_features_ms_is_reported() -> None:
    """**推論の所要の大半は特徴量づくり**（W4 プラン §6.3 の完了条件）。"""
    port = ready_port()
    summary = run_inference(port, "hellocycling", NOW)
    assert summary.features_ms > 0
    assert to_detail(summary)["features_ms"] == summary.features_ms
    recorded = port.details[0]
    assert recorded is not None and recorded["features_ms"] == summary.features_ms
    # 除外の内訳も残す（なぜ出せなかったかが分かる）
    assert isinstance(recorded["excluded"], dict)
    # **どの版の参照データを使ったか**も残す（00:00〜05:00 JST は 1 つ古い版になる）
    yesterday = (AT.astimezone(JST).date() - timedelta(days=1)).isoformat()
    assert recorded["reference_date"] == yesterday


# ── 内訳がぜんぶ記録に届くこと（W4 プラン §8.5.8）───────────────
#: `NowStats` の欄のうち、**わざと記録に出さないもの**と、その理由。
#:
#: **ここに書いていない欄は、記録に出ていなければならない。** 下の検査がそれを固定する。
#: 出さない judgement をこの表に書かせることで、「うっかり落ちた」と「出さないと決めた」を
#: 区別する——`stations_without_weather` が 1 度も記録に出なかったのは前者だった。
NOT_RECORDED: Final[Mapping[str, str]] = {
    "at": "基準時刻は `base_observed_at` と `generated_at` が列で持っている",
    "predictable": "`stations - skipped` で出る（同じ数を 2 か所に置かない）",
    "rows": "`n_rows` として列に在る",
}


class TroubledPort(FakePort):
    """**0 でない内訳が出る代役。**

    値が 0 のままだと、「本当に 0 だった」のか「配線を忘れて既定値のまま」なのかを
    検査が区別できない。**本番で実際に起きている 2 つの状況**を作る。

      * **天気を別の格子へずらす**（§12 の 119。要求した格子と計算した格子が食い違い、
        4 ポートが恒久的に NULL になっている）→ 全ポートが「天気なし」になる
      * **参照スナップショットから 1 ポート落とす**（§6.3c。新しいポートは載るまで
        予測が出ない）→ 1 ポートが「未掲載」になる
    """

    def list_weather(self, start: datetime, end: datetime) -> tuple[WeatherRow, ...]:
        rows = super().list_weather(start, end)
        return tuple(replace(one, cell_lat_idx=one.cell_lat_idx + 10) for one in rows)

    def download(self, bucket: str, path: str) -> bytes | None:
        if bucket == MODEL_BUCKET:
            return super().download(bucket, path)
        self.downloads.append(path)
        # 台帳の最後の 1 ポートを参照に載せない
        return serving.reference_files(serving.day_of(path), self.rows[:-1]).get(path)


def test_every_now_stat_reaches_the_record() -> None:
    """**`NowStats` が計算した内訳は、出さないと決めたもの以外すべて記録に届く。**

    `stations_without_weather`（PR D）も `stations_unreferenced`（PR C）も、
    計算されて `as_dict()` にも入っていたのに、**`inference_log` には 1 度も出て
    いなかった**（W4 プラン §8.5.8）。§6.3c は「数は `stations_unreferenced` に出る」と
    書いていたが、出ていなかった。

    **数え上げで固定する。** 欄を足して転送を忘れれば、ここが落ちる。
    """
    port = TroubledPort(body=to_bytes(ARTIFACT))
    run_inference(port, "hellocycling", NOW)
    recorded = port.details[0]
    assert recorded is not None
    missing = [
        name
        for name in NowStats.__dataclass_fields__
        if name not in recorded and name not in NOT_RECORDED
    ]
    assert missing == [], f"計算しているのに記録に出ていない内訳: {missing}"


def test_the_ports_without_weather_and_reference_are_recorded() -> None:
    """**2 つの数が、0 でない値で記録に届く。**

    **0 のままの代役では検査にならない**（配線を忘れても既定の 0 と区別できない）。
    `TroubledPort` が本番で起きている 2 つの状況を作り、**その数がそのまま出る**ことを
    確かめる。
    """
    port = TroubledPort(body=to_bytes(ARTIFACT))
    summary = run_inference(port, "hellocycling", NOW)
    recorded = port.details[0]
    assert recorded is not None

    # **計算元と突き合わせる。** 要約どうしで比べると、2 つを取り違えていても
    # 記録と要約が同じ値になるので気づけない
    _, ready = read_features(TroubledPort(body=to_bytes(ARTIFACT)), "hellocycling", AT, frozenset())
    stats = ready.stats

    # 仕込みが効いていること。**2 つが違う数**でなければ取り違えを見抜けない
    assert stats.stations_without_weather > 0, "天気なしのポートを作れていない（仕込みの誤り）"
    assert stats.stations_unreferenced > 0, "未掲載のポートを作れていない（仕込みの誤り）"
    assert stats.stations_without_weather != stats.stations_unreferenced, (
        "2 つが同じ数だと取り違えを見抜けない"
    )

    assert recorded["stations_without_weather"] == stats.stations_without_weather
    assert recorded["stations_unreferenced"] == stats.stations_unreferenced
    assert to_detail(summary)["stations_without_weather"] == stats.stations_without_weather
    assert to_detail(summary)["stations_unreferenced"] == stats.stations_unreferenced


def test_the_diagnostics_are_zero_when_nothing_is_wrong() -> None:
    """**何も起きていなければ 0。** 常に 0 でない数を出しているのではないことを見る。"""
    port = ready_port()
    summary = run_inference(port, "hellocycling", NOW)
    assert summary.n_without_weather == 0
    assert summary.n_unreferenced == 0


def test_both_feature_sets_are_recorded() -> None:
    """**作った版と、当てはめたときの版は別物で、両方要る**（W4-17）。

    ベースラインが読む 6 列は v0 から v3 まで変わっていないので、`model_feature_set`
    が v0 のまま `feature_set` が v3 になるのが正常である。**片方しか出さないと、
    版を上げたことが記録から読めない。**
    """
    port = ready_port()
    summary = run_inference(port, "hellocycling", NOW)
    recorded = port.details[0]
    assert recorded is not None
    assert recorded["feature_set"] == FEATURE_SET
    assert recorded["model_feature_set"] == summary.model_feature_set
    assert "feature_set" in recorded and "model_feature_set" in recorded


def test_the_dry_run_reports_the_same_diagnostics() -> None:
    """**試し打ちでも同じ内訳が出る。** 組み立てが 1 か所になっている証拠になる。

    以前は試し打ちと本番で要約を別々に組み立てていた。**片方だけに欄を足す道**が
    在ったので、ここで両方が同じ経路を通ることを固定する。
    """
    from bikechance_ml.models import artifact as lightgbm_artifact
    from tests.test_model_artifact import ARTIFACT as LGBM

    port = ready_port()
    port.candidate = Registered(
        model_version=LGBM.model_version,
        kind="lightgbm",
        feature_set=LGBM.feature_set,
        artifact_path=lightgbm_artifact.artifact_path(LGBM.model_version),
        status="candidate",
    )
    port.lightgbm_body = lightgbm_artifact.to_bytes(LGBM)

    summary = run_inference(port, "hellocycling", NOW, LGBM.model_version)
    assert summary.status == "dry_run"
    detail = to_detail(summary)
    for name in ("stations_without_weather", "stations_unreferenced", "feature_set"):
        assert name in detail, f"試し打ちの応答に {name} が無い"
    assert detail["feature_set"] == FEATURE_SET
    # **試し打ちは記録しない**ので、`inference_log` には行が増えない（W4-18）
    assert port.details == []


def test_cpu_time_is_measured_and_reported() -> None:
    """**費用は実時間ではなく Active CPU で決まる**（開発プラン §4.5a）。

    `duration_ms` は Storage の取得と PostgREST の往復を含むので、計算に使った時間を
    分けて記録する。
    """
    port = ready_port()
    summary = run_inference(port, "hellocycling", NOW)
    assert summary.cpu_ms >= 0
    assert to_detail(summary)["cpu_ms"] == summary.cpu_ms
    recorded = port.details[0]
    assert recorded is not None and recorded["cpu_ms"] == summary.cpu_ms


def test_cpu_time_is_reported_even_when_nothing_was_inferred() -> None:
    """掴めなかった回も測る。**二重起動の抑止にも CPU は使っている。**"""
    port = ready_port()
    port.base = None
    summary = run_inference(port, "hellocycling", NOW)
    assert summary.status == "skipped"
    assert summary.cpu_ms >= 0


def test_horizons_are_ordered_as_the_contract_says() -> None:
    payload = to_payload(forecasts(), AT, BASE, "m1")
    assert payload[0]["horizons_min"] == sorted(HORIZONS_MIN)
    assert HORIZONS_MIN[0] == 5


def test_probabilities_decay_with_the_horizon_for_an_empty_port() -> None:
    """**台数 0 のポートは、先に行くほど「借りられる」確率が上がる。**

    5 分後に誰かが返す確率より、3 時間後に誰かが返す確率のほうが高い。
    ここが逆なら水平の並びが崩れている。
    """
    result = {one.station_id: one for one in forecasts()}
    empty = result["a"].p_bike_x1000
    assert empty[-1] >= empty[0]


@pytest.mark.parametrize("system", ["hellocycling", "docomo-cycle"])
def test_both_systems_can_be_predicted(system: str) -> None:
    assert len(forecasts(system_id=system)) == len(STATIONS)


def test_it_falls_back_to_an_older_reference_before_dawn() -> None:
    """**00:00〜05:00 JST は前日の版がまだ無い**（05:00 に書かれる。§12 の 118）。

    そこで止めると 1 日の 5 分の 1 で予測が出なくなる。1 つ古い版に下がり、
    **どちらを使ったかを記録する**。
    """

    @dataclass
    class OnlyOlder(FakePort):
        """前日の版がまだ置かれていない朝。"""

        def download(self, bucket: str, path: str) -> bytes | None:
            if bucket != MODEL_BUCKET and serving.day_of(path) == _yesterday():
                return None
            return super().download(bucket, path)

    port = OnlyOlder(body=to_bytes(ARTIFACT))
    reference_day, ready = read_features(port, "hellocycling", AT, frozenset())
    assert reference_day == _yesterday() - timedelta(days=1)
    assert ready.table.num_rows > 0


def _yesterday() -> date:
    return AT.astimezone(JST).date() - timedelta(days=1)


def test_it_reads_yesterdays_reference() -> None:
    """**学習と同じ規則で「前日の版」を読む**（W3 プラン §14.3）。

    「学習は前日、推論は最新」にすると、そこが新しい train/serve skew になる。
    """
    port = ready_port()
    features(port)
    yesterday = (AT.astimezone(JST).date() - timedelta(days=1)).isoformat()
    assert port.downloads, "参照スナップショットを読んでいない"
    assert all(f"date={yesterday}" in path for path in port.downloads)


def test_it_reads_the_two_windows_of_its_own_system_and_one_of_the_other() -> None:
    """**他系統は近傍のためだけに居る。** 履歴まで読むと所要が倍になる（§6.3）。"""
    port = ready_port()
    features(port)
    own = [one for one in port.reads if one[0] == "hellocycling"]
    other = [one for one in port.reads if one[0] == "docomo-cycle"]
    assert len(own) == 2
    assert len(other) == 1
    # 他系統の窓は、自系統のどの窓よりも短い
    span = min((end - start) for _, start, end in own)
    assert (other[0][2] - other[0][1]) < span


# ── 入口（`/ml/infer/{system}`）────────────────────────────────
@contextmanager
def lend(port: FakePort) -> Iterator[FakePort]:
    yield port


def client_for(port: FakePort, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    return TestClient(build_app(make_infer_port=lambda: lend(port)))


AUTH = {"Authorization": "Bearer s3cret"}


def test_route_requires_the_cron_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """**DB にもログにも何も書かずに弾く。**"""
    port = ready_port()
    response = client_for(port, monkeypatch).get("/ml/infer/hellocycling")
    assert response.status_code == 401
    assert port.claims == []


def test_route_rejects_an_unknown_system(monkeypatch: pytest.MonkeyPatch) -> None:
    """**DB に問い合わせる前に弾く。**"""
    port = ready_port()
    response = client_for(port, monkeypatch).get("/ml/infer/nope", headers=AUTH)
    assert response.status_code == 400
    assert response.json()["title"] == "unknown_system"
    assert port.claims == []


def test_route_needs_an_active_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """**配る版は登録簿が決める**（W4-08）。登録が無ければ 500 で、黙って配らない。"""
    port = ready_port()
    port.registered = None
    response = client_for(port, monkeypatch).get("/ml/infer/hellocycling", headers=AUTH)
    assert response.status_code == 500
    assert response.json()["title"] == "misconfigured"
    assert port.claims == []


def test_route_can_try_a_candidate_without_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    """**候補は試し打ちになる。** どこにも書かず、確率だけ返す（W4-07）。"""
    port = ready_port()
    port.candidate = Registered(
        model_version="lgbm-v0-test",
        kind="baseline",  # 中身はベースラインの成果物を使い回す（配線だけ見る）
        feature_set=ARTIFACT.feature_set,
        artifact_path=artifact_path(ARTIFACT.model_version),
        status="candidate",
    )
    response = client_for(port, monkeypatch).get(
        "/ml/infer/hellocycling?model=lgbm-v0-test", headers=AUTH
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "dry_run"
    assert body["rows"] == 0
    assert body["predicted"] == len(STATIONS)
    assert len(body["sample"]["p_bike_x1000"]) == len(HORIZONS_MIN)
    # **掴んでいない・書いていない**（本物の推論を邪魔しない）
    assert port.claims == []
    assert port.written == []
    assert port.finished == []


def test_route_runs_and_returns_the_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    port = ready_port()
    response = client_for(port, monkeypatch).get("/ml/infer/hellocycling", headers=AUTH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["status"] == "ok"
    assert body["rows"] == len(STATIONS)


def test_route_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """**Cron の二重起動でも 200。** 2 度目は何も書かない。"""
    port = ready_port()
    client = client_for(port, monkeypatch)
    client.get("/ml/infer/hellocycling", headers=AUTH)
    again = client.get("/ml/infer/hellocycling", headers=AUTH)
    assert again.status_code == 200
    assert again.json()["status"] == "skipped"
    assert len(port.written) == len(STATIONS)


def test_route_reports_a_missing_artifact_as_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """**500 を返し、失敗として記録する。**

    500 にするのは Vercel Observability のエラー率検知を効かせるため（CLAUDE.md §3）。
    掴んだ後の失敗なので、`inference_log` にも `failed` が残る。
    """
    port = FakePort(body=None)
    response = client_for(port, monkeypatch).get("/ml/infer/hellocycling", headers=AUTH)
    assert response.status_code == 500
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"] == "MissingArtifactError"
    assert port.finished == [(2, "failed", 0)]


# ── 成果物のキャッシュ ────────────────────────────────────────
def test_artifact_is_fetched_once_per_version() -> None:
    """**3.2 MB を 5 分毎に取り直さない**（開発プラン §8.2）。"""
    port = CountingPort(body=to_bytes(ARTIFACT))
    registry.load(port, _registered(ARTIFACT.model_version))
    registry.load(port, _registered(ARTIFACT.model_version))
    assert port.fetches == 1


def test_a_new_version_replaces_the_cache() -> None:
    port = CountingPort(body=to_bytes(ARTIFACT))
    registry.load(port, _registered(ARTIFACT.model_version))
    registry.load(port, _registered("another-version"))
    assert port.fetches == 2


def _registered(model_version: str) -> Registered:
    return Registered(
        model_version=model_version,
        kind="baseline",
        feature_set=ARTIFACT.feature_set,
        artifact_path=artifact_path(model_version),
        status="active",
    )


@dataclass
class CountingPort(FakePort):
    """成果物を取りに行った回数を数えるだけの口。"""

    fetches: int = 0

    def download(self, bucket: str, path: str) -> bytes | None:
        self.fetches += 1
        return self.body


# ── LightGBM の版で通す（W4 プラン §6.5 の完了条件）─────────
def test_a_lightgbm_candidate_produces_probabilities() -> None:
    """**特徴量 → 行列 → 木 → 確率**が 1 本で通る。

    完了条件の「候補の版で確率が出る」をここで固定する。成果物は
    `tests/test_model_artifact.py` が作った小さな森で、**中身は乱数**なので値に意味は
    無い——見るのは**経路が通ること**と、**確率が 0〜1000 に収まること**である。
    """
    from bikechance_ml.models import artifact as lightgbm_artifact
    from tests.test_model_artifact import ARTIFACT as LGBM

    port = ready_port()
    port.candidate = Registered(
        model_version=LGBM.model_version,
        kind="lightgbm",
        feature_set=LGBM.feature_set,
        artifact_path=lightgbm_artifact.artifact_path(LGBM.model_version),
        status="candidate",
    )
    port.lightgbm_body = lightgbm_artifact.to_bytes(LGBM)

    summary = run_inference(port, "hellocycling", NOW, LGBM.model_version)
    assert summary.status == "dry_run"
    assert summary.model_kind == "lightgbm"
    assert summary.n_predicted == len(STATIONS)
    # **全ポート共通のモデルなので「知らないポート」が無い**（B2 と違う）
    assert summary.n_unknown_ports == 0
    probabilities = summary.sample["p_bike_x1000"]
    assert isinstance(probabilities, list)
    assert len(probabilities) == len(HORIZONS_MIN)
    assert all(0 <= one <= 1000 for one in probabilities)
    # **どこにも書いていない**
    assert port.claims == []
    assert port.written == []


def test_a_lightgbm_model_refuses_another_feature_set() -> None:
    """**61 列すべてを読むので、版が違えば配らない**（W4-17）。"""
    from dataclasses import replace as _replace

    from bikechance_ml.models import artifact as lightgbm_artifact
    from tests.test_model_artifact import ARTIFACT as LGBM

    stale = _replace(LGBM, feature_set="v1")
    port = ready_port()
    port.candidate = Registered(
        model_version=stale.model_version,
        kind="lightgbm",
        feature_set="v1",
        artifact_path=lightgbm_artifact.artifact_path(stale.model_version),
        status="candidate",
    )
    port.lightgbm_body = lightgbm_artifact.to_bytes(stale)
    with pytest.raises(FeatureSetMismatchError):
        run_inference(port, "hellocycling", NOW, stale.model_version)
