"""ふだんの傾向のジョブ（`jobs/build_trend.py`。W9 の PR B、開発プランの D-47）。

守りたいのは 5 つ。
  * **写す回は成功も失敗も `job_runs` に記録する**（`monitored_jobs` の `build_trend` が見る）
  * **写さない回は何も書かない**（DB にも `job_runs` にも）
  * **P の日次が無ければ写さない**（P の版を名乗る表を作らない）
  * **定時の 2 回目は、その日の `ok` が在れば何もしない**（`--skip-if-current`）
  * **欠けた日は飛ばし、要約に日付で出す**（補間しない）
"""

import io
import json
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from typing import Final

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from bikechance_ml.features import profile, trend
from bikechance_ml.features.grid import profile_path
from bikechance_ml.io.supabase import PARQUET_BUCKET
from bikechance_ml.jobs import build_trend as job

P: Final[date] = date(2026, 10, 31)
SYSTEMS: Final[tuple[str, ...]] = ("docomo-cycle", "hellocycling")


def dow_of(day: date) -> str:
    return {5: "sat", 6: "sun_holiday"}.get(day.weekday(), "weekday")


def daily_bytes(day: date, seed: int) -> bytes:
    """2 系統 × 4 ポート × 96 枠の日次。**ポートごとに「ある」割合の違う乱数**。"""
    rng = np.random.default_rng(seed)
    rows: dict[str, list[object]] = {name: [] for name in profile.DAILY_SCHEMA.names}
    for system_id in SYSTEMS:
        for index in range(4):
            base = 0.2 + 0.2 * index
            for slot in range(96):
                n = int(rng.integers(1, 4))
                rows["system_id"].append(system_id)
                rows["station_id"].append(f"s{index}")
                rows["dow_type"].append(dow_of(day))
                rows["slot15"].append(slot)
                rows["n"].append(n)
                rows["n_bike_ok"].append(int(rng.binomial(n, base)))
                rows["n_dock_ok"].append(int(rng.binomial(n, 1 - base)))
    for name in ("n_suspended", "sum_bikes", "sum_bikes_sq", "sum_rentals_60", "sum_returns_60"):
        rows[name] = [0] * len(rows["n"])
    sink = io.BytesIO()
    pq.write_table(pa.table(rows, schema=profile.DAILY_SCHEMA), sink)
    return sink.getvalue()


class FakePort:
    """`TrendPort` を満たす試験用の入出力。呼ばれた記録を残す。"""

    def __init__(self, days: Sequence[date], *, done_today: bool = False) -> None:
        self.objects = {
            profile_path(day, profile.DAILY_NAME): daily_bytes(day, day.toordinal()) for day in days
        }
        self.done_today = done_today
        self.upserts: list[int] = []
        self.started: list[str] = []
        self.finished: list[tuple[int, str, Mapping[str, object]]] = []
        self.fail_upsert = False

    def download(self, bucket: str, path: str) -> bytes | None:
        assert bucket == PARQUET_BUCKET
        return self.objects.get(path)

    def upsert_station_trend(self, rows: Sequence[Mapping[str, object]]) -> int:
        if self.fail_upsert:
            raise RuntimeError("db down")
        self.upserts.append(len(rows))
        return len(rows)

    def latest_ok_run(self, job_name: str, date_key: str, day: date) -> datetime | None:
        assert (job_name, date_key) == (job.JOB_NAME, "date")
        return datetime(2026, 11, 1, 2, 0) if self.done_today else None

    def job_started(self, job_name: str) -> int:
        self.started.append(job_name)
        return len(self.started)

    def job_finished(self, run_id: int, status: str, detail: Mapping[str, object]) -> None:
        self.finished.append((run_id, status, detail))


def all_days(skip: Sequence[date] = ()) -> list[date]:
    return [day for day in trend.TrendWindows(P).daily_days() if day not in skip]


