"""天気の取り込み（`jobs/load_weather.py`）。

**「何を取り込むか」は DB が決める**（`v_weather_pending`）。ここで確かめるのは、
その答えに素直に従うことと、**落ちても次の実行が拾える形になっている**ことである。

  * 未処理を古い順に、上限まで
  * 同じ発行を入れ直しても増えない（冪等）
  * 1 つ落ちても残りは続け、**落ちたものは未処理のまま残る**
  * `job_runs` に記録する（**足したジョブは見張りから見えなければならない**。§12 の 116）
"""

import gzip
import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest
from fastapi.testclient import TestClient

from bikechance_ml.api import build_app
from bikechance_ml.jobs.load_weather import (
    JOB_NAME,
    LOOKBACK_HOURS,
    MissingBatchError,
    load_pending,
    read_issue,
    run_load,
)
from bikechance_ml.jobs.weather_archive import (
    WEATHER_BUCKET,
    PendingIssue,
    issued_hour_of,
    weather_object_path,
)

SAMPLE: Final[Path] = Path(__file__).resolve().parents[3] / "fixtures" / "weather"
BODY: Final[bytes] = (SAMPLE / "jma_msm_sample.json.gz").read_bytes()

#: 縮約したファイルの発行時刻（2 地点ぶんが入っている）。
HOUR_EPOCH_S: Final[int] = 1_789_002_000
ISSUED: Final[datetime] = issued_hour_of(HOUR_EPOCH_S)
NOW: Final[datetime] = ISSUED + timedelta(hours=1)


def _issue(
    hour_epoch_s: int = HOUR_EPOCH_S,
    n_cells: int = 2,
    n_loaded: int = 0,
    available_at: datetime | None = None,
) -> PendingIssue:
    issued = issued_hour_of(hour_epoch_s)
    return PendingIssue(
        hour_epoch_s=hour_epoch_s,
        issued_hour=issued,
        available_at=available_at if available_at is not None else issued + timedelta(minutes=18),
        n_cells=n_cells,
        n_loaded=n_loaded,
    )


def _recent_issue() -> PendingIssue:
    """**入口（`/ml/weather`）の検査で使う発行。**

    入口は `datetime.now(UTC)` を使い、既定の窓は「48 時間前まで」である。発行時刻は
    フィクスチャに縛られている（`read_batch` が `hourly.time` の範囲を要求する）ので
    動かせないが、**`available_at` は動かせる**。

    **固定の `available_at` にしていたせいで、2026-09-12 01:18 UTC にこの検査は
    期限切れになった**——フィクスチャの発行が 48 時間より古くなり、窓から外れて
    「未処理が 0 件」になった。**日付が来ると落ちる検査は、書いた日にしか通らない。**
    """
    return _issue(available_at=datetime.now(UTC) - timedelta(minutes=1))


def _key(row: Mapping[str, object]) -> tuple[int, int, str]:
    """`weather_hourly` の主キー。"""
    return (
        int(str(row["cell_lat_idx"])),
        int(str(row["cell_lon_idx"])),
        str(row["issued_hour"]),
    )


@dataclass
class FakePort:
    """`WeatherPort` の代役。**取り込んだ行を覚える。**"""

    pending: tuple[PendingIssue, ...] = ()
    missing: frozenset[str] = frozenset()
    #: 主キーで持つ。**同じ発行を入れ直しても増えない**ことがそのまま見える
    stored: dict[tuple[int, int, str], Mapping[str, object]] = field(default_factory=dict)
    asked: list[tuple[datetime, int]] = field(default_factory=list)
    downloads: list[str] = field(default_factory=list)
    runs: list[tuple[str, str, Mapping[str, object]]] = field(default_factory=list)

    def list_weather_pending(self, since: datetime, limit: int) -> tuple[PendingIssue, ...]:
        self.asked.append((since, limit))
        return tuple(one for one in self.pending if one.available_at >= since)[:limit]

    def download(self, bucket: str, path: str) -> bytes | None:
        assert bucket == WEATHER_BUCKET
        self.downloads.append(path)
        return None if path in self.missing else BODY

    def upsert_weather_hourly(self, rows: Sequence[Mapping[str, object]]) -> int:
        for row in rows:
            self.stored[_key(row)] = row
        return len(rows)

    def job_started(self, job_name: str) -> int:
        self.runs.append((job_name, "running", {}))
        return len(self.runs)

    def job_finished(self, run_id: int, status: str, detail: Mapping[str, object]) -> None:
        self.runs.append((JOB_NAME, status, detail))


# ── 取り込み ──────────────────────────────────────────────────
def test_it_loads_a_pending_issue() -> None:
    port = FakePort(pending=(_issue(),))
    summary = load_pending(port, NOW - timedelta(days=1), 6, NOW)
    assert summary.ok
    assert summary.n_loaded == 1
    assert summary.n_rows == 2
    assert len(port.stored) == 2


def test_it_reads_every_batch_of_the_issue() -> None:
    """595 格子なら 6 分割。**パスは取得側と同じ規約**で組み立てる。"""
    port = FakePort()
    read_issue(port, _issue(n_cells=595))
    assert port.downloads == [weather_object_path(HOUR_EPOCH_S, batch) for batch in range(6)]


def test_reloading_the_same_issue_changes_nothing() -> None:
    """**冪等。** 主キーで上書きするので、2 度目も同じ行数のまま。"""
    port = FakePort(pending=(_issue(),))
    load_pending(port, NOW - timedelta(days=1), 6, NOW)
    load_pending(port, NOW - timedelta(days=1), 6, NOW)
    assert len(port.stored) == 2


