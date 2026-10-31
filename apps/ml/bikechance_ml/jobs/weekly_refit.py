"""月曜の朝の当てはめ直し（W8 の PR G。D-45、W8 プランの W8-01〜06）。

#5（W8 プラン §4.1・§8 の 10/26）で手で通した手順を、**人の手を使わずに候補まで**進める。
**昇格はしない**——人が記録の PR を読み、確認を取ってから SQL を流す（W8-04、CLAUDE.md §6）。

| 段（`phase`） | すること | 使うもの |
|---|---|---|
| `gate` | 最終学習日の 3 つの `ok` を待つ（3 時間まで） | `refit_gate` |
| `fit_b3` | B3 を 7 日で当てはめて置く | `fit_baseline` の CLI そのもの |
| `verify` | 置いたバイト列が手元と同じか | Storage |
| `inspect_b3` | 曜日種別ごとのセル（合格だけ先へ） | `scripts/inspect-artifact.py --file` |
| `evaluate` | 検証日を日曜と土曜にした評価 | `evaluate_baselines.prepare`・`harness` |
| `register_b3` | B3 を candidate で登録 | `register_body` |
| `drop` | 削るセル（日次評価の直近 7 日。W6-12） | `drop_cells` |
| `compose` | 合成器を組んで置いて登録（森は active のまま） | `build_composite` の関数そのもの |
| `inspect_composite` | 部品と組・特徴量の版 | `scripts/inspect-artifact.py` |
| `dry_run` | 本番の口で試し打ち（両系統 2 回、何も書かない） | `dry_run` |
| `record` | 記録と昇格の SQL を書く | `refit_record` |

**各段は今の CLI の関数を呼ぶ**（二つ目の実装を作らない。W8 プラン §4.6）。失敗は段の名前で
包み、`job_runs` の `weekly_refit` に `failed`（`detail.phase`）で残す——`monitor_jobs` の
`job_failed:` が鳴り（W8-05）、止まった段から手で続けられる。**門で待つ間は記録を始めない**
（`refit_gate` の冒頭）。

**試しの形**（`--trial`）は**何も書かない**：置かず・登録せず・`job_runs` にも残さない。合成器は
手元に集めた部品で組み、試し打ちは今の shadow で叩く（候補は登録していないので本番の口で
引けない）。**PR を開くのはワークフロー**（`weekly-refit.yml`）で、ここは記録のファイルを書くまで。

使い方（環境変数は `.env` から読み込んでから）:

    cd apps/ml
    ./.venv/bin/python -m bikechance_ml.jobs.weekly_refit --trial --out .cache/refit \\
        --origin https://bike-chance.vercel.app
"""

import argparse
import hashlib
import json
import math
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final, Protocol

from bikechance_ml.baselines import artifact as baseline_artifact
from bikechance_ml.config import read_config, read_storage_config
from bikechance_ml.eval import harness
from bikechance_ml.eval.report import render_markdown
from bikechance_ml.features.grid import features_path, jst_date, jst_yesterday
from bikechance_ml.io.infer_client import open_infer_client
from bikechance_ml.io.supabase import PARQUET_BUCKET, open_storage
from bikechance_ml.jobs import (
    build_composite,
    drop_cells,
    dry_run,
    evaluate_baselines,
    fit_baseline,
    recording,
    refit_gate,
    refit_record,
    register_body,
)
from bikechance_ml.jobs.cards import card_reference, expand, repo_root
from bikechance_ml.jobs.clock import Clock, SystemClock
from bikechance_ml.jobs.window import days_between
from bikechance_ml.models import artifact as lightgbm_artifact
from bikechance_ml.models import composite, registry
from bikechance_ml.models import gates as gate_tables
from bikechance_ml.redact import redact

#: `job_runs` の名前（W8-05）。
JOB_NAME: Final[str] = "weekly_refit"

