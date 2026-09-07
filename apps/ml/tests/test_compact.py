"""毎時 Parquet 化のジョブと入口（`jobs/compact.py`、`api.py` の `/ml/compact`）。

差し替え可能な `CompactPort` にしてあるので、本物の Supabase 無しで分岐を全部通せる。
ここで守りたいのは 3 つ。
  * **1 システムの失敗が他を巻き込まない**（片方だけでも畳めたほうがよい）
  * **記録（`job_runs`）の不調でジョブを落とさない**
  * **同じ時間帯は同じパスに写像し、上書きする**（二重起動を無害にする）
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from bikechance_ml.api import build_app, parse_hour
from bikechance_ml.jobs.compact import (
    JOB_NAME,
    CompactPort,
    compact_hour,
    resolve_window,
    to_detail,
)
from bikechance_ml.jobs.snapshot_table import Snapshot, StationRow

SECRET = "test-cron-secret-value"
NOW = datetime(2026, 9, 8, 5, 7, tzinfo=UTC)
HOUR_START = datetime(2026, 9, 8, 4, 0, tzinfo=UTC)


def snapshot(minute: int, bikes: list[int]) -> Snapshot:
    size = len(bikes)
    return Snapshot(
        observed_at=HOUR_START + timedelta(minutes=minute),
        bikes=bikes,
        docks=[1] * size,
        flags=[7] * size,
        reported_age_s=[30] * size,
    )


class FakePort:
    """`CompactPort` を満たす試験用の入出力。呼ばれた記録を残す。"""

    def __init__(
        self,
        snapshots: Mapping[str, list[Snapshot]] | None = None,
        systems: tuple[str, ...] = ("hellocycling", "docomo-cycle"),
    ) -> None:
        self.systems = systems
        self.snapshots = dict(snapshots or {})
        self.uploads: list[tuple[str, bytes]] = []
        self.finished: list[tuple[int, str, Mapping[str, object]]] = []
        self.started: list[str] = []
        self.fail_upload_for: str | None = None
        self.fail_job_started = False
        self.fail_job_finished = False

    def list_active_systems(self) -> tuple[str, ...]:
        return self.systems

    def list_stations(self, system_id: str) -> tuple[StationRow, ...]:
        return (StationRow("a", 0), StationRow("b", 1))

    def list_snapshots(
        self, system_id: str, start: datetime, end: datetime
    ) -> tuple[Snapshot, ...]:
        return tuple(self.snapshots.get(system_id, []))

    def upload_parquet(self, path: str, body: bytes) -> None:
        if self.fail_upload_for is not None and self.fail_upload_for in path:
            raise RuntimeError("storage が 500 を返した")
        self.uploads.append((path, body))

    def job_started(self, job_name: str) -> int:
        if self.fail_job_started:
            raise RuntimeError("rpc が落ちた")
        self.started.append(job_name)
        return 42

    def job_finished(self, run_id: int, status: str, detail: Mapping[str, object]) -> None:
        if self.fail_job_finished:
            raise RuntimeError("rpc が落ちた")
        self.finished.append((run_id, status, detail))


def both_systems() -> FakePort:
    return FakePort(
        {
            "hellocycling": [snapshot(0, [1, 2]), snapshot(5, [3, 4])],
            "docomo-cycle": [snapshot(1, [5, 6])],
        }
    )


def as_port(port: FakePort) -> CompactPort:
    """Protocol を満たしていることを型検査で確かめる。"""
    return port


# ── 時間帯の決定 ──────────────────────────────────────────────
def test_window_defaults_to_the_previous_hour() -> None:
    assert resolve_window(NOW, None) == (HOUR_START, HOUR_START + timedelta(hours=1))


def test_window_accepts_an_explicit_past_hour() -> None:
    earlier = datetime(2026, 9, 7, 23, 0, tzinfo=UTC)
    assert resolve_window(NOW, earlier) == (earlier, earlier + timedelta(hours=1))


def test_window_rejects_a_time_that_is_not_on_the_hour() -> None:
    with pytest.raises(ValueError, match="正時"):
        resolve_window(NOW, datetime(2026, 9, 8, 4, 30, tzinfo=UTC))


def test_window_rejects_the_current_hour() -> None:
    """まだ観測が入り続けている時間帯を畳むと、後から行が増えて食い違う。"""
    with pytest.raises(ValueError, match="終わっていない"):
        resolve_window(NOW, datetime(2026, 9, 8, 5, 0, tzinfo=UTC))


# ── ジョブ ────────────────────────────────────────────────────
def test_compacts_every_active_system() -> None:
    port = both_systems()
    summary = compact_hour(as_port(port), NOW)
    assert summary.ok
    assert summary.n_systems == 2
    assert [path for path, _ in port.uploads] == [
        "hellocycling/date=2026-09-08/hour=04/part.parquet",
        "docomo-cycle/date=2026-09-08/hour=04/part.parquet",
    ]


def test_counts_rows_and_bytes() -> None:
    summary = compact_hour(as_port(both_systems()), NOW)
    # HELLO は 2 スナップショット × 2 ポート、ドコモは 1 × 2
    assert summary.n_rows == 6
    assert summary.bytes > 0


def test_records_the_run() -> None:
    port = both_systems()
    compact_hour(as_port(port), NOW)
    assert port.started == [JOB_NAME]
    assert len(port.finished) == 1
    run_id, status, detail = port.finished[0]
    assert (run_id, status) == (42, "ok")
    assert detail["n_rows"] == 6


def test_one_failing_system_does_not_stop_the_other() -> None:
    port = both_systems()
    port.fail_upload_for = "hellocycling"
    summary = compact_hour(as_port(port), NOW)
    assert not summary.ok
    assert summary.n_failed == 1
    assert [path for path, _ in port.uploads] == [
        "docomo-cycle/date=2026-09-08/hour=04/part.parquet"
    ]
    assert port.finished[0][1] == "failed"


def test_an_hour_without_snapshots_writes_nothing() -> None:
    """空のファイルを置くと「収集していない」と「畳んでいない」を見分けられなくなる。"""
    port = FakePort({}, systems=("hellocycling",))
    summary = compact_hour(as_port(port), NOW)
    assert summary.ok
    assert summary.n_empty == 1
    assert port.uploads == []


def test_job_started_failure_does_not_stop_the_work() -> None:
    port = both_systems()
    port.fail_job_started = True
    summary = compact_hour(as_port(port), NOW)
    assert summary.ok
    assert len(port.uploads) == 2
    assert port.finished == []


def test_job_finished_failure_does_not_change_the_summary() -> None:
    port = both_systems()
    port.fail_job_finished = True
    assert compact_hour(as_port(port), NOW).ok


def test_running_twice_writes_the_same_path_and_bytes() -> None:
    """二重起動は同じバイト列を 2 回書くだけ。結果は変わらない。"""
    port = both_systems()
    compact_hour(as_port(port), NOW)
    compact_hour(as_port(port), NOW)
    first, second = port.uploads[0], port.uploads[2]
    assert first == second


def test_detail_is_json_safe() -> None:
    detail = to_detail(compact_hour(as_port(both_systems()), NOW))
    assert detail["hour_start"] == "2026-09-08T04:00:00Z"
    systems = detail["systems"]
    assert isinstance(systems, list)
    assert len(systems) == 2


# ── 入口 ─────────────────────────────────────────────────────
@contextmanager
def lend(port: FakePort) -> Iterator[CompactPort]:
    yield port


def client_for(port: FakePort) -> TestClient:
    return TestClient(build_app(lambda: lend(port)))


def test_hour_parsing_requires_a_timezone() -> None:
    with pytest.raises(ValueError, match="タイムゾーン"):
        parse_hour("2026-09-08T04:00:00")


def test_hour_parsing_accepts_z_and_offsets() -> None:
    assert parse_hour("2026-09-08T04:00:00Z") == HOUR_START
    assert parse_hour("2026-09-08T13:00:00+09:00") == HOUR_START


def test_hour_parsing_of_nothing() -> None:
    assert parse_hour(None) is None
    assert parse_hour("") is None


def test_compact_requires_the_cron_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRON_SECRET", SECRET)
    port = both_systems()
    with client_for(port) as client:
        assert client.get("/ml/compact").status_code == 401
        assert client.get("/ml/compact", headers={"Authorization": "Bearer wrong"}).status_code
    assert port.uploads == []


def test_compact_rejects_an_empty_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """秘密が未設定なら誰でも通る、を作らない。"""
    monkeypatch.delenv("CRON_SECRET", raising=False)
    with client_for(both_systems()) as client:
        response = client.get("/ml/compact", headers={"Authorization": "Bearer "})
    assert response.status_code == 401


def test_compact_returns_the_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRON_SECRET", SECRET)
    port = both_systems()
    with client_for(port) as client:
        response = client.get("/ml/compact", headers={"Authorization": f"Bearer {SECRET}"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["n_rows"] == 6


def test_compact_returns_500_when_a_system_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """エラー率で気づけるようにする（3xx も 200 も返さない）。"""
    monkeypatch.setenv("CRON_SECRET", SECRET)
    port = both_systems()
    port.fail_upload_for = "docomo"
    with client_for(port) as client:
        response = client.get("/ml/compact", headers={"Authorization": f"Bearer {SECRET}"})
    assert response.status_code == 500
    assert response.json()["n_failed"] == 1


def test_compact_rejects_a_bad_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRON_SECRET", SECRET)
    with client_for(both_systems()) as client:
        headers = {"Authorization": f"Bearer {SECRET}"}
        assert client.get("/ml/compact?hour=yesterday", headers=headers).status_code == 400
        assert (
            client.get("/ml/compact?hour=2026-09-08T04:00:00", headers=headers).status_code == 400
        )


def test_compact_rejects_an_unfinished_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRON_SECRET", SECRET)
    ahead = (datetime.now(UTC) + timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
    with client_for(both_systems()) as client:
        response = client.get(
            f"/ml/compact?hour={ahead:%Y-%m-%dT%H:%M:%S}Z",
            headers={"Authorization": f"Bearer {SECRET}"},
        )
    assert response.status_code == 400
