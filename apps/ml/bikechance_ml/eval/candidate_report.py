"""候補を並べた報告書の §12（W6 の PR F、W6 プラン §6.6 の ①〜⑩）。

**`report.render_markdown`（§1〜§11）の後ろに足す。** §3〜§11 の LightGBM の列は**門を
当てる前の単体**の成績で、v1 が配るのは門を当てた形（合成器。契約 33・35）である。
ここはその形で候補を比べる。

**候補はすべて同じ検証の行の上で測る**（`harness.run` に同じ行を配っている）。

**ここは純粋である。** 数字は `eval/candidates.py` の `Summary` と、当てはめた側が測った
`ForestFacts` から取る。
"""

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Final

from bikechance_ml.eval import candidates, gates, metrics, report
from bikechance_ml.eval.candidates import Candidate, ForestFacts, Summary
from bikechance_ml.eval.dataset import BUCKET_LABELS, TARGETS
from bikechance_ml.eval.split import DaySplit
from bikechance_ml.features.constants import HORIZONS_MIN
from bikechance_ml.features.fingerprint import Fingerprint

#: 1 秒あたりのマイクロ秒（1 行 1 木の秒を µs で出す）。
MICROSECONDS: Final[float] = 1e6

#: 表に出す系統の名前。**並びは費用の係数の並び**（`eval/gates.py`）。
SYSTEM_LABELS: Final[Mapping[str, str]] = {"hellocycling": "HELLO", "docomo-cycle": "ドコモ"}

_CELL: Final[str] = gates.CELL_GATE
_HORIZON: Final[str] = gates.HORIZON_GATE


def render(
    summaries: Sequence[Summary],
    facts: Mapping[Candidate, ForestFacts],
    chosen: Candidate | None,
    prints: Mapping[date, Fingerprint],
    split: DaySplit,
) -> list[str]:
    """§12 の行。**候補の並びは渡された順**（`--candidates` に書いた順）。"""
    measured = [
        *_gate_counts(summaries),
        *_gate_tables(summaries, chosen),
        *_served(summaries),
        *_calibration(summaries),
        *_decisions(summaries),
    ]
    grown = [*_forests(summaries, facts), *_importance(summaries, facts)]
    return [*_intro(chosen), *measured, *grown, *_data(prints, split), *_budget(summaries)]


def _intro(chosen: Candidate | None) -> list[str]:
    picked = (
        f"**{chosen.name}**（`--choose {chosen.spec}`）"
        if chosen
        else "**選んでいない**（置く・登録する・カードと門の表を書くのは、選んだ候補だけ）"
    )
    return [
        "## 12. 候補の比較（W6 の PR F、①〜⑩）",
        "",
        "**v1 は LightGBM 単体では配らない**（契約 33）。セル（system × ターゲット × 水平 × "
        "台数バケツ）ごとに、LightGBM が門の相手（**本番と同じ作り方の B3**。W6-10、契約 36）を"
        " 10% 以上改善したところだけ LightGBM、他は B3 を配る（開発プラン §7.1、契約 35）。"
        "**§3〜§11 の LightGBM の列は門を当てる前の単体**で、配る形の成績はここにある。",
        "",
        "**候補はすべて同じ検証の行の上で測った。** 本数と学習率は組で決める（契約 37）。",
        "",
        f"- **選んだ候補**：{picked}",
        "",
    ]


# ── ① 門を越えた数 ────────────────────────────────────────────
def _gate_counts(summaries: Sequence[Summary]) -> list[str]:
    return [
        "### 12.1 ① 門を越えた数",
        "",
        "**判定の対象**は B0 の Brier が 0.001 以上の切り口（W3-16）。**越えた**のは門の相手"
        "（B3）を 10% 以上改善したもの。全部は検証日に行があった切り口の数。",
        "",
        _row("候補", "セル：越えた", "判定の対象", "全部", "水平：越えた", "判定の対象", "全部"),
        "|---|---:|---:|---:|---:|---:|---:|",
        *[_count_row(one) for one in summaries],
        "",
    ]


def _count_row(one: Summary) -> str:
    judged = one.judged
    counts = (
        judged.passing(_CELL),
        judged.judgeable(_CELL),
        judged.cells,
        judged.passing(_HORIZON),
        judged.judgeable(_HORIZON),
        judged.horizons,
    )
    return _row(one.candidate.name, *[str(len(part)) for part in counts])