#: 評価の窓（検証日から何日前まで読むか）：学習 7 日・空け 1 日・検証 1 日（#5 と同じ）。
EVAL_SPAN_DAYS: Final[int] = 8

#: 点検 1 回の上限（秒）。
INSPECT_TIMEOUT_S: Final[int] = 600

#: 成果物を開いたときの山を測る 1 回の上限（秒）。
OPEN_PEAK_TIMEOUT_S: Final[int] = 300

#: 記録の置き場所（リポジトリからの相対）。
REFITS_DIR: Final[str] = "docs/refits"
CARDS_DIR: Final[str] = "docs/model_cards"
PROMOTE_TEMPLATE: Final[str] = f"{REFITS_DIR}/promote.sql.template"
INSPECT_SCRIPT: Final[str] = "scripts/inspect-artifact.py"

#: 評価を `evaluate_baselines.prepare` に渡す指定（検証 1 日・空け 1 日・B2 はプロファイルから）。
EVAL_OPTIONS: Final[argparse.Namespace] = argparse.Namespace(
    eval_days=1, purge_days=1, no_profile=False
)

#: 配信と同じ読み手で成果物を開き、山の上がり幅を出す（別のプロセスで測る。#4・#5 と同じ）。
OPEN_PEAK_CODE: Final[str] = (
    "import resource, sys\n"
    "from pathlib import Path\n"
    "from bikechance_ml.baselines.artifact import from_bytes\n"
    "body = Path(sys.argv[1]).read_bytes()\n"
    "before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n"
    "artifact = from_bytes(body)\n"
    "after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n"
    "print(after - before)\n"
)


class RefitError(RuntimeError):
    """段の中で、先へ進めない形が見つかった（点検が不合格・版が合わない など）。"""


class PhaseError(RuntimeError):
    """段の失敗。**どの段で止まったか**を持つ（`job_runs.detail.phase`）。"""

    def __init__(self, phase: str, cause: BaseException) -> None:
        self.phase = phase
        self.error_name = type(cause).__name__
        super().__init__(f"{phase} で止まりました: {self.error_name}")


class RefitPort(Protocol):
    """入出力の口（`SupabaseIo`）。登録簿・成果物・日次評価・`job_runs`。"""

    def download(self, bucket: str, path: str) -> bytes | None: ...
    def upload(self, bucket: str, path: str, body: bytes, content_type: str) -> None: ...
    def find_model(self, model_version: str) -> registry.Registered | None: ...
    def active_model(self) -> registry.Registered | None: ...
    def shadow_model(self) -> registry.Registered | None: ...
    def register_model_version(self, row: Mapping[str, object]) -> str: ...
    def promoted_at(self, model_version: str) -> datetime | None: ...
    def latest_ok_run(self, job_name: str, date_key: str, day: date) -> datetime | None: ...
    def list_daily_metrics(
        self, versions: Sequence[str], first: date, last: date
    ) -> tuple[drop_cells.DailyMetric, ...]: ...
    def list_active_systems(self) -> tuple[str, ...]: ...
    def job_started(self, job_name: str) -> int: ...
    def job_finished(self, run_id: int, status: str, detail: Mapping[str, object]) -> None: ...


@dataclass(frozen=True)
class Options:
    """1 回の指定。"""

    day: date
    trial: bool
    out: Path
    repo: Path
    gate_wait_s: int
    gate_poll_s: int


@dataclass(frozen=True)
class Env:
    """段が使う口と指定。**秘密は `secrets` に持ち、記録する文字列から消すのに使う。**"""

    port: RefitPort
    infer: dry_run.CallsInfer
    clock: Clock
    options: Options
    secrets: tuple[str, ...] = ()


def phase[T](name: str, work: Callable[[], T]) -> T:
    """1 段を回す。**失敗は段の名前で包む**（例外の文言は記録しない。種類だけ）。"""
    try:
        return work()
    except PhaseError:
        raise
    except Exception as cause:
        print(f"{name} で止まりました: {type(cause).__name__}", file=sys.stderr)
        raise PhaseError(name, cause) from cause