# ── 写す回 ──────────────────────────────────────────────────
def test_a_written_run_is_recorded_with_every_group() -> None:
    port = FakePort(all_days())
    summary = job.build_and_write(port, P)
    assert port.started == [job.JOB_NAME]
    assert [status for _, status, _ in port.finished] == ["ok"]
    detail = port.finished[0][2]
    # **2 系統 × 2 ターゲットの 4 組とも測ってある**（出すかどうかは門が決める）
    groups = detail["groups"]
    assert isinstance(groups, dict)
    assert sorted(groups) == [f"{s}/{t}" for s in SYSTEMS for t in trend.TARGETS]
    assert detail["rows"] == 8
    assert detail["written"] == 8
    assert detail["dailies_read"] == 43
    assert detail["dailies_missing"] == []
    assert summary["written"] == 8


def test_rows_are_sent_in_batches() -> None:
    rows: list[Mapping[str, object]] = [{"i": one} for one in range(2_500)]
    assert [len(one) for one in job.batches(rows, job.UPSERT_BATCH)] == [1_000, 1_000, 500]


def test_a_failed_write_is_recorded_and_raised() -> None:
    port = FakePort(all_days())
    port.fail_upsert = True
    with pytest.raises(RuntimeError, match="db down"):
        job.build_and_write(port, P)
    _, status, detail = port.finished[0]
    # **例外の種類だけ**（文言に接続先が混じる経路を作らない）
    assert (status, detail) == ("failed", {"date": P.isoformat(), "error": "RuntimeError"})


def test_a_missing_daily_for_the_day_is_not_written() -> None:
    """**P の日次が無ければ写さない**（P の版を名乗る表を作らない）。"""
    port = FakePort(all_days(skip=[P]))
    with pytest.raises(job.MissingDailyError):
        job.build_and_write(port, P)
    assert port.upserts == []
    assert port.finished[0][1] == "failed"


def test_missing_days_are_skipped_and_listed() -> None:
    gap = P - timedelta(days=3)
    port = FakePort(all_days(skip=[gap]))
    built = job.build(port, P)
    assert built.summary["dailies_missing"] == [gap.isoformat()]
    assert built.summary["dailies_read"] == 42


def empty_daily() -> bytes:
    sink = io.BytesIO()
    pq.write_table(profile.DAILY_SCHEMA.empty_table(), sink)
    return sink.getvalue()


def test_empty_dailies_are_skipped_like_missing_ones() -> None:
    """**行の無い日次（1 日まるごと観測の無い日）は、無い日と同じに飛ばす**（落とさない）。"""
    gap = P - timedelta(days=5)
    port = FakePort(all_days())
    port.objects[profile_path(gap, profile.DAILY_NAME)] = empty_daily()
    built = job.build(port, P)
    assert built.summary["dailies_empty"] == [gap.isoformat()]
    assert built.summary["dailies_missing"] == []
    assert built.summary["rows"] == 8


def test_an_empty_daily_for_the_day_is_not_written() -> None:
    port = FakePort(all_days())
    port.objects[profile_path(P, profile.DAILY_NAME)] = empty_daily()
    with pytest.raises(job.MissingDailyError):
        job.build(port, P)


# ── CLI ─────────────────────────────────────────────────────
def _run(argv: Sequence[str], port: FakePort, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(job, "read_storage_config", lambda: None)
    monkeypatch.setattr(job, "open_storage", lambda config: nullcontext(port))
    return job.run(argv)


def test_the_cli_without_write_records_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    port = FakePort(all_days())
    assert _run(["--date", P.isoformat()], port, monkeypatch) == 0
    assert (port.upserts, port.started, port.finished) == ([], [], [])
    printed = json.loads(capsys.readouterr().out)
    assert (printed["wrote"], printed["written"], printed["rows"]) == (False, 0, 8)


def test_the_cli_skips_when_the_day_is_already_written(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """**定時の 2 回目は何もしない**（00:40 の回が写し終えている）。"""
    port = FakePort(all_days(), done_today=True)
    argv = ["--date", P.isoformat(), "--write", "--skip-if-current"]
    assert _run(argv, port, monkeypatch) == 0
    assert (port.upserts, port.started) == ([], [])
    assert json.loads(capsys.readouterr().out) == {"skipped": True, "date": P.isoformat()}


def test_the_cli_writes_when_the_day_is_not_written_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    port = FakePort(all_days())
    argv = ["--date", P.isoformat(), "--write", "--skip-if-current"]
    assert _run(argv, port, monkeypatch) == 0
    assert port.upserts == [8]
    assert [status for _, status, _ in port.finished] == ["ok"]