# ── ② 門の表 ─────────────────────────────────────────────────
def _gate_tables(summaries: Sequence[Summary], chosen: Candidate | None) -> list[str]:
    return [
        "### 12.2 ② 門の表（セル単位、検証日ぜんぶで決めたもの）",
        "",
        "値は B3 に対する相対改善。**太字が LightGBM を配るセル**、— は判定の対象外、空欄は"
        "検証日に行が無いセル（どちらも B3 を配る）。**選んだ候補の表は JSON にも書く**"
        "（置く回は `lightgbm/<版>.gates.json.gz`）。",
        "",
        *[line for one in summaries for line in _gate_table(one, opened=one.candidate == chosen)],
    ]


def _gate_table(one: Summary, *, opened: bool) -> list[str]:
    cells = {(cell.system, cell.target, cell.h_min, cell.bucket): cell for cell in one.judged.cells}
    groups = sorted({(cell.system, cell.target) for cell in one.judged.cells})
    return [
        f"<details{' open' if opened else ''}><summary>{one.candidate.name}</summary>",
        "",
        *[line for system, target in groups for line in _matrix(cells, system, target)],
        "</details>",
        "",
    ]


def _matrix(
    cells: Mapping[tuple[str, str, int, str | None], gates.Scored], system: str, target: str
) -> list[str]:
    return [
        f"**{system} / {target}**",
        "",
        _row("水平", *BUCKET_LABELS),
        "|---:|" + "---:|" * len(BUCKET_LABELS),
        *[
            _row(
                f"{h_min} 分",
                *[_mark(cells.get((system, target, h_min, label))) for label in BUCKET_LABELS],
            )
            for h_min in HORIZONS_MIN
        ],
        "",
    ]


def _mark(one: gates.Scored | None) -> str:
    if one is None:
        return ""
    if one.reason == gates.EXCLUDED:
        return "—"
    text = _percent(one.improvement)
    return f"**{text}**" if one.reason == gates.PASSED else text


# ── ③ 配る形の改善 ・ ④ 森を歩く行 ─────────────────────────────
def _served(summaries: Sequence[Summary]) -> list[str]:
    return [
        "### 12.3 ③ 配る形の改善 ・ ④ 森を歩く行",
        "",
        "**改善**は配る形の Brier を B3 と比べた相対値で、**system × ターゲットの 4 組の平均**"
        "（M4 の文書 §1.4 と同じ物差し）。**同じ日で選んで測ると良く見える**ので、「1 日目で"
        "選び、残りの日で測る」を並べる（W6-14）。**森を歩く行**は費用の重み付き（抽出の重み × "
        "系統の 1 行 1 木の秒）で、森の費用はこれに比例する。",
        "",
        *_served_header(),
        *[_served_row(one, unit) for one in summaries for unit in gates.UNITS],
        "",
    ]


def _served_header() -> list[str]:
    return [
        _row(
            "候補",
            "門の単位",
            "改善（検証日ぜんぶ）",
            "改善（1 日目で選び、残りで測る）",
            "森を歩く行（ぜんぶ）",
            "森を歩く行（残りの日）",
        ),
        "|---|---|---:|---:|---:|---:|",
    ]


def _served_row(one: Summary, unit: str) -> str:
    whole = one.judged.served[unit]
    held = one.judged.holdout.get(unit)
    return _row(
        one.candidate.name,
        gates.UNIT_LABELS[unit],
        _percent(whole.improvement),
        "—" if held is None else _percent(held.improvement),
        f"{whole.forest_share:.1%}",
        "—" if held is None else f"{held.forest_share:.1%}",
    )


# ── ⑤ 校正 ───────────────────────────────────────────────────
def _calibration(summaries: Sequence[Summary]) -> list[str]:
    return [
        "### 12.4 ⑤ 校正（LightGBM そのものの ECE。等頻度 15、重み付き）",
        "",
        f"**v1 は校正器を入れず、すべて {candidates.MAX_ECE} 未満なら配る**（W6-15）。"
        "バケツ 0・1 は水平をまとめ、system × ターゲットのうち最大（開発プラン §7.1）。",
        "",
        *_ece_header(),
        *[_ece_row(one) for one in summaries],
        "",
    ]


def _ece_header() -> list[str]:
    buckets = [f"バケツ {label}" for label in candidates.ECE_BUCKETS]
    return [
        _row("候補", "水平別の最大", "その場所", *buckets, "曜日種別の最大", "その場所", "基準"),
        "|---|---:|---|" + "---:|" * len(buckets) + "---:|---|---|",
    ]