# ── B3 ──────────────────────────────────────────────────────
def fit_b3(env: Env) -> refit_record.Fitted:
    """B3 を当てはめる（**`fit_baseline` の CLI そのもの**）。試しの形でなければ置く。"""
    path = env.options.out / "b3.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    arguments = ["--to", env.options.day.isoformat(), "--out", str(path)]
    code = fit_baseline.run(arguments if env.options.trial else [*arguments, "--upload"])
    if code != 0:
        raise RefitError(f"fit_baseline が終了コード {code} で終わりました")
    body = path.read_bytes()
    artifact = baseline_artifact.from_bytes(body)
    if artifact.train_days[-1] != env.options.day.isoformat():
        raise RefitError(f"最終学習日が {artifact.train_days[-1]} です（{env.options.day} のはず）")
    return refit_record.Fitted(artifact=artifact, body=body, path=path)


def verify(env: Env, fitted: refit_record.Fitted) -> str | None:
    """**置いたバイト列が手元と同じか**（#5 の確認①）。試しの形は置いていないので None。"""
    if env.options.trial:
        return None
    stored = env.port.download(
        registry.MODEL_BUCKET, baseline_artifact.artifact_path(fitted.version)
    )
    if stored != fitted.body:
        raise RefitError(f"Storage の {fitted.version} が手元と違います")
    return hashlib.sha256(stored).hexdigest()


def inspect(env: Env, label: str, arguments: Sequence[str]) -> refit_record.Inspected:
    """点検（`scripts/inspect-artifact.py`）。**不合格（終了コード 0 以外）なら止める。**"""
    script = env.options.repo / INSPECT_SCRIPT
    completed = subprocess.run(
        [sys.executable, str(script), *arguments],
        capture_output=True,
        text=True,
        timeout=INSPECT_TIMEOUT_S,
        check=False,
    )
    text = redact((completed.stdout + completed.stderr).strip(), env.secrets)
    print(text, file=sys.stderr)
    if completed.returncode != 0:
        raise RefitError(f"点検が不合格です: {label}（終了コード {completed.returncode}）")
    return refit_record.Inspected(label=label, text=text)


def evaluate(env: Env) -> tuple[register_body.EvalFacts, ...]:
    """検証日を**日曜（最終学習日）と土曜**にした評価（W8-02）。報告書は `out` に書く。"""
    days = (env.options.day, env.options.day - timedelta(days=1))
    return tuple(evaluate_one(env, one) for one in days)


def evaluate_one(env: Env, validation: date) -> register_body.EvalFacts:
    """1 つの検証日。**`evaluate_baselines` と同じ読み方・同じ当てはめ方**で測る。"""
    window = days_between(validation - timedelta(days=EVAL_SPAN_DAYS), validation)
    with open_storage(read_storage_config()) as source:
        prepared = evaluate_baselines.prepare(source, window, None, EVAL_OPTIONS)
    outcome = harness.run(prepared.samples, prepared.split, climate=prepared.climate)
    title = f"B3 の評価（当てはめ直し {env.options.day}、検証 {validation}）"
    text = render_markdown(outcome, title, "", weather=prepared.window.weather)
    (env.options.out / f"eval-{validation}.md").write_text(text, encoding="utf-8")
    return register_body.eval_facts(outcome, validation)


def open_peak_delta_mb(path: Path) -> int:
    """配信と同じ読み手で開いたときの山の上がり幅（MB）。**別のプロセスで測る。**"""
    completed = subprocess.run(
        [sys.executable, "-c", OPEN_PEAK_CODE, str(path)],
        capture_output=True,
        text=True,
        timeout=OPEN_PEAK_TIMEOUT_S,
        check=True,
    )
    # macOS の ru_maxrss はバイト、Linux は KiB
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return math.ceil(int(completed.stdout.strip()) / divisor)


