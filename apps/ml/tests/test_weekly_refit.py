"""月曜の朝の当てはめ直し（`jobs/weekly_refit.py`。W8 の PR G、D-45）。

**主題は「止まるべき所で止まり、止まった段が残ること」と「試しの形は何も書かないこと」。**

  * 段は順に進み、**1 つ落ちたらそれより後は動かない**。`job_runs` に `failed` と段の名前
  * **門で諦めたときは、その時刻に行を作る**（3 時間待ってから「始まり 3 時間前の失敗」に
    しない。見張りの窓から外れる）
  * 置いた回は、B3 → 確かめ → 登録 → 合成器を置いて登録 → 試し打ち → 記録 の順
  * **試しの形**：置かず・登録せず・`job_runs` も書かず、試し打ちは今の shadow で叩く
  * 記録には版の名前を差し込んだ昇格の SQL が入り、**差し込み口は残らない**
  * **定時の回はその日の `ok` が在れば何もしない**（W9-13）。手で起こす回はやり直せる

合成器は**本物の `build_composite`** で組む（部品は `tests/composite_fixture.py`）。当てはめ・
評価・点検は、それぞれの検査が別にあるので、ここでは差し替える。
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest

from bikechance_ml.baselines import artifact as baseline_artifact
from bikechance_ml.features.grid import features_path
from bikechance_ml.io.supabase import PARQUET_BUCKET
from bikechance_ml.jobs import drop_cells, fit_baseline, refit_gate, refit_record, register_body
from bikechance_ml.jobs import weekly_refit as job
from bikechance_ml.models import artifact as lightgbm_artifact
from bikechance_ml.models import composite, registry
from bikechance_ml.models import gates as gate_tables
from bikechance_ml.models.gates import CellKey
from tests import composite_fixture as fixture
from tests import test_build_composite, test_eval_harness
from tests.test_dry_run import FakeClock

#: 仕込みの B3 の最終学習日（`tests/eval_fixture.DAYS` の最後）。
DAY: Final[date] = date(2026, 9, 9)
START: Final[datetime] = datetime(2026, 9, 10, 1, 30, tzinfo=UTC)
ACTIVE: Final[registry.Registered] = registry.Registered(
    model_version=fixture.VERSION,
    kind=composite.KIND,
    feature_set="v4",
    artifact_path=composite.artifact_path(fixture.VERSION),
    status="active",
)
SHADOW: Final[registry.Registered] = registry.Registered(
    model_version="baseline-b3-v0-20260902",
    kind=registry.BASELINE_KIND,
    feature_set="v4",
    artifact_path=baseline_artifact.artifact_path("baseline-b3-v0-20260902"),
    status="shadow",
)
WORSE: Final[CellKey] = fixture.LIGHTGBM_CELLS[0]
NEW_B3: Final[str] = fixture.b3().model_version
NEW_COMPOSITE: Final[str] = composite.model_version_for(fixture.b3().train_days, None)

#: 試験のひな形。**昇格を呼ぶ形は書かない**（CI がコードの中のそれを止める）。
TEMPLATE: Final[str] = (
    "-- {day}\nselect up('{composite}');\nselect side('{b3}');\n"
    "-- 前 {previous_active} / {previous_shadow}\n"
)


@dataclass
class FakePort:
    """Storage・登録簿・日次評価・`job_runs` の代役。**した順を `events` に残す。**"""

    objects: dict[tuple[str, str], bytes] = field(default_factory=dict)
    rows: dict[str, registry.Registered] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    registered: list[Mapping[str, object]] = field(default_factory=list)
    finished: list[tuple[int, str, Mapping[str, object]]] = field(default_factory=list)
    metrics: tuple[drop_cells.DailyMetric, ...] = ()
    arrive: datetime = START
    clock: FakeClock | None = None
    #: その日の `weekly_refit` の `ok` の始まり（W9-13）。None なら済んでいない
    refit_done: datetime | None = None
    #: `weekly_refit` の `ok` を引くと落ちる（引く口の不調）
    refit_lookup_fails: bool = False

    def download(self, bucket: str, path: str) -> bytes | None:
        return self.objects.get((bucket, path))

    def upload(self, bucket: str, path: str, body: bytes, content_type: str) -> None:
        self.events.append(f"upload {path}")
        self.objects[(bucket, path)] = body

    def find_model(self, model_version: str) -> registry.Registered | None:
        return self.rows.get(model_version)

    def active_model(self) -> registry.Registered | None:
        return next((one for one in self.rows.values() if one.status == "active"), None)

    def shadow_model(self) -> registry.Registered | None:
        return next((one for one in self.rows.values() if one.status == "shadow"), None)

    def register_model_version(self, row: Mapping[str, object]) -> str:
        self.events.append(f"register {row['model_version']}")
        self.registered.append(row)
        return str(row["model_version"])

    def promoted_at(self, model_version: str) -> datetime | None:
        return datetime(2026, 9, 3, 1, 32, tzinfo=UTC)

    def latest_ok_run(self, job_name: str, date_key: str, day: date) -> datetime | None:
        """**仕事の名前で分ける**：当てはめ直し自身（W9-13）と、門が待つ 3 つ。"""
        if job_name == job.JOB_NAME:
            if self.refit_lookup_fails:
                raise RuntimeError("PostgREST に届かない")
            return self.refit_done if day == DAY else None
        now = START if self.clock is None else self.clock.now()
        return self.arrive if self.arrive <= now and day == DAY else None

    def list_daily_metrics(
        self, versions: Sequence[str], first: date, last: date
    ) -> tuple[drop_cells.DailyMetric, ...]:
        self.events.append(f"metrics {first}..{last}")
        return self.metrics

    def list_active_systems(self) -> tuple[str, ...]:
        return ("docomo-cycle", "hellocycling")

    def job_started(self, job_name: str) -> int:
        self.events.append(f"started {job_name}")
        return 42

    def job_finished(self, run_id: int, status: str, detail: Mapping[str, object]) -> None:
        self.events.append(f"finished {status}")
        self.finished.append((run_id, status, detail))


class FakeInfer:
    """試し打ちの口。**頼んだ版で、合格の要約を返す**（`rss_mb` は変えられる）。"""

    def __init__(self, rss_mb: int = 1_100) -> None:
        self.rss_mb = rss_mb
        self.calls: list[tuple[str, str]] = []

    def call(self, system_id: str, model_version: str) -> tuple[int, Mapping[str, object]]:
        self.calls.append((system_id, model_version))
        body: dict[str, object] = {
            "status": "dry_run",
            "model_version": model_version,
            "predicted": 100,
            "unknown_ports": 0,
            "duration_ms": 15_000,
            "cpu_ms": 11_000,
            "model_load_ms": 0,
            "rss_mb": self.rss_mb,
        }
        return 200, body


def _metrics() -> tuple[drop_cells.DailyMetric, ...]:
    """比べる日の成績。**`WORSE` だけ森が B3 より悪い。**"""
    rows: list[drop_cells.DailyMetric] = []
    for step in range(1, 7):
        day = date(2026, 9, 3) + timedelta(days=step)
        for cell in fixture.LIGHTGBM_CELLS:
            mine = 0.30 if cell == WORSE else 0.10
            for version, brier in ((fixture.VERSION, mine), (SHADOW.model_version, 0.20)):
                rows.append(drop_cells.DailyMetric(version, day, *cell, 50, brier))
    return tuple(rows)


def _port() -> FakePort:
    port = FakePort(rows={ACTIVE.model_version: ACTIVE, SHADOW.model_version: SHADOW})
    bucket = registry.MODEL_BUCKET
    forest = fixture.FOREST.model_version
    port.objects[(bucket, ACTIVE.artifact_path)] = composite.to_bytes(fixture.build())
    port.objects[(bucket, lightgbm_artifact.artifact_path(forest))] = fixture.FOREST_BODY
    port.objects[(bucket, gate_tables.gates_path(forest))] = fixture.gates_body()
    port.objects[(PARQUET_BUCKET, features_path(DAY))] = test_build_composite._day_of_samples()
    port.metrics = _metrics()
    return port


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "docs" / "refits").mkdir(parents=True)
    (repo / job.PROMOTE_TEMPLATE).write_text(TEMPLATE, encoding="utf-8")
    return repo


def _env(
    tmp_path: Path,
    port: FakePort,
    *,
    trial: bool = False,
    infer: FakeInfer | None = None,
    skip_if_done: bool = False,
) -> job.Env:
    clock = FakeClock(START)
    port.clock = clock
    options = job.Options(
        day=DAY,
        trial=trial,
        out=tmp_path / "out",
        repo=_repo(tmp_path),
        gate_wait_s=refit_gate.WAIT_S,
        gate_poll_s=refit_gate.POLL_S,
        skip_if_done=skip_if_done,
    )
    options.out.mkdir(parents=True)
    return job.Env(port=port, infer=infer or FakeInfer(), clock=clock, options=options)


def _stub(monkeypatch: pytest.MonkeyPatch, port: FakePort) -> list[list[str]]:
    """当てはめ・評価・点検・山の測りを差し替える。**`fit_baseline` に渡した引数を返す。**

    当てはめの代役は仕込みの B3 を書き、`--upload` なら `port` に置く（本物の `fit_baseline` と
    同じく、置くのは当てはめた本人）。
    """
    calls: list[list[str]] = []
    facts = register_body.eval_facts(test_eval_harness.OUTCOME, DAY)

    def fit(argv: Sequence[str]) -> int:
        calls.append(list(argv))
        out = Path(argv[argv.index("--out") + 1])
        out.write_bytes(fixture.b3_body())
        if "--upload" in argv:
            path = baseline_artifact.artifact_path(NEW_B3)
            port.upload(registry.MODEL_BUCKET, path, out.read_bytes(), "application/gzip")
        return 0

    def inspected(env: job.Env, label: str, arguments: Sequence[str]) -> refit_record.Inspected:
        return refit_record.Inspected(label, "**合格**")

    monkeypatch.setattr(fit_baseline, "run", fit)
    monkeypatch.setattr(job, "evaluate_one", lambda env, validation: facts)
    monkeypatch.setattr(job, "open_peak_delta_mb", lambda path: 300)
    monkeypatch.setattr(job, "inspect", inspected)
    return calls


def _last_detail(port: FakePort) -> Mapping[str, object]:
    return port.finished[-1][2]


# ── 置く回 ────────────────────────────────────────────────────
def test_a_full_run_places_registers_and_records_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port, infer = _port(), FakeInfer()
    fitted = _stub(monkeypatch, port)
    assert job.execute(_env(tmp_path, port, infer=infer)) == 0
    assert "--upload" in fitted[0]
    order = [one for one in port.events if not one.startswith("metrics")]
    assert order == [
        "started weekly_refit",
        f"upload {baseline_artifact.artifact_path(NEW_B3)}",
        f"register {NEW_B3}",
        f"upload {composite.artifact_path(NEW_COMPOSITE)}",
        f"register {NEW_COMPOSITE}",
        "finished ok",
    ]
    assert [version for _, version in infer.calls] == [NEW_COMPOSITE] * 4


def test_the_worse_cell_is_dropped_from_the_new_composite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _port()
    _stub(monkeypatch, port)
    assert job.execute(_env(tmp_path, port)) == 0
    stored = port.objects[(registry.MODEL_BUCKET, composite.artifact_path(NEW_COMPOSITE))]
    assert composite.from_bytes(stored).dropped == (WORSE,)
    # 比べる日は昇格（09-03）の翌日から最終学習日まで
    assert "metrics 2026-09-04..2026-09-09" in port.events


def test_the_record_and_promotion_sql_are_written_into_the_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _port()
    _stub(monkeypatch, port)
    env = _env(tmp_path, port)
    assert job.execute(env) == 0
    refits = env.options.repo / "docs" / "refits"
    sql = (refits / f"{DAY}-promote.sql").read_text()
    assert f"up('{NEW_COMPOSITE}')" in sql and f"side('{NEW_B3}')" in sql
    assert refit_record.PLACEHOLDER.search(sql) is None, "差し込み口が残らない"
    record = (refits / f"{DAY}.md").read_text()
    assert NEW_COMPOSITE in record and "昇格はまだ" in record and sql.strip() in record
    assert (env.options.repo / "docs" / "model_cards" / f"{NEW_COMPOSITE}.md").exists()
    # **写しも `out` に残す**（ワークフローがログに流す。PR を開く段で落ちても作り直せる）
    for name in ("record.md", "promote.sql", "card.md"):
        assert (env.options.out / name).exists(), name


def test_the_pull_request_lists_the_three_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _port()
    _stub(monkeypatch, port)
    env = _env(tmp_path, port)
    assert job.execute(env) == 0
    pull = json.loads((env.options.out / "pr.json").read_text())
    assert pull["files"] == [
        f"docs/refits/{DAY}.md",
        f"docs/refits/{DAY}-promote.sql",
        f"docs/model_cards/{NEW_COMPOSITE}.md",
    ]
    assert pull["branch"] == f"refit/{DAY}" and pull["trial"] is False


def test_the_registered_b3_points_at_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _port()
    _stub(monkeypatch, port)
    assert job.execute(_env(tmp_path, port)) == 0
    b3_row = port.registered[0]
    assert b3_row["card_path"] == f"docs/refits/{DAY}.md"
    assert b3_row["status"] == "candidate"


def test_the_summary_is_left_in_job_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port = _port()
    _stub(monkeypatch, port)
    assert job.execute(_env(tmp_path, port)) == 0
    assert port.finished[-1][1] == "ok"
    detail = _last_detail(port)
    assert (detail["b3"], detail["composite"], detail["dropped"]) == (NEW_B3, NEW_COMPOSITE, 1)


def test_the_record_shows_the_bss_from_b0(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**評価の表に B0 と B3 の BSS**（W9-13。昇格の目安「BSS +10% 前後」を記録から読む）。

    仕込みの評価は小さな表で、B0 の Brier が 0（完全に当てる）なので、BSS は書けずに「—」になる。
    """
    port = _port()
    _stub(monkeypatch, port)
    env = _env(tmp_path, port)
    assert job.execute(env) == 0
    record = (env.options.repo / "docs" / "refits" / f"{DAY}.md").read_text()
    assert "| 系統/ターゲット | B0 | B1 | B3 | B3 の BSS |" in record
    facts = register_body.eval_facts(test_eval_harness.OUTCOME, DAY)
    key = sorted(facts.brier_b3)[0]
    row = f"| {key} | 0.00000 | {facts.brier_b1[key]:.5f} | {facts.brier_b3[key]:.5f} | — |"
    assert row in record