def _ece_row(one: Summary) -> str:
    ece = one.ece
    return _row(
        one.candidate.name,
        f"{ece.horizon:.4f}",
        ece.horizon_where,
        *[f"{ece.bucket[label]:.4f}" for label in candidates.ECE_BUCKETS],
        f"{ece.dow_type:.4f}",
        ece.dow_type_where,
        "満たす" if ece.passes else "**超える**",
    )


# ── ⑥ 意思決定の指標 ─────────────────────────────────────────
def _decisions(summaries: Sequence[Summary]) -> list[str]:
    return [
        "### 12.5 ⑥ 意思決定の指標（W6-16、開発プラン §7.3）",
        "",
        "**アプリの約束がどれだけ実現するか。** `precision@0.9` は 0.9 以上と出した行の実現率、"
        "`coverage@0.9` は 0.9 以上と出せた割合。3 段階は**割合 / 実現率**（重み付き）。"
        "**配る形はセル単位の門**（検証日ぜんぶで決めた門）で出した確率。",
        "",
        *[line for one in summaries for line in decision_table(one)],
    ]


def decision_table(one: Summary) -> list[str]:
    """1 つの候補の表。**モデルカードからも呼ぶ**（正を 2 つにしない）。"""
    return [
        f"**{one.candidate.name}**",
        "",
        _row("system / ターゲット", "形", "precision@0.9", "coverage@0.9", *_band_labels()),
        "|---|---|---:|---:|" + "---:|" * len(metrics.BANDS),
        *[
            line
            for group in one.decisions
            for line in (
                _decision_row(group, "B3", group.reference),
                _decision_row(group, "配る形", group.served),
            )
        ],
        "",
    ]


def _band_labels() -> list[str]:
    """3 段階の見出し（`metrics.BANDS` から作る。**境目の数字を書き写さない**）。"""
    return [_band_label(index) for index in range(len(metrics.BANDS))]


def _band_label(index: int) -> str:
    """いちばん上は「以上」、いちばん下（下限 0）は「未満」、間は「〜」。"""
    name, low = metrics.BANDS[index]
    if index == 0:
        return f"{name}（{low:.0%} 以上）"
    high = metrics.BANDS[index - 1][1]
    return f"{name}（{low:.0%}〜{high:.0%}）" if low > 0.0 else f"{name}（{high:.0%} 未満）"


def _decision_row(group: candidates.GroupDecision, label: str, one: metrics.Decision) -> str:
    return _row(
        f"{group.system} / {group.target}",
        label,
        _rate(one.precision),
        f"{one.coverage:.1%}",
        *[f"{band.share:.1%} / {_rate(band.realized)}" for band in one.bands],
    )


# ── ⑦ 森の大きさと 1 行 1 木の秒 ─────────────────────────────
def _forests(summaries: Sequence[Summary], facts: Mapping[Candidate, ForestFacts]) -> list[str]:
    production = "・".join(
        f"{SYSTEM_LABELS[name]} {_microseconds(seconds)}"
        for name, seconds in gates.ROW_TREE_SECONDS.items()
    )
    return [
        "### 12.6 ⑦ 森の大きさと、当てはめた機械で 1 行 1 木を歩いた秒",
        "",
        "**当てはめた機械で**、検証の行を 2 ターゲットの森でまとめて歩いた最小。本番の係数"
        f"（v0 の実測）は {production} で、手元の約 3.3 倍だった（M4 の文書 §1.3）。"
        "**費用の見積りは本番の係数で行い、ここは候補どうしの比と、係数が変わっていないかを見る。**",
        "",
        *_forest_header(),
        *[_forest_row(one.candidate, facts[one.candidate]) for one in summaries],
        "",
    ]


def _forest_header() -> list[str]:
    targets = " / ".join(target.name for target in TARGETS)
    sizes = [f"{name}（{targets}）" for name in ("木", "節", "最大の深さ")]
    speeds = [f"1 行 1 木：{label}" for label in SYSTEM_LABELS.values()]
    return [
        _row("候補", *sizes, *speeds),
        "|---|" + "---:|" * (len(sizes) + len(speeds)),
    ]


def _forest_row(candidate: Candidate, facts: ForestFacts) -> str:
    return _row(
        candidate.name,
        *[_by_target(part) for part in (facts.trees, facts.nodes, facts.depth)],
        *[_microseconds(facts.row_tree_seconds.get(name)) for name in SYSTEM_LABELS],
    )


