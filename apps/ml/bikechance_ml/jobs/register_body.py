"""B3 を candidate で登録する本文と、評価の要点（W8 の PR G、W8-02）。

#4・#5 では手元の道具（`b3_tools.py register`）が、点検と評価の **Markdown を正規表現で読み直して**
本文を作っていた。表の書き方を変えると、登録の中身が黙って欠ける。**ここでは成果物と評価の結果
（`harness.Outcome`）から直接作る。**

**形は #5 の登録（`baseline-b3-v0-20261025`）と同じ**にする。登録簿を読む側（点検の SQL・
日次評価の比べ）が版ごとに読み方を変えずに済む。

純粋な関数だけを置く。成果物を開いたときのメモリの山（`open_peak_delta_mb`）は、別の
プロセスで測るので `jobs/weekly_refit.py` が渡す。
"""

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

from bikechance_ml.baselines import climatology
from bikechance_ml.baselines.artifact import Artifact, artifact_path
from bikechance_ml.eval.harness import Outcome, SliceScores
from bikechance_ml.features.grid import profile_path
from bikechance_ml.features.profile import PROFILE_NAME

#: 自分の日をどう引くか（D-37）。**到着日で引く**——#4 から変わっていない作り方。
OWN_DAY: Final[str] = "arrival"

#: 曜日種別ごとの使えるセルを数えるターゲット（#4・#5 の登録と同じく貸出）。
#: **2 つのターゲットのセルは同じ下限で決まる**ので、数は揃う（#5：どちらも 6,186,675）。
BY_DOW_TARGET: Final[str] = "bike"

#: 登録と記録に出す Brier の桁（評価の報告書の表と同じ）。
BRIER_DIGITS: Final[int] = 5


@dataclass(frozen=True)
class Behind:
    """短い水平で B3 が B1 より悪い 1 組（評価の報告書の「B3 が B1 以下」と同じ `>`）。"""

    system: str
    target: str
    horizon: int
    excess: float

    def describe(self) -> str:
        """**B1 の Brier が 0 なら比は書けない**（B1 が完全に当てた切り口。小さな表で起きる）。"""
        excess = "B1 の Brier が 0" if math.isinf(self.excess) else f"{self.excess:+.2%}"
        return f"{self.system} の {self.target} の {self.horizon} 分（{excess}）"


@dataclass(frozen=True)
class EvalFacts:
    """1 つの検証日の評価の要点。**登録の本文と記録の両方がこれを読む。**"""

    validation: date
    split: str
    n_eval: int
    brier_b3: Mapping[str, float]
    brier_b1: Mapping[str, float]
    behind: tuple[Behind, ...]
    pairs: int

    @property
    def worst(self) -> Behind | None:
        return max(self.behind, key=lambda one: one.excess, default=None)


def eval_facts(outcome: Outcome, validation: date) -> EvalFacts:
    """評価の結果から要点を取る。**全体は system × ターゲット、組は水平ごと。**"""
    return EvalFacts(
        validation=validation,
        split=outcome.split.describe(),
        n_eval=outcome.n_eval,
        brier_b3=_briers(outcome.overall, "B3"),
        brier_b1=_briers(outcome.overall, "B1"),
        behind=tuple(_behind(one) for one in outcome.by_horizon if _is_behind(one)),
        pairs=len(outcome.by_horizon),
    )


def _briers(parts: Sequence[SliceScores], model: str) -> dict[str, float]:
    return {
        f"{one.slice.system}/{one.slice.target}": round(one.weighted[model].brier, BRIER_DIGITS)
        for one in parts
    }


def _is_behind(one: SliceScores) -> bool:
    return one.weighted["B3"].brier > one.weighted["B1"].brier


def _behind(one: SliceScores) -> Behind:
    b3, b1 = one.weighted["B3"].brier, one.weighted["B1"].brier
    return Behind(
        system=one.slice.system,
        target=one.slice.target,
        horizon=int(one.slice.horizon or 0),
        excess=b3 / b1 - 1.0 if b1 > 0 else math.inf,
    )


def caveat(facts: EvalFacts, served_days: Sequence[str]) -> str:
    """登録の但し書き。**報告書の版と配る版が違う**ことと、短い水平の組を書く。"""
    worst = facts.worst
    tail = "" if worst is None else f"（最悪 {worst.describe()}）"
    return (
        f"報告書は同じ形で別の 7 日に当てはめた版を {facts.validation} で測ったもので"
        f"（{facts.split}）、配るのは {served_days[0]}〜{served_days[-1]} の版である"
        f"（fit_baseline は期間を分けない）。短い水平で B3 が B1 以下なのは "
        f"{facts.pairs} 件中 {len(facts.behind)} 件{tail}"
    )


def b2_record(artifact: Artifact) -> dict[str, object]:
    """B2 の作り方と使えるセル（#5 の `metrics.b2` と同じ形）。"""
    model = artifact.targets[BY_DOW_TARGET]
    return {
        "source": profile_path(date.fromisoformat(artifact.train_days[-1]), PROFILE_NAME),
        "serve_days": dict(model.b2.floor.serve),
        "fit_offset_days": model.b2.floor.fit_offset,
        "own_day": OWN_DAY,
        "max_days": None if model.b2.max_days is None else dict(model.b2.max_days),
        "usable_cells": {
            name: int(one.b2.usable.sum()) for name, one in sorted(artifact.targets.items())
        },
        "usable_cells_by_dow": climatology.usable_by_dow(model.b2.usable),
    }


def artifact_record(artifact: Artifact, body: bytes, open_peak_delta_mb: int) -> dict[str, object]:
    """成果物そのものの素性（#5 の `metrics.artifact` と同じ形）。"""
    return {
        "format_version": artifact.format_version,
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "ports": len(artifact.ports),
        "blend": {name: one.b3.describe() for name, one in sorted(artifact.targets.items())},
        "open_peak_delta_mb": open_peak_delta_mb,
    }


@dataclass(frozen=True)
class Context:
    """本文に添える文（どの回の記録か・何を書き添えるか）。"""

    card_path: str
    note: str
    source: str
    open_peak_delta_mb: int


def registration(
    artifact: Artifact, body: bytes, facts: EvalFacts, context: Context
) -> dict[str, object]:
    """`register_model_version` に渡す本文。**`candidate` としてしか登録できない**（0038）。"""
    return {
        "model_version": artifact.model_version,
        "kind": "baseline",
        "status": "candidate",
        "feature_set": artifact.feature_set,
        "artifact_path": artifact_path(artifact.model_version),
        "train_days": list(artifact.train_days),
        "card_path": context.card_path,
        "note": context.note,
        "metrics": {
            "source": context.source,
            "b2": b2_record(artifact),
            "artifact": artifact_record(artifact, body, context.open_peak_delta_mb),
            "split": facts.split,
            "n_eval": facts.n_eval,
            "brier_weighted": dict(facts.brier_b3),
            "brier_weighted_b1": dict(facts.brier_b1),
            "caveat": caveat(facts, artifact.train_days),
        },
    }