def test_it_stops_at_the_limit() -> None:
    port = FakePort(pending=(_issue(HOUR_EPOCH_S - 7200), _issue(HOUR_EPOCH_S - 3600), _issue()))
    summary = load_pending(port, NOW - timedelta(days=1), 2, NOW)
    assert summary.n_pending == 2
    assert summary.n_loaded == 2


def test_nothing_pending_is_a_success() -> None:
    summary = load_pending(FakePort(), NOW - timedelta(days=1), 6, NOW)
    assert summary.ok
    assert summary.n_loaded == 0
    assert summary.n_rows == 0


# ── 落ちたとき ────────────────────────────────────────────────
def test_a_missing_batch_fails_only_that_issue() -> None:
    """**部分的に取り込まない。** その発行は未処理のまま残り、次の実行が拾う。"""
    good = _issue(HOUR_EPOCH_S - 3600)
    port = FakePort(
        pending=(good, _issue()), missing=frozenset({weather_object_path(HOUR_EPOCH_S, 0)})
    )
    summary = load_pending(port, NOW - timedelta(days=1), 6, NOW)
    assert not summary.ok
    assert summary.n_loaded == 1
    assert summary.n_failed == 1
    assert summary.as_dict()["failures"] == [
        {"issued_hour": ISSUED.isoformat(), "error": MissingBatchError.__name__}
    ]
    # **落ちた発行の行は 1 つも入っていない**
    assert all(key[2] != ISSUED.isoformat() for key in port.stored)


def test_a_broken_file_fails_only_that_issue() -> None:
    """壊れたファイルでも、記録は種類だけ（**文言に接続先を混ぜない**）。"""

    @dataclass
    class BrokenPort(FakePort):
        def download(self, bucket: str, path: str) -> bytes | None:
            return gzip.compress(json.dumps({"latitude": 24.35}).encode())

    port = BrokenPort(pending=(_issue(),))
    summary = load_pending(port, NOW - timedelta(days=1), 6, NOW)
    assert not summary.ok
    failures = summary.as_dict()["failures"]
    assert isinstance(failures, list)
    assert failures[0]["error"] == "ShapeError"


# ── 記録と窓 ──────────────────────────────────────────────────
def test_it_records_the_run() -> None:
    """**足したジョブは `job_runs` に書く**（書かないと監視から見えない。§12 の 116）。"""
    port = FakePort(pending=(_issue(),))
    run_load(port, NOW)
    assert [one[0] for one in port.runs] == [JOB_NAME, JOB_NAME]
    assert port.runs[-1][1] == "ok"
    assert port.runs[-1][2]["loaded"] == 1


def test_a_failed_load_is_recorded_as_failed() -> None:
    port = FakePort(pending=(_issue(),), missing=frozenset({weather_object_path(HOUR_EPOCH_S, 0)}))
    summary = run_load(port, NOW)
    assert not summary.ok
    assert port.runs[-1][1] == "failed"


def test_the_default_window_is_shorter_than_the_retention() -> None:
    """**保持（30 日）より短く。** 消えた発行が「未処理」として蘇らないため（0037）。"""
    port = FakePort()
    run_load(port, NOW)
    since, _ = port.asked[0]
    assert since == NOW - timedelta(hours=LOOKBACK_HOURS)
    assert LOOKBACK_HOURS < 30 * 24


def test_an_explicit_window_is_used_for_backfill() -> None:
    port = FakePort()
    start = datetime(2026, 9, 7, tzinfo=UTC)
    run_load(port, NOW, start, 80)
    assert port.asked == [(start, 80)]


def test_the_record_is_json_safe() -> None:
    port = FakePort(pending=(_issue(),))
    summary = run_load(port, NOW)
    assert json.loads(json.dumps(summary.as_dict()))["rows"] == 2


def test_it_fails_loudly_when_the_pending_list_cannot_be_read() -> None:
    """**一覧が読めないのは全体の失敗。** 静かに「0 件」と記録しない。"""

    @dataclass
    class BlindPort(FakePort):
        def list_weather_pending(self, since: datetime, limit: int) -> tuple[PendingIssue, ...]:
            raise RuntimeError("読めない")

    port = BlindPort()
    with pytest.raises(RuntimeError):
        run_load(port, NOW)
    assert port.runs[-1][1] == "failed"


# ── 入口（`/ml/weather`）──────────────────────────────────────
@contextmanager
def _lend(port: FakePort) -> Iterator[FakePort]:
    yield port


def _client(port: FakePort, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    return TestClient(build_app(make_weather_port=lambda: _lend(port)))


AUTH: Final[Mapping[str, str]] = {"Authorization": "Bearer s3cret"}


def test_route_requires_the_cron_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """**DB にもログにも何も書かずに弾く。**"""
    port = FakePort(pending=(_recent_issue(),))
    response = _client(port, monkeypatch).get("/ml/weather")
    assert response.status_code == 401
    assert port.runs == []


def test_route_returns_the_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    port = FakePort(pending=(_recent_issue(),))
    response = _client(port, monkeypatch).get("/ml/weather", headers=dict(AUTH))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["loaded"] == 1


def test_route_returns_500_when_an_issue_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """**失敗は 500 で返す**（Vercel Observability のエラー率検知を効かせる）。"""
    port = FakePort(
        pending=(_recent_issue(),), missing=frozenset({weather_object_path(HOUR_EPOCH_S, 0)})
    )
    response = _client(port, monkeypatch).get("/ml/weather", headers=dict(AUTH))
    assert response.status_code == 500
    assert response.json()["failed"] == 1
