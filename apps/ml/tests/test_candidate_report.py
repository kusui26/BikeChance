"""候補を並べた報告書の §12（`eval/candidate_report.py`、W6 の PR F の ①〜⑩）。

**主題は「読む人が取り違えないこと」。** 候補の比べ方（どの値で比べたか・どの候補を
選んだか・どのセルで LightGBM を配るか）は、表の書き方ひとつで読み違える。**入力は手で
組み、出る文字列をそのまま確かめる**（`harness` を通すと、どの数字が出るべきかが手で書けない）。
"""

from datetime import date
from typing import Final

from bikechance_ml.eval import candidate_report, candidates, gates, metrics
from bikechance_ml.eval.candidates import Candidate, ForestFacts, Importance, Summary
from bikechance_ml.eval.split import DaySplit
from bikechance_ml.features.fingerprint import Fingerprint

DAYS: Final[tuple[date, ...]] = (date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7))
SPLIT: Final[DaySplit] = DaySplit(fit=(DAYS[0],), purge=(DAYS[1],), evaluate=(DAYS[2],))
SMALL: Final[Candidate] = Candidate(50, 0.15)
LARGE: Final[Candidate] = Candidate(300, 0.05)
SHA: Final[str] = "0123456789abcdef" * 4


def _cell(h_min: int, bucket: str, *, b0: float, model: float) -> gates.Scored:
    """HELLO の bike の 1 セル。**B3 は 0.10 に固定**し、候補の Brier で理由を決める。"""
    return gates.Scored(
        system="hellocycling",
        target="bike",
        h_min=h_min,
        bucket=bucket,
        n=10,
        weight=10.0,
        b0=b0,
        b3=0.10,
        model=model,
    )


#: 越える（+20%）・届かない（+5%）・判定の対象外（B0 がほぼ 0）の 3 セル。
CELLS: Final[tuple[gates.Scored, ...]] = (
    _cell(30, "0", b0=0.2, model=0.08),
    _cell(30, "1", b0=0.2, model=0.095),
    _cell(5, "11+", b0=0.0005, model=0.01),
)

BANDS: Final[tuple[metrics.BandScore, ...]] = (
    metrics.BandScore("高", 0.85, 0.5, 0.9),
    metrics.BandScore("中", 0.60, 0.3, 0.7),
    metrics.BandScore("低", 0.0, 0.2, None),
)
DECISION: Final[metrics.Decision] = metrics.Decision(precision=0.93, coverage=0.4, bands=BANDS)


def _summary(candidate: Candidate, *, holdout: float | None, ece: float = 0.02) -> Summary:
    served = {unit: gates.Served(improvement=0.12, forest_share=0.6) for unit in gates.UNITS}
    held = (
        {}
        if holdout is None
        else {unit: gates.Served(improvement=holdout, forest_share=0.55) for unit in gates.UNITS}
    )
    judged = gates.Judged(
        model=candidate.name, cells=CELLS, horizons=(), served=served, holdout=held
    )
    group = candidates.GroupDecision("hellocycling", "bike", DECISION, DECISION)
    summary_ece = candidates.EceSummary(
        ece, "hellocycling / bike / 5", {"0": 0.01, "1": 0.02}, 0.01, "—"
    )
    return Summary(candidate, judged, summary_ece, (group,))


def _facts(seconds: dict[str, float]) -> ForestFacts:
    return ForestFacts(
        trees={"bike": 50, "dock": 50},
        nodes={"bike": 12_345, "dock": 6_789},
        depth={"bike": 14, "dock": 12},
        row_tree_seconds=seconds,
        importance={
            "bike": (Importance("bikes", 9.0, 0.9), Importance("h_min", 1.0, 0.1)),
            "dock": (),
        },
    )


FACTS: Final[dict[Candidate, ForestFacts]] = {
    SMALL: _facts({"hellocycling": 2.2e-7}),
    LARGE: _facts({"hellocycling": 2.1e-7, "docomo-cycle": 2.3e-7}),
}
PRINTS: Final[dict[date, Fingerprint]] = {day: Fingerprint(rows=100, sha256=SHA) for day in DAYS}


def _render(*summaries: Summary, chosen: Candidate | None = SMALL) -> str:
    return "\n".join(candidate_report.render(summaries, FACTS, chosen, PRINTS, SPLIT))


BOTH: Final[tuple[Summary, ...]] = (
    _summary(SMALL, holdout=0.10),
    _summary(LARGE, holdout=0.115),
)