def test_the_bss_column_is_the_improvement_from_b0() -> None:
    facts = register_body.EvalFacts(
        DAY,
        "分割",
        10,
        {"hellocycling/bike": 0.09},
        {"hellocycling/bike": 0.095},
        {"hellocycling/bike": 0.10},
        (),
        40,
    )
    rows = refit_record._brier_rows(facts)
    assert rows[-1] == "| hellocycling/bike | 0.10000 | 0.09500 | 0.09000 | +10.0% |"


def test_a_zero_reference_brier_writes_no_ratio() -> None:
    assert refit_record._bss(0.01, 0.0) == "—"
    assert refit_record._bss(0.09, 0.10) == "+10.0%"
    assert refit_record._bss(0.11, 0.10) == "-10.0%"


# ── 1 日 1 度だけ（W9-13）────────────────────────────────────
def test_a_scheduled_run_does_nothing_when_the_day_is_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**門より前で何もせずに終わる**：当てはめず・置かず・行も作らず、印だけを書く。"""
    port = _port()
    fitted = _stub(monkeypatch, port)
    port.refit_done = START + timedelta(hours=4)
    env = _env(tmp_path, port, skip_if_done=True)
    assert job.execute(env) == 0
    assert fitted == [], "当てはめない"
    assert port.events == [], "置かず・登録せず・行も作らない"
    marker = json.loads((env.options.out / job.SKIPPED_NAME).read_text())
    assert marker == {"skipped": True, "date": str(DAY), "done_at": port.refit_done.isoformat()}
    assert not (env.options.out / "record.md").exists()
    assert not (env.options.out / "pr.json").exists(), "記録の PR を開かない"


def test_a_scheduled_run_goes_on_when_the_day_is_not_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _port()
    _stub(monkeypatch, port)
    env = _env(tmp_path, port, skip_if_done=True)
    assert job.execute(env) == 0
    assert port.events[0] == "started weekly_refit"
    assert not (env.options.out / job.SKIPPED_NAME).exists()
    assert (env.options.out / "pr.json").exists()


def test_a_manual_run_redoes_a_day_that_is_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**手で起こす回は見ない**（失敗や確認の後のやり直しに要る。`docs/refits/README.md`）。"""
    port = _port()
    _stub(monkeypatch, port)
    port.refit_done = START
    env = _env(tmp_path, port)
    assert job.execute(env) == 0
    assert f"register {NEW_B3}" in port.events
    assert not (env.options.out / job.SKIPPED_NAME).exists()


