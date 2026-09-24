"""学習サンプルの日次ジョブ（`jobs/build_features.py`、W4 プラン §6.8 の PR J）。

**この 1 ファイルの主題は「止まったときに気づけること」。** 組み立ての規則は
`features/build.py` にあり、そちらは `test_features_build.py` が 40 件で固めている。
ここが守るのは 4 つ。

  * **成功も失敗も `job_runs` に記録する**（`check_jobs_missing` から見える）
  * **記録の不調でジョブを落とさない**（置けたことのほうが大事。`compact` と同じ）
  * **失敗は投げ直す**（GitHub Actions の実行が赤くなり、記録にも `failed` が残る）
  * **置かないときは記録しない**（手元の試し打ちで `job_runs` を汚さない）

**止まると取り返せない。** 学習サンプルは導出データだが、作り直せるのは
`weather_hourly` の保持（30 日）のあいだだけである（§8.5.2）。

**代役は本物の組み立てを通す。** 参照スナップショットは本物の Parquet を返し、観測は
**最初の 1 時間ぶんだけ**返す（残りは欠損）。こうすると `build_one_day` が端から端まで
動き、**欠けた時間帯が記録に出る**ことまで一緒に確かめられる。
"""

from collections.abc import Mapping
from contextlib import nullcontext
from datetime import UTC, date, datetime
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from bikechance_ml.features.grid import features_path, jst_yesterday
from bikechance_ml.features.profile import SchemaMismatchError
from bikechance_ml.features.reference_snapshot import (
    NEIGHBORS_NAME,
    STATIONS_NAME,
    to_neighbors_table,
    to_stations_table,
)
from bikechance_ml.features.schema import SCHEMA
from bikechance_ml.features.weather import WeatherRow
from bikechance_ml.jobs import build_features as job
from bikechance_ml.jobs.build_features import (
    JOB_NAME,
    FeaturesPort,
    build_and_upload,
    build_one_day,
)
from bikechance_ml.jobs.snapshot_table import to_parquet_bytes as snapshot_bytes
from tests import features_fixture as fixture

#: フィクスチャの日（2026-09-07 月曜）。天気も観測もこの日ぶんが入っている。
DAY: Final[date] = fixture.DAY
BUILT_AT: Final[datetime] = datetime(2026, 9, 7, 20, 0, tzinfo=UTC)


def _reference_bodies() -> dict[str, bytes]:
    """参照スナップショットを**本物の Parquet** にする（読み直しまで通す）。"""
    systems, estimates, _ = fixture.load_reference()
    return {
        STATIONS_NAME: snapshot_bytes(to_stations_table(systems, estimates, [], built_at=BUILT_AT)),
        NEIGHBORS_NAME: snapshot_bytes(to_neighbors_table(systems, built_at=BUILT_AT)),
    }


REFERENCE: Final[dict[str, bytes]] = _reference_bodies()
SNAPSHOTS: Final[bytes] = snapshot_bytes(fixture.load_snapshots())
#: 前日の版のプロファイル（**本物の Parquet**。読み直しまで通す）
PROFILE: Final[bytes] = snapshot_bytes(fixture.load_profile())


class FakePort:
    """`FeaturesPort` を満たす試験用の入出力。呼ばれた記録を残す。"""

    def __init__(self) -> None:
        self.uploads: list[tuple[str, int]] = []
        self.started: list[str] = []
        self.finished: list[tuple[int, str, Mapping[str, object]]] = []
        self.fail_upload = False
        self.fail_job_started = False
        self.fail_job_finished = False
        self.no_reference = False
        self._served_snapshots = False
        #: 置いてあるプロファイル（None なら「無い」）。**読みに来たパス**は下に残る
        self.profile_body: bytes | None = PROFILE
        self.profile_reads: list[str] = []

    def download(self, bucket: str, path: str) -> bytes | None:
        if path.startswith("reference/"):
            return None if self.no_reference else REFERENCE[path.split("/")[-1].split(".")[0]]
        if path.startswith("profiles/"):
            self.profile_reads.append(path)
            return self.profile_body
        # **観測は 1 時間ぶんだけ返す。** 残りは「その時間帯が無い」＝欠損（補間しない）
        if self._served_snapshots:
            return None
        self._served_snapshots = True
        return SNAPSHOTS

    def list_holidays(self) -> tuple[date, ...]:
        return ()

    def list_weather(self, start: datetime, end: datetime) -> tuple[WeatherRow, ...]:
        return tuple(one for one in fixture.load_weather_rows() if start <= one.available_at < end)

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


def _upload(port: FeaturesPort) -> Mapping[str, object]:
    return build_and_upload(port, DAY).summary


def _rows_of(summary: Mapping[str, object]) -> int:
    """要約の `rows` を整数として取り出す。**`as` を使わずに絞る**（CLAUDE.md §3）。"""
    rows = summary["rows"]
    assert isinstance(rows, int), "rows は整数のはず"
    return rows


