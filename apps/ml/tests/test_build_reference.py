"""日次の参照スナップショットのジョブ（`jobs/build_reference.py`）。

**この 1 ファイルの主題は「止まったときに気づけること」。** 学習も推論も「基準時刻の
前日の版」を読む設計なので、このジョブが黙って止まると**翌日に静かに壊れる**
（W4 プラン §12 の 116）。守るのは 3 つ。
  * **成功も失敗も `job_runs` に記録する**（`check_jobs_missing` から見える）
  * **記録の不調でジョブを落とさない**（置けたことのほうが大事。`compact` と同じ）
  * **失敗は投げ直す**（入口の 500 と、記録の `failed` の両方を残す）
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from bikechance_ml.features.grid import day_hours, jst_yesterday
from bikechance_ml.features.reference import NeighborRow, StationAttributeRow, StationGeoRow
from bikechance_ml.features.reference_snapshot import (
    NEIGHBORS_NAME,
    STATIONS_NAME,
    daily_capacity_max,
)
from bikechance_ml.jobs.build_features import SYSTEM_IDS
from bikechance_ml.jobs.build_reference import (
    JOB_NAME,
    ReferencePort,
    build_and_upload,
    load_daily_max,
    read_hour,
)
from bikechance_ml.jobs.snapshot_table import SCHEMA as SNAPSHOT_SCHEMA
from bikechance_ml.jobs.snapshot_table import parquet_path
from bikechance_ml.jobs.snapshot_table import to_parquet_bytes as snapshot_bytes

DAY = date(2026, 9, 9)
NOW = datetime(2026, 9, 10, 20, 0, tzinfo=UTC)
SEEN = datetime(2026, 9, 6, 11, 0, tzinfo=UTC)


class FakePort:
    """`ReferencePort` を満たす試験用の入出力。呼ばれた記録を残す。"""

    def __init__(self) -> None:
        self.uploads: list[tuple[str, int]] = []
        self.started: list[str] = []
        self.finished: list[tuple[int, str, Mapping[str, object]]] = []
        self.capacity_rows: list[Mapping[str, object]] = []
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

    def upsert_capacity_est(self, rows: Sequence[Mapping[str, object]]) -> int:
        """ポートの大きさを DB に写す（0045）。**Storage に置いたあとで呼ばれる。**"""
        self.capacity_rows.extend(rows)
        return len(rows)

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
    # **観測が無ければ DB にも 1 行も写さない**（0 を「大きさ 0」として入れない。0045）
    assert summary["capacity_est_rows"] == 0
    assert port.capacity_rows == []


def test_yesterday_is_the_jst_day_before() -> None:
    """05:00 JST に走らせるので、**前日ぶんが揃っている**。"""
    # 2026-09-10 20:00 UTC = 2026-09-11 05:00 JST → 前日は 09-10
    assert jst_yesterday(NOW) == date(2026, 9, 10)


# ── 1 時間ずつ畳む（W6-35、D-40）──────────────────────────────
#: その日の 24 時間（UTC の時の頭。09-08 15:00 〜 09-09 14:00）
HOURS = day_hours(DAY)

#: 時間ごとのスナップショット：(システム, 何時間目, [(ポート, bikes, docks)])
HOURLY: tuple[tuple[str, int, tuple[tuple[str, int, int], ...]], ...] = (
    ("hellocycling", 0, (("a", 3, 5),)),
    ("hellocycling", 5, (("a", 2, 9), ("b", -1, -1))),
    ("hellocycling", 7, (("b", 4, 4), ("a", 1, 1))),
    ("docomo-cycle", 3, (("a", 10, 2),)),
    ("docomo-cycle", 20, (("a", -1, 30), ("a", 6, 6))),
)

#: 古い形（`fetched_at` が無い）のファイルが置かれている時間（W3 プラン §12 の 96）
OLD_FORM = ("hellocycling", 10)


def hour_table(system_id: str, index: int, rows: Sequence[tuple[str, int, int]]) -> pa.Table:
    at = HOURS[index]
    return pa.Table.from_pylist(
        [
            {
                "system_id": system_id,
                "station_id": station_id,
                "observed_at": at,
                "fetched_at": at,
                "bikes": bikes,
                "docks": docks,
                "flags": 7,
                "reported_age_s": 0,
            }
            for station_id, bikes, docks in rows
        ],
        schema=SNAPSHOT_SCHEMA,
    )


def old_form_body() -> bytes:
    """`fetched_at` の無い古い形。**読めても使わない**（容量が過小になる。§12 の 97）。"""
    table = hour_table(*OLD_FORM, (("a", 50, 50),)).drop_columns(["fetched_at"])
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)
    return bytes(sink.getvalue())


class SnapshotPort(FakePort):
    """その日の毎時スナップショットを持つ Storage。**ほかの時間と前の版は無い。**"""

    def __init__(self) -> None:
        super().__init__()
        self.bodies = {
            parquet_path(system_id, HOURS[index]): snapshot_bytes(
                hour_table(system_id, index, rows)
            )
            for system_id, index, rows in HOURLY
        }
        self.bodies[parquet_path(OLD_FORM[0], HOURS[OLD_FORM[1]])] = old_form_body()

    def download(self, bucket: str, path: str) -> bytes | None:
        return self.bodies.get(path)


def joined_day() -> pa.Table:
    """**前のやり方**：1 日ぶんをつなげた表（答え合わせに使う）。"""
    return pa.concat_tables(hour_table(system_id, index, rows) for system_id, index, rows in HOURLY)


def test_folding_the_hours_gives_the_same_capacity_as_joining_the_day() -> None:
    """**1 日ぶんをつなげない**（所見 212）。答えはつなげて取ったものと同じ。"""
    daily_max, _ = load_daily_max(SnapshotPort(), DAY)
    assert daily_max == daily_capacity_max(joined_day())
    assert daily_max == {
        ("hellocycling", "a"): 11,
        ("hellocycling", "b"): 8,
        ("docomo-cycle", "a"): 12,
    }


def test_missing_and_old_hours_are_reported_as_before() -> None:
    """**欠けた時間帯の数と文言は前と同じ**（`missing_hours` の見張りが読む）。古い形は使わない。"""
    _, missing = load_daily_max(SnapshotPort(), DAY)
    served = {(system_id, index) for system_id, index, _ in HOURLY}
    # 並びはシステムごと・時間の順。古い形は「（古い形）」を付けて数える（前のやり方と同じ）
    expected = tuple(
        f"{system_id} {hour:%Y-%m-%dT%H}Z"
        + ("（古い形）" if (system_id, index) == OLD_FORM else "")
        for system_id in SYSTEM_IDS
        for index, hour in enumerate(HOURS)
        if (system_id, index) not in served
    )
    assert missing == expected
    assert len(missing) == 48 - len(HOURLY)


def test_one_hour_is_read_on_its_own() -> None:
    port = SnapshotPort()
    table, label = read_hour(port, "hellocycling", HOURS[5])
    assert label == f"hellocycling {HOURS[5]:%Y-%m-%dT%H}Z"
    assert table is not None and table.num_rows == 2
    assert read_hour(port, "hellocycling", HOURS[1]) == (
        None,
        f"hellocycling {HOURS[1]:%Y-%m-%dT%H}Z",
    )
    old, old_label = read_hour(port, OLD_FORM[0], HOURS[OLD_FORM[1]])
    assert old is None and old_label.endswith("（古い形）")


def test_the_whole_job_writes_the_folded_capacity() -> None:
    """端から端まで：**置く表と DB に写す容量が、畳んだ答えを使う。**"""
    port = SnapshotPort()
    summary = run(port)
    assert summary["missing_hours"] == 48 - len(HOURLY)
    # 台帳にあるのは両システムの "a" だけ（`FakePort`）。前の版が無いので、当日の最大がそのまま入る
    assert summary["with_capacity_est"] == 2
    by_port = {(row["system_id"], row["station_id"]): row for row in port.capacity_rows}
    assert by_port[("hellocycling", "a")]["capacity_est"] == 11
    assert by_port[("docomo-cycle", "a")]["capacity_est"] == 12