def _by_target(values: Mapping[str, int]) -> str:
    return " / ".join(f"{values[target.name]:,}" for target in TARGETS)


def _microseconds(seconds: float | None) -> str:
    return "—" if seconds is None else f"{seconds * MICROSECONDS:.3f} µs"


# ── ⑧ 特徴量の重要度 ─────────────────────────────────────────
def _importance(summaries: Sequence[Summary], facts: Mapping[Candidate, ForestFacts]) -> list[str]:
    return [
        f"### 12.7 ⑧ 特徴量の重要度（gain の上位 {candidates.IMPORTANCE_TOP}）",
        "",
        "**割合は、その森の gain の総和に対して。** W6-29（`bikes_same_time_7d` を足すか）の材料。",
        "",
        *[
            line
            for one in summaries
            for line in _importance_table(one.candidate, facts[one.candidate])
        ],
    ]


def _importance_table(candidate: Candidate, facts: ForestFacts) -> list[str]:
    ranked = [facts.importance.get(target.name, ()) for target in TARGETS]
    depth = max((len(one) for one in ranked), default=0)
    return [
        f"<details><summary>{candidate.name}</summary>",
        "",
        _row("順位", *[part for target in TARGETS for part in (target.name, "割合")]),
        "|---:|" + "---|---:|" * len(TARGETS),
        *[_importance_row(rank, ranked) for rank in range(depth)],
        "",
        "</details>",
        "",
    ]


def _importance_row(rank: int, ranked: Sequence[tuple[candidates.Importance, ...]]) -> str:
    cells = [
        part
        for one in ranked
        for part in (
            (f"`{one[rank].column}`", f"{one[rank].share:.1%}") if rank < len(one) else ("", "")
        )
    ]
    return _row(str(rank + 1), *cells)


# ── ⑨ データの版 ─────────────────────────────────────────────
def _data(prints: Mapping[date, Fingerprint], split: DaySplit) -> list[str]:
    return [
        "### 12.8 ⑨ データの版（W6-17、開発プラン §7.5）",
        "",
        "**読んだ `features/` のバイト列そのものの SHA-256。** 同じパスで作り直すことがあるので"
        "（J1）、パスだけでは何から当てはめたかが残らない。**登録簿（`metrics.data`）と"
        "モデルカードにも同じものが入る。** 候補はすべて同じデータで当てはめた。",
        "",
        *report.fingerprint_block(prints, split),
        "",
    ]


# ── ⑩ D-32 の (c) ────────────────────────────────────────────
def _budget(summaries: Sequence[Summary]) -> list[str]:
    found = candidates.tree_budget(summaries)
    return [
        "### 12.9 ⑩ 本数の判定（D-32 の (c)）",
        "",
        "**上限の本数（費用の式から出す）と 300 本を並べ、配る形（セル単位）の改善の差が "
        f"{candidates.BUDGET_TOLERANCE_PP:g} ポイント以内なら、上限の本数で配る。** 超えたら"
        "利用者に諮る（上限を上げるか、`iad1` に移すか）。比べるのは「1 日目で選び、残りの日で"
        "測った」値（検証が 1 日なら検証日ぜんぶ）。",
        "",
        _row("候補", "比べた値", "配る形の改善", f"{candidates.REFERENCE_TREES} 本との差", "判定"),
        "|---|---|---:|---:|---|",
        *[_budget_row(one, summary) for one, summary in zip(found, summaries, strict=True)],
        "",
        *_budget_note(found),
    ]


def _budget_row(one: candidates.Budget, summary: Summary) -> str:
    held = gates.CELL_GATE in summary.judged.holdout
    verdict = "—" if one.within is None else ("以内" if one.within else "**超える**")
    return _row(
        one.candidate.name,
        "1 日目で選び、残りで測る" if held else "検証日ぜんぶ",
        _percent(candidates.budget_basis(summary).improvement),
        "—" if one.gap_pp is None else f"{one.gap_pp:+.2f} ポイント",
        verdict,
    )


def _budget_note(found: Sequence[candidates.Budget]) -> list[str]:
    if any(one.gap_pp is not None for one in found):
        return []
    return [
        f"**{candidates.REFERENCE_TREES} 本の候補が無いので判定しない**"
        f"（`--candidates` に `{candidates.REFERENCE_TREES}:0.05` を入れる）。",
        "",
    ]


# ── 書式 ─────────────────────────────────────────────────────
def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2%}"


def _rate(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def _row(*cells: str) -> str:
    return "| " + " | ".join(cells) + " |"
