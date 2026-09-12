"""ポートプロファイルの日次ジョブ（`jobs/build_profiles.py`、W5 プラン §6.2 の PR B）。

**この 1 ファイルの主題は「転がしの前提が崩れたときに気づけること」。** 数え方は
`features/profile.py` にあり、そちらは `test_features_profile.py` が 25 件で固めている。
ここが守るのは 5 つ。

  * **読むのは 3 つだけ**（前日の `profile`・当日の観測・28 日前の `daily`）
  * **前日の版が無くても止まらない**（さかのぼって作るときの初日）
  * **成功も失敗も `job_runs` に記録する**（`check_jobs_missing` から見える）
  * **記録の不調でジョブを落とさない**（置けたことのほうが大事）
  * **置かないときは記録しない**（手元の試し打ちで `job_runs` を汚さない）

**読む時間帯は学習より短い。** ラベルも同時刻履歴も要らないので、25 時間ではなく
2 時間の余白で足りる（`features/profile.LOOKBACK_HOURS`）。**読みすぎていないこと**も
ここで固定する——1 日あたり 46 ファイル（学習）と 6 ファイルでは費用が違う。
"""

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from bikechance_ml.features import profile
from bikechance_ml.features.grid import day_start, jst_yesterday, parquet_hours, profile_path
from bikechance_ml.jobs import build_profiles as job
from bikechance_ml.jobs.build_profiles import JOB_NAME, build_and_upload, build_one_day
from bikechance_ml.jobs.snapshot_table import SCHEMA as SNAPSHOT_SCHEMA
from bikechance_ml.jobs.snapshot_table import to_parquet_bytes as snapshot_bytes

DAY: Final[date] = date(2026, 9, 7)  # 月曜
OPEN: Final[int] = 7
CADENCE_S: Final[int] = 150


