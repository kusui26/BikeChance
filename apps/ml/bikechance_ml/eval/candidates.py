"""当てはめた候補を並べて比べる（W6 の PR F、W6-13・W6-15・W6-16、D-32 の (c)、契約 37）。

**本数と学習率は組で決める**（契約 37）。学習率を上げずに本数を減らすと崩れる——予備の
当てはめで、100 本・学習率 0.05 は ECE が 0.0311 で校正の基準（0.03）を割った（M4 の文書
§1.4 ①）。だから候補は `本数:学習率` の組で受け、**同じ検証の行の上で**並べる。

**比べるのは「配る形」**（`eval/gates.py`）。LightGBM 単体の Brier ではない。

**D-32 の (c)**：本数の上限（費用の式から出す）と 300 本を一緒に当てはめ、**配る形の改善の差が
1 ポイント以内なら上限の本数で配る**。超えたら利用者に諮る（上限を上げるか、`iad1` に移すか）。
比べるのは「1 日目で選び、残りの日で測った」値——同じ日で選んで測ると良く見える（W6-14）。

**ここは純粋である。** 当てはめはしない（`jobs/fit_lightgbm.py`）。
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

import numpy as np

from bikechance_ml.eval import gates, metrics
from bikechance_ml.eval.dataset import BUCKET_LABELS, TARGETS, Samples, Target, bucket_index
from bikechance_ml.eval.harness import Outcome, SliceScores
from bikechance_ml.features.arrays import Bools, Float64

#: 表に出す名前の頭。`harness` の B0〜B3 と並ぶ。
MODEL_PREFIX: Final[str] = "LGBM"

#: D-32 の (c)：比べる相手の本数と、許す差（配る形の改善のポイント）。
REFERENCE_TREES: Final[int] = 300
BUDGET_TOLERANCE_PP: Final[float] = 1.0

#: 校正の基準（開発プラン §7.1）。**v1 は校正器を入れず、これを満たせば配る**（W6-15）。
MAX_ECE: Final[float] = 0.03

#: バケツ別の ECE を出すバケツ（開発プラン §7.1：**バケツ 0・1 の ECE も並べて判定する**）。
ECE_BUCKETS: Final[tuple[str, ...]] = ("0", "1")

#: ⑧ 報告書に出す特徴量の重要度の数（gain の上位。W6-29 で v5 を足すかの材料になる）。
IMPORTANCE_TOP: Final[int] = 20

#: 候補の書き方（`本数:学習率`）。
_CANDIDATE: Final[re.Pattern[str]] = re.compile(r"\A(\d+):(\d*\.?\d+)\Z")


class CandidateError(ValueError):
    """候補の書き方が違う、または同じ組が 2 度ある。**当てはめる前に止める。**"""


@dataclass(frozen=True, order=True)
class Candidate:
    """木の本数と学習率の組（契約 37）。"""

    trees: int
    learning_rate: float

    @property
    def name(self) -> str:
        """表に出す名前（`LGBM 100×0.15`）。**`harness` のモデル名にもなる。**"""
        return f"{MODEL_PREFIX} {self.trees}×{self.learning_rate:g}"

    @property
    def spec(self) -> str:
        """書き方（`100:0.15`）。`--choose` にはこれを渡す。"""
        return f"{self.trees}:{self.learning_rate:g}"


def parse_candidates(text: str) -> tuple[Candidate, ...]:
    """`本数:学習率` をカンマで並べたものを読む（例 `50:0.15,100:0.15,300:0.05`）。"""
    found = tuple(parse_one(one.strip()) for one in text.split(",") if one.strip())
    if not found:
        raise CandidateError("候補が 1 つもありません")
    if len(set(found)) != len(found):
        raise CandidateError(f"同じ候補が 2 度あります: {text}")
    return found


def parse_one(text: str) -> Candidate:
    """1 つの候補。**本数は 1 以上、学習率は 0 より大きく 1 以下。**"""
    matched = _CANDIDATE.match(text)
    if matched is None:
        raise CandidateError(f"候補は `本数:学習率` で書きます: {text!r}")
    trees, rate = int(matched.group(1)), float(matched.group(2))
    if trees < 1 or not 0.0 < rate <= 1.0:
        raise CandidateError(f"本数は 1 以上、学習率は 0 より大きく 1 以下です: {text!r}")
    return Candidate(trees=trees, learning_rate=rate)


# ── 1 つの候補のまとめ ──────────────────────────────────────────
@dataclass(frozen=True)
class EceSummary:
    """⑤ **LightGBM そのもの**の ECE（等頻度 15、重み付き）。いちばん悪い所と、その場所。"""

    horizon: float
    horizon_where: str
    #: バケツ（`0`・`1`）→ system × ターゲットのうち最大
    bucket: Mapping[str, float]
    dow_type: float
    dow_type_where: str

    @property
    def passes(self) -> bool:
        """**すべて 0.03 未満**なら配れる（W6-15。v1 は校正器を入れない）。

        **Python の `bool` で返す。** ECE は numpy の数なので、比べると `np.bool_` になり
        うる——それは JSON にできず、**森を置いた後の登録で落ちる**（`model_versions.metrics`）。
        """
        return bool(max(self.horizon, self.dow_type, *self.bucket.values()) < MAX_ECE)


@dataclass(frozen=True)
class GroupDecision:
    """⑥ system × ターゲットの 1 組の意思決定の指標。**いまの B3 と、配る形を並べる。**"""

    system: str
    target: str
    reference: metrics.Decision
    served: metrics.Decision


@dataclass(frozen=True)
class Summary:
    """1 つの候補を、同じ検証の行の上で測った結果（報告書の ①〜⑥）。"""

    candidate: Candidate
    judged: gates.Judged
    ece: EceSummary
    decisions: tuple[GroupDecision, ...]


def summarize(
    candidate: Candidate, outcome: Outcome, evaluated: Samples, days: Sequence[date]
) -> Summary:
    """候補 1 つをまとめる。`evaluated` は**検証の行**（`outcome` の確率と同じ並び）。"""
    probability = probabilities_of(outcome)
    return Summary(
        candidate=candidate,
        judged=gates.judge(evaluated, probability, candidate.name, days),
        ece=ece_summary(outcome, evaluated, probability, candidate.name),
        decisions=decision_groups(evaluated, probability, candidate.name),
    )


def probabilities_of(outcome: Outcome) -> gates.Probabilities:
    """`harness` の結果から、ターゲット → モデル → 検証の行の確率を取り出す。"""
    return {fitted.target.name: fitted.probability for fitted in outcome.fits}


def ece_summary(
    outcome: Outcome, evaluated: Samples, probability: gates.Probabilities, model: str
) -> EceSummary:
    """⑤ 水平別・バケツ 0 と 1・曜日種別別の、いちばん悪い ECE（開発プラン §7.1）。"""
    horizon, horizon_where = _worst(outcome.by_horizon, model)
    dow, dow_where = _worst(outcome.by_dow_type, model)
    return EceSummary(
        horizon=horizon,
        horizon_where=horizon_where,
        bucket={label: _bucket_ece(evaluated, probability, model, label) for label in ECE_BUCKETS},
        dow_type=dow,
        dow_type_where=dow_where,
    )


def _worst(scores: Sequence[SliceScores], model: str) -> tuple[float, str]:
    """切り口のうち ECE がいちばん大きいものと、その名前。**切り口が無ければ 0。**"""
    if not scores:
        return 0.0, "—"
    worst = max(scores, key=lambda one: one.weighted[model].ece)
    return worst.weighted[model].ece, _where(worst)


def _where(one: SliceScores) -> str:
    part = one.slice
    extra = [str(part.horizon)] if part.horizon is not None else [part.cut or ""]
    return " / ".join([part.system, part.target, *extra])


def _bucket_ece(
    evaluated: Samples, probability: gates.Probabilities, model: str, label: str
) -> float:
    """そのバケツの行（水平はまとめる）の ECE。**system × ターゲットのうち最大。**"""
    index = BUCKET_LABELS.index(label)
    values = (
        _group_ece(
            evaluated,
            probability[target.name][model],
            target,
            _bucket_rows(evaluated, target, index, system),
        )
        for target in TARGETS
        for system in range(len(evaluated.systems))
    )
    return max((one for one in values if one is not None), default=0.0)


def _bucket_rows(evaluated: Samples, target: Target, index: int, system: int) -> Bools:
    in_bucket = bucket_index(evaluated.count_of(target)) == index
    return np.asarray(in_bucket & (evaluated.system == system), dtype=np.bool_)


def _group_ece(evaluated: Samples, p: Float64, target: Target, inside: Bools) -> float | None:
    """その行の ECE。**行が無ければ None**（0 と区別する）。"""
    if not bool(inside.any()):
        return None
    return metrics.ece(evaluated.y(target)[inside], p[inside], evaluated.weight[inside])


def decision_groups(
    evaluated: Samples, probability: gates.Probabilities, model: str
) -> tuple[GroupDecision, ...]:
    """⑥ system × ターゲットごとに、**いまの B3 と、セル単位の門で配る形**の意思決定の指標。"""
    every = np.ones(len(evaluated), dtype=np.bool_)
    walks = gates.routed(evaluated, probability, model, gates.CELL_GATE, every)
    mixed = gates.served_probability(probability, model, walks)
    return tuple(
        _group_decision(evaluated, probability[target.name][gates.REFERENCE], mixed, target, system)
        for target in TARGETS
        for system in range(len(evaluated.systems))
        if bool((evaluated.system == system).any())
    )


def _group_decision(
    evaluated: Samples,
    reference: Float64,
    mixed: Mapping[str, Float64],
    target: Target,
    system: int,
) -> GroupDecision:
    inside = np.asarray(evaluated.system == system, dtype=np.bool_)
    y, weight = evaluated.y(target)[inside], evaluated.weight[inside]
    return GroupDecision(
        system=evaluated.systems[system],
        target=target.name,
        reference=metrics.decision(y, reference[inside], weight),
        served=metrics.decision(y, mixed[target.name][inside], weight),
    )


# ── 森の素性（⑦・⑧）───────────────────────────────────────────
@dataclass(frozen=True)
class Importance:
    """⑧ 1 つの列の重要度（gain）。**割合は、その森の gain の総和に対して。**"""

    column: str
    gain: float
    share: float


@dataclass(frozen=True)
class ForestFacts:
    """⑦・⑧ 当てはめた森の素性。**当てはめた側が測って渡す**（ここは測らない）。"""

    #: ターゲット → 木の本数・節の数・いちばん深い木の深さ
    trees: Mapping[str, int]
    nodes: Mapping[str, int]
    depth: Mapping[str, int]
    #: system → **当てはめた機械で** 1 行 1 木を歩いた秒（2 ターゲットの森をまとめて）
    row_tree_seconds: Mapping[str, float]
    #: ターゲット → gain の上位
    importance: Mapping[str, tuple[Importance, ...]]


def top_importance(
    columns: Sequence[str], gains: Float64, top: int = IMPORTANCE_TOP
) -> tuple[Importance, ...]:
    """gain の大きい順に `top` 列。**同じ gain なら列の並び順**（毎回同じ表になる）。

    **gain が 0 の列は出さない**（一度も分岐に使われていない）。
    """
    total = float(gains.sum())
    order = np.argsort(-gains, kind="stable")[:top]
    return tuple(
        Importance(columns[index], float(gains[index]), float(gains[index]) / total)
        for index in order
        if gains[index] > 0.0
    )


# ── D-32 の (c) ────────────────────────────────────────────────
@dataclass(frozen=True)
class Budget:
    """⑩ 1 つの候補の判定。**300 本の配る形の改善との差**（ポイント）。"""

    candidate: Candidate
    #: 300 本の改善 − この候補の改善（ポイント）。**300 本の候補が無ければ None**
    gap_pp: float | None
    #: 差が 1 ポイント以内か。300 本が無ければ None
    within: bool | None


def budget_basis(summary: Summary) -> gates.Served:
    """比べる値：**1 日目で選び、残りの日で測った**セル単位の配る形（無ければ検証日ぜんぶ）。"""
    held = summary.judged.holdout.get(gates.CELL_GATE)
    return held if held is not None else summary.judged.served[gates.CELL_GATE]


def tree_budget(summaries: Sequence[Summary]) -> tuple[Budget, ...]:
    """⑩ 各候補を 300 本と比べる（D-32 の (c)）。**300 本が 2 つ以上あれば学習率の低いほう。**"""
    reference = sorted(
        (one for one in summaries if one.candidate.trees == REFERENCE_TREES),
        key=lambda one: one.candidate.learning_rate,
    )
    if not reference:
        return tuple(Budget(one.candidate, None, None) for one in summaries)
    base = budget_basis(reference[0]).improvement
    return tuple(_budget(one, base) for one in summaries)


def _budget(summary: Summary, base: float) -> Budget:
    gap = (base - budget_basis(summary).improvement) * 100.0
    return Budget(summary.candidate, gap, gap <= BUDGET_TOLERANCE_PP)
