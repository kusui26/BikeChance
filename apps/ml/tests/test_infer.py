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
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from bikechance_ml.api import MODEL_VERSION_ENV, build_app
from bikechance_ml.baselines.artifact import Artifact, to_bytes
from bikechance_ml.eval.dataset import to_samples
from bikechance_ml.features.constants import HORIZONS_MIN, MAX_STALENESS_S
from bikechance_ml.features.grid import JST
from bikechance_ml.jobs.fit_baseline import build_artifact
from bikechance_ml.jobs.infer import (
    MODEL_BUCKET,
    Forecast,
    InferSummary,
    MissingArtifactError,
    batches,
    confidence_of,
    forget_artifacts,
    grid_time,
    load_artifact,
    model_path,
    predict,
    read_features,
    run_inference,
    to_detail,
    to_payload,
    to_record,
    to_x1000,
    unknown_ports,
)
from bikechance_ml.jobs.snapshot_table import Snapshot, StationRow
from tests import eval_fixture as fixture
from tests import infer_fixture as serving


@pytest.fixture(autouse=True)
def _clean_cache() -> Iterator[None]:
    """**成果物のキャッシュはモジュールに残る。** 検査ごとに捨てる。

    本番では版が変われば入れ替わるので問題にならないが、検査は同じ版名で
    中身の違う口を使うので、持ち越すと前の検査の答えが出る。
    """
    forget_artifacts()
    yield
    forget_artifacts()


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
    return predict(ARTIFACT, system_id, AT, features(port, system_id), stale)


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
    """**過半の水平で気候値が効いたら 3**（全水平だと蓄積が伸びるまで出ない）。"""
    half = len(HORIZONS_MIN) // 2
    assert confidence_of(stale=True, climatology_horizons=len(HORIZONS_MIN)) == 1
    assert confidence_of(stale=False, climatology_horizons=0) == 2
    assert confidence_of(stale=False, climatology_horizons=half) == 2
    assert confidence_of(stale=False, climatology_horizons=half + 1) == 3


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
    assert model_path("baseline-b3-v0-20260909") == "baseline/baseline-b3-v0-20260909.json.gz"


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
    downloads: list[str] = field(default_factory=list)
    body: bytes | None = None
    next_id: int = 1

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

    def download(self, bucket: str, path: str) -> bytes | None:
        if bucket == MODEL_BUCKET:
            return self.body
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
    summary = run_inference(port, "hellocycling", ARTIFACT.model_version, NOW)
    assert summary.status == "ok"
    assert summary.n_rows == len(STATIONS)
    assert len(port.written) == len(STATIONS)
    assert port.finished == [(2, "ok", len(STATIONS))]


def test_the_same_observation_is_not_inferred_twice() -> None:
    """**Vercel Cron の二重起動は無害**（`inference_log` の一意制約で掴む）。"""
    port = ready_port()
    run_inference(port, "hellocycling", ARTIFACT.model_version, NOW)
    again = run_inference(port, "hellocycling", ARTIFACT.model_version, NOW)
    assert again.status == "skipped"
    assert again.n_rows == 0
    assert len(port.written) == len(STATIONS)


def test_a_new_observation_is_inferred() -> None:
    port = ready_port()
    run_inference(port, "hellocycling", ARTIFACT.model_version, NOW)
    port.base = BASE + timedelta(minutes=5)
    later = run_inference(port, "hellocycling", ARTIFACT.model_version, NOW)
    assert later.status == "ok"


def test_no_observation_yet_is_skipped_without_claiming() -> None:
    port = FakePort(base=None, body=to_bytes(ARTIFACT))
    summary = run_inference(port, "hellocycling", ARTIFACT.model_version, NOW)
    assert summary.status == "skipped"
    assert port.claims == []


def test_a_missing_artifact_is_recorded_as_failed() -> None:
    """**代わりの値をでっち上げない。** 失敗として記録する。"""
    port = FakePort(body=None)
    summary = run_inference(port, "hellocycling", "nope", NOW)
    assert summary.status == "failed"
    assert summary.error == MissingArtifactError.__name__
    assert port.finished == [(2, "failed", 0)]