def register_b3(
    env: Env, fitted: refit_record.Fitted, facts: register_body.EvalFacts
) -> dict[str, object]:
    """B3 を candidate で登録する。**本文は試しの形でも書き出す**（`register-b3.json`）。"""
    record_path = f"{REFITS_DIR}/{env.options.day}.md"
    context = register_body.Context(
        card_path=record_path,
        note=b3_note(env.options.day),
        source=f"{record_path}（当てはめ直しの記録。自動）",
        open_peak_delta_mb=open_peak_delta_mb(fitted.path),
    )
    body = register_body.registration(fitted.artifact, fitted.body, facts, context)
    (env.options.out / "register-b3.json").write_text(
        json.dumps(body, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    if not env.options.trial:
        env.port.register_model_version(body)
    return body


def b3_note(day: date) -> str:
    """登録の注記。**どの回の、どの作り方か**（#5 の注記と同じ要素）。"""
    return (
        f"当てはめ直し {day}（自動。W8 の PR G、D-45）。日曜までのデータで月曜の朝に当てはめた "
        "B3 だけの回で、森はそのまま（W8-02）。合成器の B3 で、shadow にも置く（W6-39）。"
        "作り方は #5 と同じ（B2 の下限は平日 3 日・土日祝 4 日、学習の行は 1 日低く、"
        "自分の日は到着日で引く。D-37）。書式 2"
    )


# ── 合成器 ────────────────────────────────────────────────────
def read_serving(env: Env) -> refit_record.Serving:
    """いま配っているもの。**active が合成器でなければ止める**（森の版を引けない）。"""
    active = registry.active(env.port)
    if active.kind != composite.KIND:
        raise RefitError(
            f"active の {active.model_version} は合成器ではありません（{active.kind}）"
        )
    body = env.port.download(registry.MODEL_BUCKET, active.artifact_path)
    if body is None:
        raise registry.MissingArtifactError(f"置かれていません: {active.artifact_path}")
    return refit_record.Serving(
        active=active, shadow=env.port.shadow_model(), composite=composite.from_bytes(body)
    )


def decide_drops(env: Env, serving: refit_record.Serving) -> drop_cells.DropDecision:
    """削るセル（W6-12）。**shadow が B3 でなければ比べず、前に削ったセルだけを引き継ぐ。**"""
    active, shadow = serving.active.model_version, serving.shadow
    promoted = env.port.promoted_at(active)
    days = drop_cells.window_days(None if promoted is None else jst_date(promoted), env.options.day)
    comparable = shadow is not None and shadow.kind == registry.BASELINE_KIND and bool(days)
    b3 = "" if shadow is None else shadow.model_version
    rows = env.port.list_daily_metrics([active, b3], days[0], days[-1]) if comparable else ()
    cells = serving.composite.lightgbm_cells()
    compared = drop_cells.compare_cells(rows, cells, active, b3) if comparable else ()
    return drop_cells.decide(
        compared, serving.composite.dropped, composite=active, b3=b3, days=days
    )


def compose(
    env: Env,
    fitted: refit_record.Fitted,
    serving: refit_record.Serving,
    drops: drop_cells.DropDecision,
) -> refit_record.Composed:
    """合成器を組んで置いて登録する（**`build_composite` の関数そのもの**）。"""
    request = composite_request(env, fitted, serving, drops)
    made = build_composite.make(env.port, request, env.clock.now())
    build_composite.write_outputs(request, made)
    build_composite.publish(env.port, request, made)
    card = expand(request.card, made.version) or ""
    return refit_record.Composed(
        version=made.version,
        size_bytes=len(made.body),
        sha256=composite.sha256_of(made.body),
        description=build_composite.describe(made),
        card_path=card_reference(card) or card,
    )


def composite_request(
    env: Env,
    fitted: refit_record.Fitted,
    serving: refit_record.Serving,
    drops: drop_cells.DropDecision,
) -> build_composite.Request:
    """組み立ての指定。**試しの形は手元の部品で組み、置かず登録しない。**"""
    trial = env.options.trial
    cards = env.options.out if trial else env.options.repo / CARDS_DIR
    return build_composite.Request(
        lightgbm=serving.composite.lightgbm.model_version,
        b3=fitted.version,
        label=None,
        dropped=drops.dropped,
        measure_day=None,
        skip_border=False,
        out=str(env.options.out / "composite.json.gz"),
        card=str(cards / "{version}.md"),
        upload=not trial,
        register=not trial,
        local=trial_parts(env, fitted, serving) if trial else None,
    )


def trial_parts(env: Env, fitted: refit_record.Fitted, serving: refit_record.Serving) -> Path:
    """試しの形の部品置き場。**B3 は手元の版、森・門の表・学習サンプルは Storage から写す。**"""
    local = env.options.out / "parts"
    forest = serving.composite.lightgbm.model_version
    target = local / baseline_artifact.artifact_path(fitted.version)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fitted.path, target)
    copies = (
        (registry.MODEL_BUCKET, lightgbm_artifact.artifact_path(forest)),
        (registry.MODEL_BUCKET, gate_tables.gates_path(forest)),
        (PARQUET_BUCKET, features_path(env.options.day)),
    )
    for bucket, path in copies:
        copy_object(env.port, bucket, path, local / path)
    return local


def copy_object(port: RefitPort, bucket: str, path: str, target: Path) -> None:
    body = port.download(bucket, path)
    if body is None:
        raise build_composite.MissingPartError(f"置かれていません: {bucket}/{path}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)


def inspect_composite(env: Env, composed: refit_record.Composed) -> refit_record.Inspected:
    """合成器の点検。**置いた回は登録簿から、試しの形は手元のファイルを開く。**"""
    if env.options.trial:
        return inspect(
            env, composed.version, ["--file", str(env.options.out / "composite.json.gz")]
        )
    return inspect(env, composed.version, ["--version", composed.version])


# ── 試し打ち ──────────────────────────────────────────────────
def dry_run_version(
    env: Env, serving: refit_record.Serving, composed: refit_record.Composed
) -> str | None:
    """叩く版。**試しの形は今の shadow**（候補は登録していないので本番の口で引けない）。"""
    if not env.options.trial:
        return composed.version
    return None if serving.shadow is None else serving.shadow.model_version


def try_serving(env: Env, version: str | None) -> tuple[dry_run.DryRun, ...]:
    """本番の口で試し打ちする。**1 つでも合格でなければ止める**（全部の結果は記録に出す）。"""
    if version is None:
        return ()
    results = dry_run.run(env.infer, env.clock, version, env.port.list_active_systems())
    for one in results:
        print(
            json.dumps({"system": one.system_id, **one.summary}, ensure_ascii=False),
            file=sys.stderr,
        )
    problems = dry_run.problems_of(results, version)
    if problems:
        raise RefitError("試し打ちが合格しません: " + "・".join(problems))
    return results


# ── 1 回の流れ ────────────────────────────────────────────────
def refit_b3(env: Env) -> refit_record.B3Result:
    """B3 の段。**置いた版が手元と同じで、点検に合格したものだけを評価して登録する。**"""
    fitted = phase("fit_b3", lambda: fit_b3(env))
    verified = phase("verify", lambda: verify(env, fitted))
    arguments = ["--file", str(fitted.path)]
    inspected = phase("inspect_b3", lambda: inspect(env, fitted.version, arguments))
    evaluations = phase("evaluate", lambda: evaluate(env))
    phase("register_b3", lambda: register_b3(env, fitted, evaluations[0]))
    return refit_record.B3Result(fitted, verified, inspected, evaluations)


def refit_composite(env: Env, fitted: refit_record.Fitted) -> refit_record.CompositeResult:
    """合成器の段。**森と門の表は active の合成器のもの**（B3 だけの週。W8-02）。"""
    serving = phase("serving", lambda: read_serving(env))
    drops = phase("drop", lambda: decide_drops(env, serving))
    composed = phase("compose", lambda: compose(env, fitted, serving, drops))
    inspected = phase("inspect_composite", lambda: inspect_composite(env, composed))
    return refit_record.CompositeResult(serving, drops, composed, inspected)


def refit(env: Env, gate: refit_gate.GateResult, started_at: datetime) -> refit_record.RefitReport:
    """門の後の段を順に回す。**どこで止まっても、それより後の段は動かない。**"""
    b3 = refit_b3(env)
    made = refit_composite(env, b3.fitted)
    version = dry_run_version(env, made.serving, made.composed)
    dry_runs = phase("dry_run", lambda: try_serving(env, version))
    return refit_record.RefitReport(
        day=env.options.day,
        trial=env.options.trial,
        started_at=started_at,
        gate=gate,
        b3=b3,
        composite=made,
        dry_run_version=version,
        dry_runs=dry_runs,
    )


@dataclass(frozen=True)
class Written:
    """書いた記録のファイル（**リポジトリからの相対**）。PR に入れるのはこれとモデルカード。"""

    record: str
    promote: str | None


def write_record(env: Env, made: refit_record.RefitReport) -> Written:
    """記録と昇格の SQL を書く。**試しの形は `out` にだけ書く**（リポジトリに入れない）。

    **置いた回も `out` に写しを残す**（記録・昇格の SQL・モデルカード）。ワークフローがログと
    要約に流すので、PR を開く段で落ちても、ログから手で PR を作り直せる。
    """
    template = (env.options.repo / PROMOTE_TEMPLATE).read_text(encoding="utf-8")
    sql = refit_record.fill(template, refit_record.promote_values(made))
    text = refit_record.render(made, sql)
    (env.options.out / "record.md").write_text(text, encoding="utf-8")
    (env.options.out / "promote.sql").write_text(sql, encoding="utf-8")
    if env.options.trial:
        return Written(record=str(env.options.out / "record.md"), promote=None)
    card = env.options.repo / made.composite.composed.card_path
    shutil.copyfile(card, env.options.out / "card.md")
    record = f"{REFITS_DIR}/{made.day}.md"
    promote = f"{REFITS_DIR}/{made.day}-promote.sql"
    (env.options.repo / REFITS_DIR).mkdir(parents=True, exist_ok=True)
    (env.options.repo / record).write_text(text, encoding="utf-8")
    (env.options.repo / promote).write_text(sql, encoding="utf-8")
    return Written(record=record, promote=promote)


def execute(env: Env) -> int:
    """門・段・記録の順に回す。**失敗は段の名前で `job_runs` に残す**（試しの形は残さない）。"""
    try:
        gate = phase("gate", lambda: wait_gate(env))
    except PhaseError as error:
        record_failure(env, None, error)
        return 1
    run_id = None if env.options.trial else recording.started_quietly(env.port, JOB_NAME)
    started_at = env.clock.now()
    try:
        made = refit(env, gate, started_at)
        written = phase("record", lambda: write_record(env, made))
    except PhaseError as error:
        record_failure(env, run_id, error)
        return 1
    finish(env, run_id, made, written)
    return 0


def wait_gate(env: Env) -> refit_gate.GateResult:
    options = env.options
    return refit_gate.wait(
        env.port, env.clock, options.day, wait_s=options.gate_wait_s, poll_s=options.gate_poll_s
    )


def record_failure(env: Env, run_id: int | None, error: PhaseError) -> None:
    """失敗を記録する。**門で止まったときは、いまの時刻で行を作る**。

    見張り（0041 の `check_jobs_failed`）は始まりが 3 時間以内の失敗しか拾わない。
    """
    if env.options.trial:
        return
    started = run_id if run_id is not None else recording.started_quietly(env.port, JOB_NAME)
    detail = {
        "date": env.options.day.isoformat(),
        "phase": error.phase,
        "error": f"{error.phase}: {error.error_name}",
        **_gate_detail(error),
    }
    recording.record_quietly(env.port, started, "failed", detail)


def _gate_detail(error: PhaseError) -> dict[str, object]:
    cause = error.__cause__
    if isinstance(cause, refit_gate.GateTimeoutError):
        return {"waited_s": cause.waited_s, "missing": list(cause.missing)}
    return {}


def finish(env: Env, run_id: int | None, made: refit_record.RefitReport, written: Written) -> None:
    """終わりを記録し、ワークフローが PR を開くための要約（`pr.json`）を書く。"""
    detail = {**refit_record.summary(made), "record": written.record}
    recording.record_quietly(env.port, run_id, "ok", detail)
    (env.options.out / "pr.json").write_text(
        json.dumps(pull_request(made, written), ensure_ascii=False, indent=1) + "\n"
    )
    print(json.dumps(detail, ensure_ascii=False))


def pull_request(made: refit_record.RefitReport, written: Written) -> dict[str, object]:
    """PR の枝・題・入れるファイル（**リポジトリからの相対**）。試しの形は開かない。"""
    composed = made.composite.composed
    promote = [written.promote] if written.promote else []
    return {
        "branch": f"refit/{made.day}",
        "title": f"docs: 当てはめ直し {made.day}（自動。{composed.version}）",
        "files": [written.record, *promote, composed.card_path],
        "body": written.record,
        "trial": made.trial,
    }


# ── 実行 ──────────────────────────────────────────────────────
def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="月曜の朝の当てはめ直し（B3 だけ。昇格はしない）")
    parser.add_argument("--to", dest="day", default=None, help="最終学習日（JST。既定は昨日）")
    parser.add_argument(
        "--trial", action="store_true", help="試しの形：置かず・登録せず・記録しない"
    )
    parser.add_argument("--out", required=True, help="報告書・本文・記録の書き出し先")
    parser.add_argument("--origin", required=True, help="本番の口（試し打ちの宛先）")
    parser.add_argument(
        "--gate-wait-s", type=int, default=refit_gate.WAIT_S, help="門で待つ上限（秒）"
    )
    parser.add_argument(
        "--gate-poll-s", type=int, default=refit_gate.POLL_S, help="門を見る間隔（秒）"
    )
    return parser.parse_args(argv)


def options_of(arguments: argparse.Namespace, now: datetime) -> Options:
    """指定を確かめる。**リポジトリの外では走らせない**（点検とひな形の場所が決まらない）。"""
    repo = repo_root(Path.cwd())
    if repo is None:
        raise RefitError("リポジトリの中で走らせてください（.git が見つかりません）")
    out = Path(arguments.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    return Options(
        day=date.fromisoformat(arguments.day) if arguments.day else jst_yesterday(now),
        trial=arguments.trial,
        out=out,
        repo=repo,
        gate_wait_s=arguments.gate_wait_s,
        gate_poll_s=arguments.gate_poll_s,
    )


def run(argv: Sequence[str] | None = None) -> int:
    arguments = _arguments(argv)
    try:
        options = options_of(arguments, datetime.now(UTC))
    except (RefitError, ValueError) as error:
        print(f"指定が違います: {error}", file=sys.stderr)
        return 2
    config = read_config()
    with ExitStack() as stack:
        port = stack.enter_context(open_storage(read_storage_config()))
        infer = stack.enter_context(open_infer_client(arguments.origin, config.cron_secret))
        secrets = (config.supabase_secret_key, config.cron_secret, config.supabase_url)
        return execute(
            Env(port=port, infer=infer, clock=SystemClock(), options=options, secrets=secrets)
        )


if __name__ == "__main__":
    raise SystemExit(run())