def test_a_failed_done_lookup_is_recorded_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**済んだか分からないなら進まない**（置き直す危うさを取らない）。`done` の段で `failed`。"""
    port = _port()
    fitted = _stub(monkeypatch, port)
    port.refit_lookup_fails = True
    assert job.execute(_env(tmp_path, port, skip_if_done=True)) == 1
    assert fitted == []
    assert port.events == ["started weekly_refit", "finished failed"]
    detail = _last_detail(port)
    assert (detail["phase"], detail["error"]) == ("done", "done: RuntimeError")


def test_the_cli_flag_reaches_the_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    base = ["--out", str(tmp_path / "out"), "--origin", "https://example.test"]
    now = datetime(2026, 11, 2, 1, 30, tzinfo=UTC)
    plain = job.options_of(job._arguments(base), now)
    flagged = job.options_of(job._arguments([*base, "--skip-if-done"]), now)
    assert (plain.skip_if_done, flagged.skip_if_done) == (False, True)


# ── 試しの形 ──────────────────────────────────────────────────
def test_a_trial_writes_nothing_and_tries_the_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port, infer = _port(), FakeInfer()
    fitted = _stub(monkeypatch, port)
    env = _env(tmp_path, port, trial=True, infer=infer)
    assert job.execute(env) == 0
    assert "--upload" not in fitted[0]
    writes = {"upload", "register", "started", "finished"}
    assert [one for one in port.events if one.split()[0] in writes] == [], "何も書かない"
    assert {version for _, version in infer.calls} == {SHADOW.model_version}
    assert not (env.options.repo / "docs" / "refits" / f"{DAY}.md").exists()
    assert (env.options.out / "record.md").exists()
    assert (env.options.out / "promote.sql").exists()
    assert (env.options.out / "register-b3.json").exists()


# ── 止まる ────────────────────────────────────────────────────
def test_a_gate_timeout_opens_and_closes_a_row_at_that_moment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**門の後に行を作る**：諦めた時刻に始まりと終わりを書く（見張りの 3 時間の窓に入る）。"""
    port = _port()
    fitted = _stub(monkeypatch, port)
    port.arrive = START + timedelta(hours=5)
    assert job.execute(_env(tmp_path, port)) == 1
    assert fitted == [], "当てはめない"
    assert port.events == ["started weekly_refit", "finished failed"]
    detail = _last_detail(port)
    assert (detail["phase"], detail["error"]) == ("gate", "gate: GateTimeoutError")
    assert detail["waited_s"] == refit_gate.WAIT_S
    missing = detail["missing"]
    assert isinstance(missing, list)
    assert set(missing) == {one.job_name for one in refit_gate.REQUIREMENTS}