def test_detail_is_json_safe() -> None:
    port = ready_port()
    detail = to_detail(run_inference(port, "hellocycling", ARTIFACT.model_version, NOW))
    assert detail["ok"] is True
    assert detail["system"] == "hellocycling"
    assert isinstance(detail["base_observed_at"], str)


def test_detail_carries_only_the_exception_name() -> None:
    """**例外の文言は要求 URL を抱えていることがある**（CLAUDE.md §5）。"""
    detail = to_detail(run_inference(FakePort(body=None), "hellocycling", "nope", NOW))
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
    assert unknown_ports(ARTIFACT, "hellocycling", table) == 1


def test_the_phantom_station_is_not_counted_as_drift() -> None:
    """幻のポート（ドコモ `5753`）は成果物に無くて当たり前である。"""
    table = features(ready_port((("5753", 2, 7, OPEN),)), "docomo-cycle")
    assert unknown_ports(ARTIFACT, "docomo-cycle", table) == 0


def test_no_drift_when_every_port_is_in_the_artifact() -> None:
    assert unknown_ports(ARTIFACT, "hellocycling", features(ready_port())) == 0


def test_the_count_reaches_the_response_and_the_record() -> None:
    """**応答にも記録にも出す。** 数えても出さなければ気づけない。"""
    port = ready_port()
    summary = run_inference(port, "hellocycling", "m1", NOW)
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
    }
    assert to_record(_summary(cpu_ms=42))["cpu_ms"] == 42


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
    summary = run_inference(port, "hellocycling", "m1", NOW)
    assert summary.features_ms > 0
    assert to_detail(summary)["features_ms"] == summary.features_ms
    recorded = port.details[0]
    assert recorded is not None and recorded["features_ms"] == summary.features_ms
    # 除外の内訳も残す（なぜ出せなかったかが分かる）
    assert isinstance(recorded["excluded"], dict)
    # **どの版の参照データを使ったか**も残す（00:00〜05:00 JST は 1 つ古い版になる）
    yesterday = (AT.astimezone(JST).date() - timedelta(days=1)).isoformat()
    assert recorded["reference_date"] == yesterday


def test_cpu_time_is_measured_and_reported() -> None:
    """**費用は実時間ではなく Active CPU で決まる**（開発プラン §4.5a）。

    `duration_ms` は Storage の取得と PostgREST の往復を含むので、計算に使った時間を
    分けて記録する。
    """
    port = ready_port()
    summary = run_inference(port, "hellocycling", "m1", NOW)
    assert summary.cpu_ms >= 0
    assert to_detail(summary)["cpu_ms"] == summary.cpu_ms
    recorded = port.details[0]
    assert recorded is not None and recorded["cpu_ms"] == summary.cpu_ms


def test_cpu_time_is_reported_even_when_nothing_was_inferred() -> None:
    """掴めなかった回も測る。**二重起動の抑止にも CPU は使っている。**"""
    port = ready_port()
    port.base = None
    summary = run_inference(port, "hellocycling", "m1", NOW)
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
    monkeypatch.setenv(MODEL_VERSION_ENV, ARTIFACT.model_version)
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


def test_route_needs_a_model_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """**配る版を環境変数で指定する。** 未設定なら黙って古い版を配らない。"""
    port = ready_port()
    client = client_for(port, monkeypatch)
    monkeypatch.delenv(MODEL_VERSION_ENV)
    response = client.get("/ml/infer/hellocycling", headers=AUTH)
    assert response.status_code == 500
    assert response.json()["title"] == "misconfigured"


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
    load_artifact(port, ARTIFACT.model_version)
    load_artifact(port, ARTIFACT.model_version)
    assert port.fetches == 1


def test_a_new_version_replaces_the_cache() -> None:
    port = CountingPort(body=to_bytes(ARTIFACT))
    load_artifact(port, ARTIFACT.model_version)
    load_artifact(port, "another-version")
    assert port.fetches == 2


@dataclass
class CountingPort(FakePort):
    """成果物を取りに行った回数を数えるだけの口。"""

    fetches: int = 0

    def download(self, bucket: str, path: str) -> bytes | None:
        self.fetches += 1
        return self.body