# ── 並び ──────────────────────────────────────────────────────
def test_the_ten_items_come_in_order() -> None:
    """**①〜⑩ が番号の順に出る**（W6 プラン §6.6 の完了条件 2）。"""
    text = _render(*BOTH)
    marks = ["①", "②", "③", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"]
    places = [text.index(f"### 12.{index + 1} {mark}") for index, mark in enumerate(marks)]
    assert places == sorted(places)
    assert "④ 森を歩く行" in text


def test_the_chosen_candidate_is_named_and_its_gates_are_open() -> None:
    text = _render(*BOTH, chosen=SMALL)
    assert f"**{SMALL.name}**（`--choose {SMALL.spec}`）" in text
    assert f"<details open><summary>{SMALL.name}</summary>" in text
    assert f"<details><summary>{LARGE.name}</summary>" in text


def test_without_a_choice_it_says_so() -> None:
    """**選んでいない回**（10/8 の比較）は、置く・書くのは選んだ候補だけと言う。"""
    text = _render(*BOTH, chosen=None)
    assert "**選んでいない**" in text
    assert "<details open>" not in text


# ── ① ② 門 ───────────────────────────────────────────────────
def test_the_gate_counts_are_per_candidate() -> None:
    """**越えた 1 / 判定の対象 2 / 全部 3**（対象外は判定に数えない）。"""
    assert f"| {SMALL.name} | 1 | 2 | 3 | 0 | 0 | 0 |" in _render(*BOTH)


def test_the_gate_table_marks_what_goes_to_lightgbm() -> None:
    """**太字が LightGBM を配るセル**、— は判定の対象外、空欄は行の無いセル。"""
    text = _render(*BOTH)
    assert "| 30 分 | **+20.00%** | +5.00% |  |  |  |  |" in text
    assert "| 5 分 |  |  |  |  |  | — |" in text


# ── ③ ④ 配る形 ───────────────────────────────────────────────
def test_the_served_rows_show_the_held_out_value() -> None:
    text = _render(*BOTH)
    label = gates.UNIT_LABELS[gates.CELL_GATE]
    assert f"| {SMALL.name} | {label} | +12.00% | +10.00% | 60.0% | 55.0% |" in text


def test_a_single_evaluation_day_has_no_held_out_value() -> None:
    """**検証が 1 日なら「残りで測る」は無い**（— と書き、0 と書かない）。"""
    text = _render(_summary(SMALL, holdout=None))
    label = gates.UNIT_LABELS[gates.CELL_GATE]
    assert f"| {SMALL.name} | {label} | +12.00% | — | 60.0% | — |" in text


# ── ⑤ ⑥ 校正と意思決定 ───────────────────────────────────────
def test_a_calibration_over_the_limit_is_called_out() -> None:
    text = _render(_summary(SMALL, holdout=None, ece=0.031))
    assert (
        "| 0.0310 | hellocycling / bike / 5 | 0.0100 | 0.0200 | 0.0100 | — | **超える** |" in text
    )


def test_the_band_labels_come_from_the_thresholds() -> None:
    """**境目の数字を書き写さない**（`metrics.BANDS` から作る。TS の 3 段階と同じ値）。"""
    text = _render(*BOTH)
    assert "高（85% 以上）" in text
    assert "中（60%〜85%）" in text
    assert "低（60% 未満）" in text


def test_a_band_without_rows_is_a_dash() -> None:
    """**入った行が無い段は —**（0% は「全部外れた」なので書き分ける）。"""
    row = "| hellocycling / bike | 配る形 | 93.0% | 40.0% |"
    assert f"{row} 50.0% / 90.0% | 30.0% / 70.0% | 20.0% / — |" in _render(*BOTH)


# ── ⑦ ⑧ 森 ──────────────────────────────────────────────────
def test_the_forest_row_shows_sizes_and_speeds() -> None:
    """**測れなかった系統は —**（検証の行が無い）。µs で出す。"""
    text = _render(*BOTH)
    assert f"| {SMALL.name} | 50 / 50 | 12,345 / 6,789 | 14 / 12 | 0.220 µs | — |" in text
    assert f"| {LARGE.name} | 50 / 50 | 12,345 / 6,789 | 14 / 12 | 0.210 µs | 0.230 µs |" in text


def test_the_importance_table_pads_the_shorter_target() -> None:
    """**ターゲットで上位の数が違っても、行は揃える**（短いほうは空欄）。"""
    text = _render(*BOTH)
    assert "| 1 | `bikes` | 90.0% |  |  |" in text
    assert "| 2 | `h_min` | 10.0% |  |  |" in text


# ── ⑨ データの版 ─────────────────────────────────────────────
def test_the_data_table_keeps_the_whole_digest() -> None:
    """**64 桁をそのまま**（縮めると、同じパスで作り直した日と見分けられない）。"""
    text = _render(*BOTH)
    assert f"| 2026-10-05 | 学習 | 100 | `{SHA}` |" in text
    assert f"| 2026-10-06 | パージ | 100 | `{SHA}` |" in text
    assert f"| 2026-10-07 | 検証 | 100 | `{SHA}` |" in text


# ── ⑩ D-32 の (c) ────────────────────────────────────────────
def test_the_budget_compares_with_300_trees_on_the_held_out_value() -> None:
    """**50 本 +10.00% 対 300 本 +11.50% は 1.50 ポイント差で「超える」。**"""
    text = _render(*BOTH)
    assert (
        f"| {SMALL.name} | 1 日目で選び、残りで測る | +10.00% | +1.50 ポイント | **超える** |"
        in text
    )
    assert f"| {LARGE.name} | 1 日目で選び、残りで測る | +11.50% | +0.00 ポイント | 以内 |" in text


def test_without_300_trees_the_budget_is_not_judged() -> None:
    text = _render(_summary(SMALL, holdout=0.1))
    assert "**300 本の候補が無いので判定しない**" in text
    assert f"| {SMALL.name} | 1 日目で選び、残りで測る | +10.00% | — | — |" in text