# ── 作って置く ────────────────────────────────────────────────
def test_the_day_lands_on_the_expected_path() -> None:
    """**パスは JST の暦日。** 上書きなので、何度走らせても同じ場所に同じものが載る。"""
    port = FakePort()
    _upload(port)
    assert [path for path, _ in port.uploads] == [features_path(DAY)]
    assert port.uploads[0][0] == "features/date=2026-09-07/part.parquet"


def test_what_is_uploaded_is_the_sample_table() -> None:
    """**置いたバイト列が読める Parquet で、列が学習サンプルのものである。**"""
    port = FakePort()
    made = build_and_upload(port, DAY)
    table = pq.read_table(pa.BufferReader(made.body))
    assert table.schema.names == SCHEMA.names
    assert table.num_rows > 0, "代役が観測を 1 時間ぶん返しているので、行が出るはず"


def test_building_without_uploading_touches_nothing() -> None:
    """**手元の試し打ちで `job_runs` を汚さない。** 置かなければ記録もしない。"""
    port = FakePort()
    build_one_day(port, DAY)
    assert (port.uploads, port.started, port.finished) == ([], [], [])


# ── 記録 ──────────────────────────────────────────────────────
def test_a_successful_run_is_recorded() -> None:
    """**回ったことが `job_runs` に残る。** 残らないと監視から見えない。"""
    port = FakePort()
    summary = _upload(port)
    assert port.started == [JOB_NAME]
    assert len(port.finished) == 1
    run_id, status, detail = port.finished[0]
    assert (run_id, status) == (42, "ok")
    assert detail["ok"] is True
    assert detail["date"] == DAY.isoformat()
    assert detail["rows"] == summary["rows"]


def test_the_job_name_is_the_one_monitoring_expects() -> None:
    """`monitored_jobs` の行（0040）と同じ名前でなければ、いつまでも「来ていない」と鳴る。"""
    assert JOB_NAME == "build_features"


#: 記録に必ず出す欄と、その理由。**「うっかり落ちた」と「出さないと決めた」を区別する。**
MUST_BE_RECORDED: Final[Mapping[str, str]] = {
    "date": "どの日を作ったか。名前だけでは分からない",
    "rows": "何行できたか。急に減ったら抽出か除外を疑う",
    "bytes": "置いたものの大きさ。保持の見積り（開発プラン §4.5b）に直に効く",
    "missing_hours": "読めなかった時間帯。**補間しないので、ここに出たぶんは永久に欠ける**",
    "weather_coverage": "天気が入っていた割合（PR I）。版は「列が在る」しか語らない",
    "stations_without_weather": "気象格子に当たらなかったポート数",
    "excluded": "理由ごとの除外件数。行が減ったときの内訳",
    "feature_set": "どの版で作ったか",
    "profile_date": "`prof_*` をどの日の版から引いたか（前日。無ければ null）",
    "profile_coverage": "`prof_*` が入っていた割合（PR J1）。版は「列が在る」しか語らない",
}


def test_every_promised_field_reaches_the_record() -> None:
    """**表に挙げた欄が `job_runs.detail` に出る。** 出ないものは表から消す（黙って減らさない）。"""
    port = FakePort()
    _upload(port)
    _, _, detail = port.finished[0]
    missing = [name for name in MUST_BE_RECORDED if name not in detail]
    assert missing == [], f"記録に出ていない欄がある: {missing}"


def test_the_missing_hours_are_recorded() -> None:
    """**欠けた時間帯が数えられて残る。** 代役は 1 時間ぶんしか返していない。"""
    port = FakePort()
    _upload(port)
    _, _, detail = port.finished[0]
    hours = detail["missing_hours"]
    assert isinstance(hours, list)
    assert len(hours) > 100, "53 時間 × 2 システムのうち 1 つだけ返している"


def test_a_failure_is_recorded_and_raised() -> None:
    """**「動いていない」と「動いたが失敗した」を区別できるようにする。**"""
    port = FakePort()
    port.fail_upload = True
    with pytest.raises(RuntimeError):
        _upload(port)
    assert len(port.finished) == 1
    _, status, detail = port.finished[0]
    assert status == "failed"
    # 例外の**種類だけ**を残す（文言に接続先が混じる経路を作らない）
    assert detail["error"] == "RuntimeError"
    assert "storage down" not in str(detail)


def test_a_missing_reference_is_recorded_and_raised() -> None:
    """**参照スナップショットが無ければ止める**（「いまの値」で代用しない）。"""
    port = FakePort()
    port.no_reference = True
    with pytest.raises(RuntimeError):
        _upload(port)
    _, status, detail = port.finished[0]
    assert status == "failed"
    assert detail["error"] == "MissingReferenceError"
    assert port.uploads == [], "作れていないのに置いてはいけない"


def test_recording_the_start_can_fail_without_stopping_the_work() -> None:
    """記録の不調で 1 日ぶんを落とさない（`compact` と同じ方針）。"""
    port = FakePort()
    port.fail_job_started = True
    summary = _upload(port)
    assert _rows_of(summary) > 0
    assert len(port.uploads) == 1
    # 始まりを記録できなければ、終わりも書かない（宙ぶらりんの run_id を作らない）
    assert port.finished == []


