"""日次評価のジョブと入口（`jobs/evaluate.py`、`api.py` の `/ml/evaluate`）。

差し替え可能な `EvaluatePort` にしてあるので、本物の Supabase 無しで分岐を全部通せる。
ここで守りたいのは 5 つ。

  * **日を切るのは `generated_at`**（パスの `base_observed_at` ではない）
  * **同じ日を 2 回走らせても行が増えない**（完了条件 2）
  * **ログの無い日は `skipped`**（失敗にしない。完了条件 5）
  * **足りない一覧の上で測らない**（Storage の上限に達したら止める）
  * **1 システムの失敗が他を巻き込まない**
"""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from bikechance_ml.api import build_app
from bikechance_ml.features.constants import HORIZONS_MIN
from bikechance_ml.features.grid import JST, parquet_hours
from bikechance_ml.jobs import forecast_log
from bikechance_ml.jobs.evaluate import (
    JOB_NAME,
    LIST_LIMIT,
    EvaluatePort,
    TruncatedListingError,
    cycle_of,
    list_logs,
    log_hours,
    log_prefix,
    probabilities,
    read_log,
    read_logs,
    run_evaluation,
    within,
)
from bikechance_ml.jobs.snapshot_table import parquet_path, to_parquet_bytes
from tests.test_eval_served import observations, stream

SECRET = "test-cron-secret-value"
DAY = date(2026, 9, 7)
MODEL = "baseline-b3-v0-20260906"


class Forecast:
    """`forecast_log.Predicted` を満たす作り物。"""

    def __init__(self, station_id: str, p_bike: int, p_dock: int) -> None:
        self.station_id = station_id
        self.p_bike_x1000 = tuple([p_bike] * len(HORIZONS_MIN))
        self.p_dock_x1000 = tuple([p_dock] * len(HORIZONS_MIN))
        self.confidence = 3


def log_file(
    stations: Sequence[str],
    generated_at: datetime,
    base_observed_at: datetime | None = None,
    model_version: str = MODEL,
    p_bike: int = 800,
) -> forecast_log.LogFile:
    """1 サイクルぶんのログ。**基準観測の時刻は `generated_at` と別に置ける。**"""
    return forecast_log.build(
        [Forecast(one, p_bike, 900) for one in stations],
        system_id="hellocycling",
        base_observed_at=base_observed_at or generated_at + timedelta(seconds=96),
        generated_at=generated_at,
        model_version=model_version,
        feature_set="v3",
    )


def at(hour: int, minute: int) -> datetime:
    return datetime(DAY.year, DAY.month, DAY.day, hour, minute, tzinfo=JST)


class FakePort:
    """`EvaluatePort` を満たす試験用の入出力。呼ばれた記録を残す。"""

    def __init__(
        self,
        logs: Sequence[forecast_log.LogFile] = (),
        observed: pa.Table | None = None,
        systems: Sequence[str] = ("hellocycling",),
    ) -> None:
        self.systems = tuple(systems)
        self.objects = {one.path: one.body for one in logs}
        self.observed = observed
        self.written: list[Mapping[str, object]] = []
        self.finished: list[tuple[int, str, Mapping[str, object]]] = []
        self.listed: list[str] = []

    def list_forecast_log(self, prefix: str, limit: int) -> tuple[str, ...]:
        self.listed.append(prefix)
        return tuple(
            sorted(path[len(prefix) :] for path in self.objects if path.startswith(prefix))
        )

    def download_forecast_log(self, path: str) -> bytes | None:
        return self.objects.get(path)

    def download_parquet(self, path: str) -> bytes | None:
        if self.observed is None or not any(path.startswith(one) for one in self.systems):
            return None
        return to_parquet_bytes(self.observed)

    def upsert_model_daily_metrics(self, rows: Sequence[Mapping[str, object]]) -> int:
        self.written.extend(rows)
        return len(rows)

    def job_started(self, job_name: str) -> int:
        assert job_name == JOB_NAME
        return 41

    def job_finished(self, run_id: int, status: str, detail: Mapping[str, object]) -> None:
        self.finished.append((run_id, status, detail))