def _snapshot_bytes(hour: datetime) -> bytes:
    """その 1 時間ぶんの観測（2 ポート）。**本番と同じ長形式。**"""
    stamps = [hour + timedelta(seconds=step * CADENCE_S) for step in range(3600 // CADENCE_S)]
    rows = [("hellocycling", "a", 3, 4), ("hellocycling", "b", 0, 9)]
    return snapshot_bytes(
        pa.table(
            {
                "system_id": [one[0] for one in rows for _ in stamps],
                "station_id": [one[1] for one in rows for _ in stamps],
                "observed_at": [at for _ in rows for at in stamps],
                "fetched_at": [at for _ in rows for at in stamps],
                "bikes": [one[2] for one in rows for _ in stamps],
                "docks": [one[3] for one in rows for _ in stamps],
                "flags": [OPEN for _ in rows for _ in stamps],
                "reported_age_s": [0 for _ in rows for _ in stamps],
            },
            schema=SNAPSHOT_SCHEMA,
        )
    )


class FakePort:
    """`ProfilesPort` を満たす試験用の入出力。**読みに来たパスを全部覚える。**"""

    def __init__(
        self, *, hours: int = 24, days: tuple[date, ...] = (DAY, DAY + timedelta(days=1))
    ) -> None:
        self.hours = hours
        self.days = days
        self.reads: list[str] = []
        self.uploads: dict[str, bytes] = {}
        self.started: list[str] = []
        self.finished: list[tuple[int, str, Mapping[str, object]]] = []
        self.stored: dict[str, bytes] = {}
        self.fail_upload = False
        self.fail_job_started = False
        self.fail_job_finished = False

    def download(self, bucket: str, path: str) -> bytes | None:
        self.reads.append(path)
        if path in self.stored:
            return self.stored[path]
        if not path.endswith("part.parquet"):
            return None
        hour = _hour_of(path)
        served = any(
            day_start(one) <= hour < day_start(one) + timedelta(hours=self.hours)
            for one in self.days
        )
        return _snapshot_bytes(hour) if served else None

    def list_holidays(self) -> tuple[date, ...]:
        return ()

    def upload_parquet(self, path: str, body: bytes) -> None:
        if self.fail_upload:
            raise RuntimeError("storage down")
        self.uploads[path] = body
        self.stored[path] = body

    def job_started(self, job_name: str) -> int:
        if self.fail_job_started:
            raise RuntimeError("db down")
        self.started.append(job_name)
        return 42

    def job_finished(self, run_id: int, status: str, detail: Mapping[str, object]) -> None:
        if self.fail_job_finished:
            raise RuntimeError("db down")
        self.finished.append((run_id, status, detail))


def _hour_of(path: str) -> datetime:
    """`hellocycling/date=2026-09-07/hour=03/part.parquet` → その UTC の正時。"""
    parts = dict(one.split("=") for one in path.split("/") if "=" in one)
    return datetime.fromisoformat(f"{parts['date']}T{parts['hour']}:00:00+00:00")


def _detail(port: FakePort) -> Mapping[str, object]:
    assert len(port.finished) == 1
    return port.finished[0][2]


def _int_of(summary: Mapping[str, object], name: str) -> int:
    """要約の数を整数として取り出す。**`as` を使わずに絞る**（CLAUDE.md §3）。"""
    value = summary[name]
    assert isinstance(value, int), f"{name} は整数のはず"
    return value


# ── 作って置く ────────────────────────────────────────────────
def test_both_files_land_on_the_expected_paths() -> None:
    """**`daily` と `profile` を置く。** 読むのは `profile` で、`daily` は 28 日後に引く。"""
    port = FakePort()
    build_and_upload(port, DAY)
    assert sorted(port.uploads) == [
        profile_path(DAY, profile.DAILY_NAME),
        profile_path(DAY, profile.PROFILE_NAME),
    ]


def test_what_is_uploaded_can_be_read_back() -> None:
    """**置いたバイト列が読める Parquet で、列が契約どおりである。**"""
    port = FakePort()
    build_and_upload(port, DAY)
    daily = pq.read_table(pa.BufferReader(port.uploads[profile_path(DAY, profile.DAILY_NAME)]))
    rolled = pq.read_table(pa.BufferReader(port.uploads[profile_path(DAY, profile.PROFILE_NAME)]))
    assert daily.schema == profile.DAILY_SCHEMA
    assert rolled.schema == profile.PROFILE_SCHEMA
    assert daily.num_rows > 0
    assert set(rolled.column("n_days").to_pylist()) == {1}, "初日なので 1 日ぶん"


def test_building_without_uploading_touches_nothing() -> None:
    """**手元の試し打ちで `job_runs` を汚さない。** 置かなければ記録もしない。"""
    port = FakePort()
    build_one_day(port, DAY)
    assert (port.uploads, port.started, port.finished) == ({}, [], [])


# ── 読むもの ──────────────────────────────────────────────────
def test_only_three_things_are_read() -> None:
    """**前日の `profile`・当日の観測・28 日前の `daily`。** それ以外は読まない。

    参照スナップショットも天気も読まない——**数えるのに要らないものを前提にしない**
    ので、`build_reference` の後でなくても走れる。
    """
    port = FakePort()
    build_one_day(port, DAY)
    kinds = {one.split("/")[0] for one in port.reads}
    assert kinds == {"profiles", "hellocycling", "docomo-cycle"}
    profiles = [one for one in port.reads if one.startswith("profiles/")]
    assert sorted(profiles) == [
        profile_path(DAY - timedelta(days=profile.PROFILE_DAYS), profile.DAILY_NAME),
        profile_path(DAY - timedelta(days=1), profile.PROFILE_NAME),
    ]


def test_the_read_window_is_shorter_than_the_training_one() -> None:
    """**2 時間の余白で足りる**（学習は 25 時間）。読みすぎは費用になる。"""
    port = FakePort()
    build_one_day(port, DAY)
    hourly = [one for one in port.reads if one.endswith("part.parquet")]
    expected = len(parquet_hours(DAY, profile.LOOKBACK_HOURS, 0)) * 2
    assert len(hourly) == expected
    # 学習の窓（25 時間 + 3 時間 + 余白）より確かに短い
    assert expected < len(parquet_hours(DAY)) * 2


def test_a_missing_hour_is_counted_not_interpolated() -> None:
    """**補間しない**（CLAUDE.md §6）。読めなかった時間帯は数で残る。"""
    port = FakePort(hours=6)
    made = build_one_day(port, DAY)
    assert _int_of(made.summary, "missing_hours") > 0
    assert made.daily.num_rows > 0, "読めた時間帯ぶんは数える"


# ── 転がす ────────────────────────────────────────────────────
def test_the_second_day_carries_the_first() -> None:
    """**前日の版に足す。** 読むのは 3 ファイルだけ（28 日ぶんを読み直さない）。"""
    port = FakePort()
    build_and_upload(port, DAY)
    second = build_and_upload(port, DAY + timedelta(days=1))
    assert second.summary["carried"] is True
    assert set(second.profile.column("n_days").to_pylist()) == {2}


def test_the_first_day_has_nothing_to_carry() -> None:
    """**前日の版が無くても止まらない。** さかのぼって作るときの初日がこれ。"""
    port = FakePort()
    made = build_and_upload(port, DAY)
    assert made.summary["carried"] is False
    assert _int_of(made.summary, "usable_cells") == 0, "1 日では下限（2 日）に届かない"


def test_the_summary_says_how_many_cells_are_usable() -> None:
    """**読む側が実際に使えるセルの数**を出す。0 のままなら気候値は B1 に落ちる。"""
    port = FakePort()
    build_and_upload(port, DAY)
    made = build_and_upload(port, DAY + timedelta(days=1))
    assert _int_of(made.summary, "usable_cells") == made.profile.num_rows
    assert made.summary["min_days"] == profile.MIN_CELL_DAYS


# ── 記録 ──────────────────────────────────────────────────────
def test_a_successful_run_is_recorded() -> None:
    """**回ったことが `job_runs` に残る。** 残らないと監視から見えない。"""
    port = FakePort()
    build_and_upload(port, DAY)
    assert port.started == [JOB_NAME]
    run_id, status, detail = port.finished[0]
    assert (run_id, status) == (42, "ok")
    assert detail["ok"] is True
    assert detail["date"] == DAY.isoformat()


def test_the_job_name_is_the_one_monitoring_expects() -> None:
    """`monitored_jobs` の行（0043）と同じ名前でなければ、いつまでも「来ていない」と鳴る。"""
    assert JOB_NAME == "build_profiles"


#: 記録に必ず出す欄と、その理由。**「うっかり落ちた」と「出さないと決めた」を区別する。**
MUST_BE_RECORDED: Final[Mapping[str, str]] = {
    "date": "どの日を作ったか。名前だけでは分からない",
    "daily_cells": "その日ぶんのセル数。急に減ったら観測か除外を疑う",
    "cells": "累計のセル数。3 曜日種別ぶん揃えば約 3 倍になる",
    "usable_cells": "**下限を満たしたセル数。** ここが 0 なら気候値は B1 に落ちる",
    "by_dow_type": "**曜日種別ごとの内訳。** 週末が 0 のままかどうかがここに出る",
    "days_max": "いちばん厚いセルの日数。28 に近づくほど窓が満ちている",
    "carried": "前日の版を引き継げたか。false が続けば転がしが切れている",
    "missing_hours": "読めなかった時間帯。**補間しないので、ここに出たぶんは欠ける**",
    "ports": "数えたポート数。台帳ではなく**その日の観測**から決まる",
    "suspended_share": "休止していた格子点の割合。**分母の取り方の判断材料**（PR D）",
    "bytes": "置いたものの大きさ。保持の見積り（開発プラン §4.5b）に直に効く",
}


@pytest.mark.parametrize("name", sorted(MUST_BE_RECORDED))
def test_the_record_carries_what_we_need_to_diagnose(name: str) -> None:
    """**足したものが記録に届くこと。** W4 の §8.5.8（計算したのに出ない）の再発を防ぐ。"""
    port = FakePort()
    build_and_upload(port, DAY)
    assert name in _detail(port), f"{name} が記録に無い（{MUST_BE_RECORDED[name]}）"


def test_a_failure_is_recorded_and_re_raised() -> None:
    """**失敗も記録してから投げ直す。** 記録が無いと「動いていない」と区別できない。"""
    port = FakePort()
    port.fail_upload = True
    with pytest.raises(RuntimeError):
        build_and_upload(port, DAY)
    run_id, status, detail = port.finished[0]
    assert (run_id, status) == (42, "failed")
    assert detail["error"] == "RuntimeError"


def test_the_record_carries_only_the_exception_name() -> None:
    """**例外の文言は接続先や鍵を抱えうる**（CLAUDE.md §5）。種類だけにする。"""
    port = FakePort()
    port.fail_upload = True
    with pytest.raises(RuntimeError):
        build_and_upload(port, DAY)
    assert "storage down" not in str(port.finished[0][2])


def test_a_broken_recorder_does_not_stop_the_job() -> None:
    """**置けたことのほうが大事。** `job_started` が落ちても作って置く。"""
    port = FakePort()
    port.fail_job_started = True
    build_and_upload(port, DAY)
    assert len(port.uploads) == 2
    assert port.finished == [], "run_id が無いので終わりも書かない"


def test_a_broken_finish_does_not_stop_the_job() -> None:
    port = FakePort()
    port.fail_job_finished = True
    build_and_upload(port, DAY)
    assert len(port.uploads) == 2


# ── 既定 ──────────────────────────────────────────────────────
def test_the_default_day_is_yesterday() -> None:
    """**既定は前日ぶん。** `build_features` と同じ関数を通る（正を 2 つ作らない）。"""
    assert jst_yesterday(datetime.fromisoformat("2026-09-13T07:55:00+09:00")) == date(2026, 9, 12)


def test_the_module_has_a_job_name_so_recording_covers_it() -> None:
    """`tests/test_recording.py` が `JOB_NAME` を持つモジュールを数え上げている。"""
    assert job.JOB_NAME == JOB_NAME