def test_recording_the_end_can_fail_without_stopping_the_work() -> None:
    port = FakePort()
    port.fail_job_finished = True
    assert _rows_of(_upload(port)) > 0
    assert len(port.uploads) == 1


# ── 既定の対象日 ──────────────────────────────────────────────
def test_the_default_day_is_yesterday_in_jst() -> None:
    """**06:00 JST に走らせて前日ぶんを作る。** `--date` を省いたときの既定。

    21:00 UTC は翌日の 06:00 JST なので、前日は**その UTC 日そのもの**になる。
    """
    assert jst_yesterday(datetime(2026, 9, 11, 21, 0, tzinfo=UTC)) == date(2026, 9, 11)
    assert jst_yesterday(datetime(2026, 9, 11, 14, 0, tzinfo=UTC)) == date(2026, 9, 10)


def test_the_command_line_takes_a_date_and_defaults_to_none() -> None:
    """**空なら昨日。** GitHub Actions の `workflow_dispatch` は空文字なら `--date` を付けない。"""
    assert job._arguments([]).date is None
    assert job._arguments(["--date", "2026-09-10"]).date == "2026-09-10"
    assert job._arguments([]).upload is False
    assert job._arguments(["--upload"]).upload is True


# ── 入口（`run`）──────────────────────────────────────────────
def _patched(monkeypatch: pytest.MonkeyPatch) -> FakePort:
    """`run` の入口を代役に差し替える。

    **入口から入れないと、呼び出し側の取り違えを捕まえられない。** 実際、`build_one_day`
    と `jst_yesterday` を直接試すだけでは、**`run` がそれを使うのをやめても素通りした**
    （PR E′ の `card_reference`、PR I の `prepare` と同じ形）。
    """
    port = FakePort()
    monkeypatch.setattr(job, "read_storage_config", lambda: None)
    monkeypatch.setattr(job, "open_storage", lambda config: nullcontext(port))
    return port


def test_run_without_upload_writes_nothing_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """**`--upload` を付けなければ、Storage にも `job_runs` にも触らない。**"""
    port = _patched(monkeypatch)
    assert job.run(["--date", DAY.isoformat()]) == 0
    assert (port.uploads, port.started, port.finished) == ([], [], [])


def test_run_with_upload_places_and_records(monkeypatch: pytest.MonkeyPatch) -> None:
    port = _patched(monkeypatch)
    assert job.run(["--date", DAY.isoformat(), "--upload"]) == 0
    assert [path for path, _ in port.uploads] == [features_path(DAY)]
    assert port.started == [JOB_NAME]
    assert port.finished[0][1] == "ok"


def test_run_without_a_date_builds_yesterday(monkeypatch: pytest.MonkeyPatch) -> None:
    """**入口が `jst_yesterday` を通っている。** 記録に残る日付で確かめる。"""
    port = _patched(monkeypatch)
    assert job.run(["--upload"]) == 0
    _, _, detail = port.finished[0]
    assert detail["date"] == jst_yesterday(datetime.now(UTC)).isoformat()


# ── ポートプロファイル（W5 プラン §6.10 の PR J1）────────────────
def test_the_job_reads_the_profile_of_the_day_before() -> None:
    """**読むのは前日の版だけ**（`profile.source_day`）。当日の版を読むと答えを見る。"""
    port = FakePort()
    summary = _upload(port)
    assert port.profile_reads == ["profiles/date=2026-09-06/profile.parquet"]
    assert summary["profile_date"] == "2026-09-06"
    coverage = summary["profile_coverage"]
    assert isinstance(coverage, dict)
    assert 0 < coverage["ratio"] < 1


def test_the_job_goes_on_without_a_profile() -> None:
    """**前日の版が無くても作り切る**（`prof_*` は NULL）。**無かったことは記録に出る。**

    天気と同じ扱いである——止めると、プロファイルの 1 日の失敗が学習サンプルの
    欠けに化ける。前々日の版には落ちない（黙って古いものを使わない）。
    """
    port = FakePort()
    port.profile_body = None
    made = build_and_upload(port, DAY)
    table = pq.read_table(pa.BufferReader(made.body))
    assert table.num_rows > 0
    assert set(table.column("prof_n_days").to_pylist()) == {0}
    assert port.profile_reads == ["profiles/date=2026-09-06/profile.parquet"]
    _, status, detail = port.finished[0]
    assert status == "ok"
    assert detail["profile_date"] is None
    coverage = detail["profile_coverage"]
    assert isinstance(coverage, dict)
    assert coverage["covered"] == 0


def test_a_profile_of_another_shape_stops_the_job() -> None:
    """**形の違う表は読まない**（`require_schema`）。失敗は記録してから投げ直す。"""
    port = FakePort()
    port.profile_body = REFERENCE[STATIONS_NAME]
    with pytest.raises(SchemaMismatchError):
        build_and_upload(port, DAY)
    _, status, detail = port.finished[0]
    assert (status, detail["error"]) == ("failed", "SchemaMismatchError")
    assert port.uploads == []
