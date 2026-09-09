"""日次の参照スナップショットのジョブ（`jobs/build_reference.py`）。

**この 1 ファイルの主題は「止まったときに気づけること」。** 学習も推論も「基準時刻の
前日の版」を読む設計なので、このジョブが黙って止まると**翌日に静かに壊れる**
（W4 プラン §12 の 116）。守るのは 3 つ。
  * **成功も失敗も `job_runs` に記録する**（`check_jobs_missing` から見える）
  * **記録の不調でジョブを落とさない**（置けたことのほうが大事。`compact` と同じ）
  * **失敗は投げ直す**（入口の 500 と、記録の `failed` の両方を残す）
"""

from collections.abc import Mapping
from datetime import UTC, date, datetime

import pytest

from bikechance_ml.features.reference import NeighborRow, StationAttributeRow, StationGeoRow
from bikechance_ml.features.reference_snapshot import NEIGHBORS_NAME, STATIONS_NAME
from bikechance_ml.jobs.build_reference import (
    JOB_NAME,
    ReferencePort,
    build_and_upload,
    yesterday,
)

DAY = date(2026, 9, 9)
NOW = datetime(2026, 9, 10, 20, 0, tzinfo=UTC)
SEEN = datetime(2026, 9, 6, 11, 0, tzinfo=UTC)


class FakePort:
    """`ReferencePort` を満たす試験用の入出力。呼ばれた記録を残す。"""

    def __init__(self) -> None:
        self.uploads: list[tuple[str, int]] = []
        self.started: list[str] = []
        self.finished: list[tuple[int, str, Mapping[str, object]]] = []
        self.fail_upload = False
        self.fail_job_started = False
        self.fail_job_finished = False

    def list_station_geo(self, system_id: str) -> tuple[StationGeoRow, ...]:
        return (StationGeoRow(station_id="a", first_seen_at=SEEN, pref_code=13, muni_code=13101),)

    def list_station_attributes(self, system_id: str) -> tuple[StationAttributeRow, ...]:
        return (
            StationAttributeRow(
                station_id="a",
                lat=35.0,
                lon=139.0,
                capacity=12,
                is_charging_station=False,
                region_id=None,
            ),
        )

    def list_neighbors(self, system_id: str) -> tuple[NeighborRow, ...]:
        return ()

    def download(self, bucket: str, path: str) -> bytes | None:
        """その日のスナップショットも前の版も無い日。**欠けたまま進む**（補間しない）。"""
        return None

    def upload_parquet(self, path: str, body: bytes) -> None:
        if self.fail_upload:
            raise RuntimeError("storage down")
        self.uploads.append((path, len(body)))

    def job_started(self, job_name: str) -> int:
        if self.fail_job_started:
            raise RuntimeError("db down")
        self.started.append(job_name)
        return 42

    def job_finished(self, run_id: int, status: str, detail: Mapping[str, object]) -> None:
        if self.fail_job_finished:
            raise RuntimeError("db down")
        self.finished.append((run_id, status, detail))


def run(port: ReferencePort) -> dict[str, object]:
    return build_and_upload(port, DAY, NOW)


# ── 記録 ──────────────────────────────────────────────────────
def test_a_successful_run_is_recorded() -> None:
    """**回ったことが `job_runs` に残る。** 残らないと監視から見えない。"""
    port = FakePort()
    summary = run(port)
    assert port.started == [JOB_NAME]
    assert len(port.finished) == 1
    run_id, status, detail = port.finished[0]
    assert (run_id, status) == (42, "ok")
    assert detail["date"] == DAY.isoformat()
    assert detail["stations"] == summary["stations"]


def test_the_job_name_is_the_one_monitoring_expects() -> None:
    """`monitored_jobs` の行と同じ名前でなければ、いつまでも「来ていない」と鳴る。"""
    assert JOB_NAME == "build_reference"


def test_a_failure_is_recorded_and_raised() -> None:
    """**「動いていない」と「動いたが失敗した」を区別できるようにする。**"""
    port = FakePort()
    port.fail_upload = True
    with pytest.raises(RuntimeError):
        run(port)
    assert len(port.finished) == 1
    _, status, detail = port.finished[0]
    assert status == "failed"
    # 例外の**種類だけ**を残す（文言に接続先が混じる経路を作らない）
    assert detail["error"] == "RuntimeError"
    assert "storage down" not in str(detail)


def test_recording_the_start_can_fail_without_stopping_the_work() -> None:
    """記録の不調で 1 日ぶんを落とさない（`compact` と同じ方針）。"""
    port = FakePort()
    port.fail_job_started = True
    summary = run(port)
    assert summary["ok"] is True
    assert len(port.uploads) == 2
    # 始まりを記録できなければ、終わりも書かない（宙ぶらりんの run_id を作らない）
    assert port.finished == []


def test_recording_the_end_can_fail_without_changing_the_result() -> None:
    port = FakePort()
    port.fail_job_finished = True
    summary = run(port)
    assert summary["ok"] is True
    assert len(port.uploads) == 2


# ── 置くもの ──────────────────────────────────────────────────
def test_it_writes_both_tables_under_the_day() -> None:
    port = FakePort()
    run(port)
    assert [path for path, _ in port.uploads] == [
        f"reference/date=2026-09-09/{STATIONS_NAME}.parquet",
        f"reference/date=2026-09-09/{NEIGHBORS_NAME}.parquet",
    ]


def test_a_day_without_snapshots_is_reported_not_invented() -> None:
    """**欠けた時間帯は数で出す。** 補間しない（CLAUDE.md §6）。"""
    port = FakePort()
    summary = run(port)
    # 2 システム × 24 時間ぶん、1 つも無い
    assert summary["missing_hours"] == 48
    assert summary["capacity_days_max"] == 0
    assert summary["with_capacity_est"] == 0


def test_yesterday_is_the_jst_day_before() -> None:
    """05:00 JST に走らせるので、**前日ぶんが揃っている**。"""
    # 2026-09-10 20:00 UTC = 2026-09-11 05:00 JST → 前日は 09-10
    assert yesterday(NOW) == date(2026, 9, 10)