# ── 日の切り方 ────────────────────────────────────────────────
def test_the_day_is_cut_by_generated_at_not_by_the_path() -> None:
    """**パスは基準観測の時刻で決まる。** 日の内外は覚え書きの `generated_at` で決める。

    実測では 2 つは最大 5 分ずれる。パスだけで切ると、日の境目のサイクルが
    **こぼれるか、二重に入る**。
    """
    inside = log_file(["p1"], at(0, 0), base_observed_at=at(0, 1))
    outside = log_file(["p1"], at(23, 55) - timedelta(days=1), base_observed_at=at(0, 1))
    assert within(DAY, at(0, 0))
    assert not within(DAY, at(23, 55) - timedelta(days=1))
    assert read_log(inside.body, inside.path).generated_at == at(0, 0).astimezone(UTC)
    assert read_log(outside.body, outside.path).model_version == MODEL


def test_the_listing_covers_the_day_with_a_margin() -> None:
    """**UTC の時間帯は JST の暦日の前後に広げる**（パスが基準観測の時刻だから）。"""
    hours = log_hours(DAY)
    assert hours[0] == datetime(2026, 9, 6, 14, tzinfo=UTC)
    assert hours[-1] == datetime(2026, 9, 7, 15, tzinfo=UTC)
    assert len(hours) == 26


def test_the_prefix_matches_the_writer() -> None:
    """一覧の接頭辞は `forecast_log.log_path` と同じ組み立てでなければならない。"""
    path = forecast_log.log_path("hellocycling", at(9, 0), MODEL)
    assert path.startswith(log_prefix("hellocycling", at(9, 0)))


def test_a_cycle_off_the_grid_is_refused() -> None:
    """**`generated_at` は 5 分格子の上にしか無い**（`jobs/infer.py` が落としている）。"""
    assert cycle_of(DAY, at(0, 5)) == 1
    with pytest.raises(ValueError, match="格子"):
        cycle_of(DAY, at(0, 5) + timedelta(seconds=30))


# ── 一覧 ──────────────────────────────────────────────────────
def test_a_truncated_listing_stops_the_job() -> None:
    """**足りない一覧の上で測らない。** 上限に達したら止める。"""

    class Truncating(FakePort):
        def list_forecast_log(self, prefix: str, limit: int) -> tuple[str, ...]:
            return tuple(f"{index}_x.parquet" for index in range(LIST_LIMIT))

    with pytest.raises(TruncatedListingError):
        list_logs(Truncating(), "hellocycling", DAY)


def test_a_cycle_outside_the_day_is_dropped_after_reading() -> None:
    """**日の外のサイクルは捨てる。** 一覧は前後 1 時間ぶん余分に拾うので必ず出てくる。

    余白は `base_observed_at` と `generated_at` が最大 5 分ずれるために要るもので、
    **拾ったあと `generated_at` で絞り直す**のが対になっている。片方だけになると、
    日の境目のサイクルが**隣の日に二重に入る**。
    """
    inside = log_file(["p1"], at(0, 0))
    before = log_file(["p1"], at(0, 0) - timedelta(minutes=5))
    after = log_file(["p1"], at(0, 0) + timedelta(days=1))
    port = FakePort([inside, before, after])
    read = read_logs(port, "hellocycling", DAY)
    assert [one.generated_at for one in read] == [at(0, 0).astimezone(UTC)]


def test_a_listed_file_that_has_vanished_is_skipped() -> None:
    """**一覧に在って本体が無いファイルは飛ばす。**

    一覧と取得のあいだに保持期間が切れれば起こりうる。**`None` をそのまま先へ流すと**
    `station_keys` が属性を引けずに落ち、1 日ぶんの成績がまるごと書けなくなる。
    """
    kept = log_file(["p1"], at(9, 0))
    lost = log_file(["p1"], at(9, 5))

    class Vanishing(FakePort):
        def download_forecast_log(self, path: str) -> bytes | None:
            return None if path == lost.path else self.objects.get(path)

    port = Vanishing([kept, lost])
    assert [one.path for one in read_logs(port, "hellocycling", DAY)] == [kept.path]


def test_names_that_are_not_logs_are_ignored() -> None:
    """`{epoch}_{version}.parquet` でない名前は拾わない。"""

    class Noisy(FakePort):
        def list_forecast_log(self, prefix: str, limit: int) -> tuple[str, ...]:
            return ("README.md", "1789000000_v1.parquet") if prefix.endswith("hour=00/") else ()

    found = list_logs(Noisy(), "hellocycling", DAY)
    assert [one.rsplit("/", 1)[1] for one in found] == ["1789000000_v1.parquet"]