def test_a_failed_inspection_stops_before_registering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(env: job.Env, label: str, arguments: Sequence[str]) -> refit_record.Inspected:
        raise job.RefitError("点検が不合格です")

    port = _port()
    _stub(monkeypatch, port)
    monkeypatch.setattr(job, "inspect", refuse)
    assert job.execute(_env(tmp_path, port)) == 1
    assert not any(one.startswith("register") for one in port.events)
    assert port.finished[-1][1] == "failed"
    detail = _last_detail(port)
    assert (detail["phase"], detail["error"]) == ("inspect_b3", "inspect_b3: RefitError")


def test_bytes_that_differ_in_storage_stop_at_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _port()
    original = port.upload

    def corrupt(bucket: str, path: str, body: bytes, content_type: str) -> None:
        original(bucket, path, body + b"x", content_type)

    monkeypatch.setattr(port, "upload", corrupt)
    _stub(monkeypatch, port)
    assert job.execute(_env(tmp_path, port)) == 1
    assert _last_detail(port)["phase"] == "verify"


def test_a_heavy_dry_run_stops_before_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _port()
    _stub(monkeypatch, port)
    env = _env(tmp_path, port, infer=FakeInfer(rss_mb=1_700))
    assert job.execute(env) == 1
    assert _last_detail(port)["phase"] == "dry_run"
    assert not (env.options.repo / "docs" / "refits" / f"{DAY}.md").exists()


def test_an_active_that_is_not_a_composite_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _port()
    _stub(monkeypatch, port)
    port.rows[ACTIVE.model_version] = registry.Registered(
        ACTIVE.model_version, registry.BASELINE_KIND, "v4", ACTIVE.artifact_path, "active"
    )
    assert job.execute(_env(tmp_path, port)) == 1
    assert _last_detail(port)["phase"] == "serving"
