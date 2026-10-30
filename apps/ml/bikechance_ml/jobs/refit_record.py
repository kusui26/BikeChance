"""当てはめ直しの 1 回の結果と、その記録（W8 の PR G、W8-03）。

**記録は PR の本文になり、昇格の確認の場所になる**（W8-03）。人が読むのは、門・B3・評価・
削るセル・合成器・試し打ちの数と、昇格の手順（`docs/refits/<日>-promote.sql`）。形は
`docs/refits/README.md` に書いてある。

**昇格の SQL はひな形から作る**（`docs/refits/promote.sql.template`）。昇格を呼ぶ文は
人が psql に打ち込む手順で、**ここ（コード）には書かない**——CI が `.github/workflows`・`apps`・
`scripts` に呼ぶ形が現れないことを見張っている（契約 22・39）。

純粋な関数と、結果の型だけを置く。ファイルに書くのは `jobs/weekly_refit.py`。
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Final

from bikechance_ml.baselines.artifact import Artifact
from bikechance_ml.features.grid import JST
from bikechance_ml.jobs import drop_cells
from bikechance_ml.jobs.dry_run import DryRun, peak_rss_mb
from bikechance_ml.jobs.refit_gate import GateResult
from bikechance_ml.jobs.register_body import EvalFacts
from bikechance_ml.models import composite
from bikechance_ml.models.registry import Registered

#: ひな形の差し込み口（`{composite}` など）。**残っていたら止める**（版の名前を書き損じない）。
PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r"\{[a-z_]+\}")


@dataclass(frozen=True)
class Fitted:
    """当てはめた B3 と、そのバイト列。"""

    artifact: Artifact
    body: bytes
    path: Path

    @property
    def version(self) -> str:
        return self.artifact.model_version


@dataclass(frozen=True)
class Inspected:
    """点検（`scripts/inspect-artifact.py`）の 1 回。**合格のときだけ先へ進む。**"""

    label: str
    text: str


@dataclass(frozen=True)
class Serving:
    """いま配っているもの。**合成器は開いて持つ**（森の版・削ったセル・森を歩くセル）。"""

    active: Registered
    shadow: Registered | None
    composite: composite.CompositeArtifact


@dataclass(frozen=True)
class Composed:
    """組んだ合成器（`build_composite.Made` の、記録に要る分）。"""

    version: str
    size_bytes: int
    sha256: str
    description: str
    card_path: str


@dataclass(frozen=True)
class B3Result:
    """B3 の段（当てはめ・確かめ・点検・評価・登録）の結果。"""

    fitted: Fitted
    verified_sha256: str | None
    inspected: Inspected
    #: 検証日を日曜（最終学習日）と土曜にした評価。**先頭が登録に使う日曜**
    evaluations: tuple[EvalFacts, ...]


@dataclass(frozen=True)
class CompositeResult:
    """合成器の段（いまの配信・削るセル・組み立て・点検）の結果。"""

    serving: Serving
    drops: drop_cells.DropDecision
    composed: Composed
    inspected: Inspected


@dataclass(frozen=True)
class RefitReport:
    """1 回の当てはめ直しの全部。**記録・`job_runs`・PR の本文はこれだけを読む。**"""

    day: date
    trial: bool
    started_at: datetime
    gate: GateResult
    b3: B3Result
    composite: CompositeResult
    dry_run_version: str | None
    dry_runs: tuple[DryRun, ...]


def summary(report: RefitReport) -> dict[str, object]:
    """`job_runs.detail` に残す要約（W8-05）。"""
    return {
        "date": report.day.isoformat(),
        "trial": report.trial,
        "waited_s": report.gate.waited_s,
        "b3": report.b3.fitted.version,
        "composite": report.composite.composed.version,
        "forest": report.composite.serving.composite.lightgbm.model_version,
        "dropped": len(report.composite.drops.dropped),
        "newly_dropped": len(report.composite.drops.newly_dropped),
        "dry_run": {
            "version": report.dry_run_version,
            "calls": len(report.dry_runs),
            "peak_rss_mb": peak_rss_mb(report.dry_runs),
        },
    }


def fill(template: str, values: Mapping[str, str]) -> str:
    """ひな形に版の名前を差し込む。**差し込み口が残れば止める。**"""
    text = template
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    left = PLACEHOLDER.findall(text)
    if left:
        raise ValueError(
            f"ひな形に埋まっていない差し込み口があります: {', '.join(sorted(set(left)))}"
        )
    return text


def promote_values(report: RefitReport) -> dict[str, str]:
    """昇格の SQL に差し込む値。前の active・shadow は**表示だけ**に使う。

    shadow に上げると、前の shadow は自動で retired になる（0038）ので、下ろす行は要らない。
    """
    shadow = report.composite.serving.shadow
    return {
        "day": report.day.isoformat(),
        "composite": report.composite.composed.version,
        "b3": report.b3.fitted.version,
        "previous_active": report.composite.serving.active.model_version,
        "previous_shadow": "無し" if shadow is None else shadow.model_version,
    }


def render(report: RefitReport, promote_sql: str) -> str:
    """記録の Markdown（`docs/refits/README.md` の形）。"""
    sections = [
        _header(report),
        _gate_block(report.gate),
        _b3_block(report.b3),
        _evaluation_block(report.b3.evaluations),
        _drop_block(report.composite.drops),
        _composite_block(report.composite),
        _dry_run_block(report),
        _promote_block(report, promote_sql),
    ]
    return "\n\n".join(sections) + "\n"


def _jst(at: datetime) -> str:
    return at.astimezone(JST).strftime("%Y-%m-%d %H:%M")


def _header(report: RefitReport) -> str:
    mode = "**試しの形**（置かず・登録せず・記録の PR も開かない）" if report.trial else "定時の回"
    return "\n".join(
        [
            f"# 当てはめ直し {report.day}（自動。W8 の PR G、D-45）",
            "",
            f"- {mode}。始まり {_jst(report.started_at)} JST、"
            f"門で {report.gate.waited_s // 60} 分待った",
            f"- B3 `{report.b3.fitted.version}` → 合成器 `{report.composite.composed.version}`"
            f"（森は `{report.composite.serving.composite.lightgbm.model_version}` のまま。W8-02）",
            "- **昇格はまだ**：この記録を読み、確認を取ってから §7 の SQL を"
            "推論の合間に流す（W8-04）",
        ]
    )


def _gate_block(gate: GateResult) -> str:
    rows = ["## 1. 前提の門（W8-01）", "", "| ジョブ | ok の始まり（JST） |", "|---|---|"]
    rows += [f"| {name} | {_jst(at)} |" for name, at in sorted(gate.arrived.items())]
    return "\n".join(rows)


def _b3_block(result: B3Result) -> str:
    fitted = result.fitted
    days = fitted.artifact.train_days
    verified = (
        "置かない（試しの形）"
        if result.verified_sha256 is None
        else f"Storage と手元はバイトまで同じ（SHA-256 `{result.verified_sha256[:16]}…`）"
    )
    return "\n".join(
        [
            "## 2. B3",
            "",
            f"- `{fitted.version}`：学習 {days[0]}〜{days[-1]}（{len(days)} 日）、"
            f"ポート {len(fitted.artifact.ports):,}、{len(fitted.body):,} バイト。{verified}",
            "",
            "```",
            result.inspected.text,
            "```",
        ]
    )


def _evaluation_block(evaluations: Sequence[EvalFacts]) -> str:
    rows = [
        "## 3. 評価（W8-02。検証は最終学習日とその前日——月曜の回なら日曜と土曜）",
        "",
        "| 検証日 | 分割 | 検証の行 | B3 が B1 以下の組 | 最悪 |",
        "|---|---|---:|---:|---|",
    ]
    for one in evaluations:
        worst = "—" if one.worst is None else one.worst.describe()
        behind = f"{len(one.behind)} / {one.pairs}"
        rows.append(f"| {one.validation} | {one.split} | {one.n_eval:,} | {behind} | {worst} |")
    return "\n".join([*rows, "", *_brier_rows(evaluations[0])])


def _brier_rows(facts: EvalFacts) -> list[str]:
    rows = [
        f"重み付き Brier（検証 {facts.validation}）：",
        "",
        "| 系統/ターゲット | B1 | B3 |",
        "|---|---:|---:|",
    ]
    return rows + [
        f"| {key} | {facts.brier_b1[key]:.5f} | {facts.brier_b3[key]:.5f} |"
        for key in sorted(facts.brier_b3)
    ]


def _drop_block(drops: drop_cells.DropDecision) -> str:
    worse = [one for one in drops.compared if one.worse]
    improvement = drop_cells.overall_improvement(drops.compared)
    span = f"{drops.days[0]}〜{drops.days[-1]}（{len(drops.days)} 日）" if drops.days else "無し"
    smallest = min(drops.compared, key=lambda one: one.improvement, default=None)
    lines = [
        "## 4. 削るセル（W6-12）",
        "",
        f"- 比べた日 {span}：合成器 `{drops.composite}` 対 shadow の B3 `{drops.b3 or '無し'}`",
        f"- 森のセル {len(drops.compared)}、森が悪いセル {len(worse)}、"
        f"森の改善 {'—' if improvement is None else f'{improvement:.2%}'}",
    ]
    if smallest is not None:
        cell = " ".join(map(str, smallest.cell))
        lines.append(f"- いちばん差の小さいセル {cell} の {smallest.improvement:.2%}")
    dropped = drop_cells.drop_lines(drops.dropped)
    lines.append(
        "- 削るセル："
        + ("、".join(dropped) if dropped else "無し")
        + f"（前から削っていた {len(drops.carried)}）"
    )
    return "\n".join(lines)


def _composite_block(result: CompositeResult) -> str:
    composed = result.composed
    return "\n".join(
        [
            "## 5. 合成器",
            "",
            f"- `{composed.version}`：{composed.size_bytes:,} バイト、"
            f"SHA-256 `{composed.sha256[:16]}…`、モデルカード `{composed.card_path}`",
            "",
            "```",
            composed.description,
            "",
            result.inspected.text,
            "```",
        ]
    )


def _dry_run_block(report: RefitReport) -> str:
    rows = [
        "## 6. 試し打ち（本番の口。何も書かない）",
        "",
        f"版 `{report.dry_run_version or '—'}`"
        + ("（試しの形なので、いまの shadow で叩いた）" if report.trial else ""),
        "",
        "| 系統 | 回 | 始まり（JST） | 所要 ms | CPU ms | 読み込み ms | rss MB | 予測 "
        "| 無いポート |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    return "\n".join(rows + [_dry_run_row(one) for one in report.dry_runs])


def _dry_run_row(one: DryRun) -> str:
    values = [
        one.summary.get(key, "—")
        for key in (
            "duration_ms",
            "cpu_ms",
            "model_load_ms",
            "rss_mb",
            "predicted",
            "unknown_ports",
        )
    ]
    cells = " | ".join(str(value) for value in values)
    started = one.started_at.astimezone(JST)
    return f"| {one.system_id} | {one.attempt} | {started:%H:%M:%S} | {cells} |"


def _promote_block(report: RefitReport, promote_sql: str) -> str:
    if report.trial:
        return "## 7. 昇格の手順\n\n試しの形なので、昇格するものは無い。"
    return "\n".join(
        [
            "## 7. 昇格の手順（W8-04。確認を取ってから）",
            "",
            f"`docs/refits/{report.day}-promote.sql` を推論の合間（毎 5 分の 2〜3 分）に流し、"
            "2 周期と鮮度の見張りを見てから、この PR をマージする。"
            f"戻すなら `{report.composite.serving.active.model_version}` を active に"
            "（確認を取ってから）。",
            "",
            "```sql",
            promote_sql.rstrip(),
            "```",
        ]
    )