# ── 通しで走らせる ────────────────────────────────────────────
def full_day_port(cycles: Sequence[datetime] = (at(9, 0), at(9, 5))) -> FakePort:
    table = observations(stream("p1", datetime(2026, 9, 7, 8, 0, tzinfo=JST), 180))
    return FakePort([log_file(["p1"], one) for one in cycles], observed=table)


def test_a_day_with_logs_is_measured_and_written() -> None:
    port = full_day_port()
    summary = run_evaluation(port, DAY)
    assert summary.status == "ok" and summary.ok
    assert summary.n_written == len(port.written) > 0
    assert {one["target"] for one in port.written} == {"bike", "dock"}
    assert {one["metric_date"] for one in port.written} == {"2026-09-07"}


def test_running_the_same_day_twice_writes_the_same_rows() -> None:
    """**冪等**（完了条件 2）。主キーは同じなので、2 度目も行数が変わらない。"""
    first = run_evaluation(full_day_port(), DAY)
    port = full_day_port()
    second = run_evaluation(port, DAY)
    assert first.n_written == second.n_written
    keys = {
        (one["model_version"], one["system_id"], one["target"], one["h_min"], one["bucket"])
        for one in port.written
    }
    assert len(keys) == len(port.written), "主キーが重複していない"


def test_a_day_without_logs_is_skipped() -> None:
    """**ログの無い日は `skipped`。** 埋めないし、失敗にもしない（完了条件 5）。"""
    summary = run_evaluation(FakePort(), DAY)
    assert summary.status == "skipped" and summary.ok
    assert summary.n_written == 0


def test_a_failing_system_does_not_stop_the_other() -> None:
    """実測が無いシステムは落ちるが、**他のシステムは測れる**。"""

    class NoParquet(FakePort):
        def download_parquet(self, path: str) -> bytes | None:
            return None

    port = NoParquet([log_file(["p1"], at(9, 0))])
    summary = run_evaluation(port, DAY)
    assert not summary.ok and summary.status == "failed"
    failed = next(one for one in summary.systems if one.system_id == "hellocycling")
    assert failed.error == "MissingObservationsError"


def test_both_versions_are_measured_separately() -> None:
    """`active` と `shadow` が同じ日に並ぶ。**版ごとに行を分ける。**

    **違う確率を配らせて、成績が違うことまで見る。** 「版の名前が 2 つ出た」だけでは、
    **両方に同じログを渡していても通ってしまう**（最初これで破壊を 1 つ取り逃した。
    §12 の 161 と同じ形）。
    """
    table = observations(stream("p1", datetime(2026, 9, 7, 8, 0, tzinfo=JST), 180))
    port = FakePort(
        [
            log_file(["p1"], at(9, 0), p_bike=900),
            log_file(["p1"], at(9, 0), model_version="shadow-v1", p_bike=100),
        ],
        observed=table,
    )
    summary = run_evaluation(port, DAY)
    assert summary.systems[0].model_versions == ("baseline-b3-v0-20260906", "shadow-v1")
    assert {one["model_version"] for one in port.written} == {MODEL, "shadow-v1"}
    briers = {
        one["model_version"]: one["brier"]
        for one in port.written
        if one["target"] == "bike" and one["h_min"] == 5 and one["bucket"] == "全体"
    }
    assert briers[MODEL] != briers["shadow-v1"], "**版ごとに別のログで測っている**"


def test_a_write_failure_still_ends_the_run() -> None:
    """**書き込みで落ちても `job_runs` は必ず終わる。**

    `running` のまま残すと、見張りが「止まった」ではなく「遅い」と読む
    （`jobs/compact.py` と同じ形）。**例外の種類だけ**を残す（CLAUDE.md §5）。
    """

    class Failing(FakePort):
        def upsert_model_daily_metrics(self, rows: Sequence[Mapping[str, object]]) -> int:
            raise RuntimeError("接続先を抱えた文言")

    port = Failing(
        [log_file(["p1"], at(9, 0))],
        observed=observations(stream("p1", datetime(2026, 9, 7, 8, 0, tzinfo=JST), 180)),
    )
    summary = run_evaluation(port, DAY)
    assert not summary.ok and summary.status == "failed"
    assert summary.error == "RuntimeError", "**文言ではなく種類だけ**"
    run_id, status, _ = port.finished[-1]
    assert (run_id, status) == (41, "failed"), "`running` のまま残さない"


