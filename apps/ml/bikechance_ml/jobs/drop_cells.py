"""合成器で削るセルを、日次評価から決める（W6-12 の決まり、W8 の PR G）。

**比べるのは「いまの合成器」と「いまの shadow の B3」**——B3 だけの週は合成器の中の B3 と
shadow の B3 が同じバイト列なので（W6-39）、森を歩くセルでの比べは**森と B3 の比べ**になる。
件数（`n`）で重み付けた Brier が合成器のほうが大きいセルを、次の合成器では B3 に戻す。

守ることは 3 つ（#4・#5 を手で回したときの `drop_check.sql` と同じ）。

  * **比べる日は昇格の翌日から**。昇格の日は前の版と時間帯が混ざる（#5 で 10-20 を外した）。
    `last` までの直近 `WINDOW_DAYS` 日に限る
  * **削るだけで、足さない**（W6-12）。前の合成器で削ったセルは、比べられない（そこでは
    合成器も B3 を出す）ので、**そのまま引き継ぐ**
  * **両方の成績がそろったセルだけを比べる**。片方しか無いセルで決めない

ここは純粋な関数だけを置く。日次評価を読むのは `io/supabase.py`、合成器を開くのは
`jobs/weekly_refit.py` である。
"""

from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

from bikechance_ml.models.gates import CellKey

#: 比べる日の上限（W8-02：直近 7 日）。
WINDOW_DAYS: Final[int] = 7


@dataclass(frozen=True)
class DailyMetric:
    """`model_daily_metrics` の 1 行のうち、比べるのに要る分だけ（0050）。"""

    model_version: str
    metric_date: date
    system_id: str
    target: str
    h_min: int
    bucket: str
    n: int
    brier: float

    @property
    def cell(self) -> CellKey:
        return (self.system_id, self.target, self.h_min, self.bucket)


@dataclass(frozen=True)
class CellComparison:
    """森を歩く 1 セルの、合成器と B3 の比べ（件数で重み付けた Brier）。"""

    cell: CellKey
    days: int
    n: int
    composite: float
    b3: float

    @property
    def worse(self) -> bool:
        """**合成器（＝森）が B3 より悪い。** 引き分けは削らない。"""
        return self.composite > self.b3

    @property
    def improvement(self) -> float:
        """森の B3 に対する改善（正が良い）。"""
        return 1.0 - self.composite / self.b3 if self.b3 > 0 else 0.0


@dataclass(frozen=True)
class DropDecision:
    """削るセルの決め方の結果。**記録とカードにそのまま出す。**"""

    composite: str
    b3: str
    days: tuple[date, ...]
    compared: tuple[CellComparison, ...]
    carried: tuple[CellKey, ...]
    dropped: tuple[CellKey, ...]

    @property
    def newly_dropped(self) -> tuple[CellKey, ...]:
        return tuple(cell for cell in self.dropped if cell not in self.carried)

    def summary(self) -> dict[str, object]:
        """`job_runs` と記録に出す要約。"""
        worse = [one for one in self.compared if one.worse]
        return {
            "composite": self.composite,
            "b3": self.b3,
            "days": [one.isoformat() for one in self.days],
            "compared": len(self.compared),
            "worse": len(worse),
            "carried": len(self.carried),
            "dropped": [list(cell) for cell in self.dropped],
            "improvement": overall_improvement(self.compared),
        }


def window_days(promoted_on: date | None, last: date) -> tuple[date, ...]:
    """比べる日。**昇格の翌日から `last` まで**、直近 `WINDOW_DAYS` 日に限る。"""
    first = last - timedelta(days=WINDOW_DAYS - 1)
    if promoted_on is not None:
        first = max(first, promoted_on + timedelta(days=1))
    span = (last - first).days + 1
    return tuple(first + timedelta(days=step) for step in range(max(span, 0)))


def compare_cells(
    rows: Iterable[DailyMetric], cells: Collection[CellKey], composite: str, b3: str
) -> tuple[CellComparison, ...]:
    """森を歩くセルごとに比べる。**両方の版の成績がそろったセルだけ**を、セルの順に返す。"""
    sums: dict[tuple[CellKey, str], list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for row in rows:
        if row.cell in cells and row.model_version in (composite, b3):
            total = sums[(row.cell, row.model_version)]
            total[0] += row.n
            total[1] += row.brier * row.n
            total[2] += 1
    return tuple(
        _comparison(cell, sums[(cell, composite)], sums[(cell, b3)])
        for cell in sorted(cells)
        if (cell, composite) in sums and (cell, b3) in sums
    )


def _comparison(cell: CellKey, mine: Sequence[float], theirs: Sequence[float]) -> CellComparison:
    return CellComparison(
        cell=cell,
        days=int(min(mine[2], theirs[2])),
        n=int(mine[0]),
        composite=mine[1] / mine[0],
        b3=theirs[1] / theirs[0],
    )


def decide(
    compared: Sequence[CellComparison],
    carried: Collection[CellKey],
    *,
    composite: str,
    b3: str,
    days: Sequence[date],
) -> DropDecision:
    """削るセル ＝ 前に削ったセルと、森が B3 より悪いセルの和（**足さない**。W6-12）。"""
    dropped = set(carried) | {one.cell for one in compared if one.worse}
    return DropDecision(
        composite=composite,
        b3=b3,
        days=tuple(days),
        compared=tuple(compared),
        carried=tuple(sorted(carried)),
        dropped=tuple(sorted(dropped)),
    )


def overall_improvement(compared: Sequence[CellComparison]) -> float | None:
    """森を歩くセル全体の改善（件数で重み付け）。**比べたセルが無ければ None。**"""
    mine = sum(one.composite * one.n for one in compared)
    theirs = sum(one.b3 * one.n for one in compared)
    return None if theirs <= 0 else 1.0 - mine / theirs


def drop_lines(cells: Iterable[CellKey]) -> list[str]:
    """`build_composite --drop-file` と同じ 1 行 1 セルの形（`hellocycling bike 30 0`）。"""
    return [" ".join(str(part) for part in cell) for cell in cells]