def test_the_run_is_recorded_with_the_breakdown() -> None:
    """`job_runs` に**落とした理由の内訳**まで残す（あとで読むのはここ）。"""
    port = full_day_port()
    run_evaluation(port, DAY)
    run_id, status, detail = port.finished[-1]
    assert (run_id, status) == (41, "ok")
    systems = detail["systems"]
    assert isinstance(systems, list)
    assert "dropped" in systems[0] and "n_kept" in systems[0]


# ── 確率の読み ────────────────────────────────────────────────
def test_the_probabilities_are_read_as_a_matrix() -> None:
    one = log_file(["p1", "p2"], at(9, 0))
    table = read_log(one.body, one.path).table
    values = probabilities(table, "bike")
    assert values.shape == (2, len(HORIZONS_MIN))
    assert set(values.flatten().tolist()) == {800}


# ── 入口 ──────────────────────────────────────────────────────
@contextmanager
def _port(port: EvaluatePort) -> Iterator[EvaluatePort]:
    yield port


def client(port: EvaluatePort) -> TestClient:
    return TestClient(build_app(make_evaluate_port=lambda: _port(port)))


def test_the_endpoint_needs_the_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRON_SECRET", SECRET)
    assert client(FakePort()).get("/ml/evaluate").status_code == 401


def test_the_endpoint_refuses_a_bad_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRON_SECRET", SECRET)
    response = client(FakePort()).get(
        "/ml/evaluate?date=2026-13-99", headers={"Authorization": f"Bearer {SECRET}"}
    )
    assert response.status_code == 400
    assert response.json()["title"] == "invalid_date"


def test_the_endpoint_measures_the_day_it_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRON_SECRET", SECRET)
    port = full_day_port()
    response = client(port).get(
        "/ml/evaluate?date=2026-09-07", headers={"Authorization": f"Bearer {SECRET}"}
    )
    assert response.status_code == 200
    assert response.json()["metric_date"] == "2026-09-07"
    assert response.headers["cache-control"] == "no-store"


def test_the_day_is_given_as_date_not_as_the_python_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**URL 上の名前は `date`。** Python 側の `date_text` では効かない。

    FastAPI は**知らない問い合わせ引数を黙って捨てる**ので、名前を間違えると
    400 ではなく「指定したつもりで前日が測られる」。**取り違えたときに気づけない
    形の失敗**なので、どちらの向きも固定しておく（W5 プラン §12 の 170）。
    """
    monkeypatch.setenv("CRON_SECRET", SECRET)
    header = {"Authorization": f"Bearer {SECRET}"}
    given = client(full_day_port()).get("/ml/evaluate?date=2026-09-07", headers=header)
    assert given.json()["metric_date"] == "2026-09-07"

    # **古い名前は効かない**（既定の「前日」に落ちる。2026-09-07 にはならない）
    ignored = client(full_day_port()).get("/ml/evaluate?date_text=2026-09-07", headers=header)
    assert ignored.json()["metric_date"] != "2026-09-07"


def test_the_endpoint_reports_a_failure_with_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """**エラーは 500 で返す**（Vercel Observability のエラー率検知を効かせる）。"""
    monkeypatch.setenv("CRON_SECRET", SECRET)
    port = FakePort([log_file(["p1"], at(9, 0))])
    response = client(port).get(
        "/ml/evaluate?date=2026-09-07", headers={"Authorization": f"Bearer {SECRET}"}
    )
    assert response.status_code == 500
    assert response.json()["ok"] is False


# ── 読み込む時間帯 ────────────────────────────────────────────
def test_the_observations_cover_the_longest_horizon() -> None:
    """**最長の水平（180 分）の目標時刻まで読む。** 足りないと `stale_at_target` になる。"""
    hours = parquet_hours(DAY, 1, 3)
    last_target = datetime(2026, 9, 7, 23, 55, tzinfo=JST) + timedelta(minutes=max(HORIZONS_MIN))
    assert hours[-1] <= last_target.astimezone(UTC) < hours[-1] + timedelta(hours=1)
    assert parquet_path("hellocycling", hours[-1]).startswith("hellocycling/date=2026-09-07/")
