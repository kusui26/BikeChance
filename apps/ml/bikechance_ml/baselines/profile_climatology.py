"""B2（気候値）を**ポートプロファイルから**作る（W5 プラン §6.4 の PR D、W5-01/02）。

**B2 とポートプロファイルは同じ量である**（W5-01）。開発プラン §6.3 の `prof_p_bike` と
§7.1 の B2 は、**同じ `(ポート, 曜日種別, 15 分枠)` のセルを別の名前で呼んでいる**。
表を 1 つにして、両方がそこから引く。

**`features/` から作らない**（W5-02）。抽出済みのサンプルは 1 セル 1 日あたり **1.62 行
（中央 1）**しか無く、下限（2 件かつ 2 日）を満たすセルがほとんど出ない。実測では
**気候値が 100% B1 に落ち**、配信中の確率は 1 システム 1 水平あたり 4〜6 個の値しか
取っていなかった（W5 プラン §2.3）。**全格子なら 1 枠に 3 点**入るので、2 日目には
ほぼ全セルが下限を満たす（実測：09-12 の版で平日セルの 99.9%）。

**配信は「その日を 1 点も含まないプロファイル」で予測する。** 学習期間の行に当てはめる
ときも同じにする——**その行の日ぶんを丸ごと引く**（`daily.parquet` がその引く量その
ものである）。1 点だけ引く leave-one-out では、**5 分違いのほとんど同じ観測が 2 つ
残る**（1 枠は 3 点＝`GRID_POINTS_PER_SLOT`）。W5 プラン §6.4 は
`predict_leave_one_out` をそのまま使うと書いていたが、**抽出重みは 25 と 100 で、
高々 84 点のセルから引くと分母が負になる**（§12 の 143）。

**ここは純粋な部分だけを持つ。** Parquet を読むのは `jobs/climate.py`。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np
import pyarrow as pa

from bikechance_ml.baselines import climatology
from bikechance_ml.eval import dataset
from bikechance_ml.eval.dataset import Samples, Target
from bikechance_ml.features import profile
from bikechance_ml.features.arrays import Bools, Float64, Int16, Int64, Strings
from bikechance_ml.features.calendar import DOW_TYPE_ORDER


class PortsMismatchError(ValueError):
    """プロファイルを読むときに使ったポートの並びが、いまのサンプルと違う。

    **番号の振り直しは静かに別のセルを指す**（W5 プラン §12 の 132 と同じ形）ので、
    例外にして止める。
    """


def ok_column(target: Target) -> str:
    """そのターゲットの「当たった格子点の数」の列名（`n_bike_ok` / `n_dock_ok`）。"""
    return f"n_{target.name}_ok"


def cells(table: pa.Table, ports: Sequence[str]) -> tuple[Int64, Bools]:
    """プロファイルの行を**気候値のセル番号**にする。**並びに無いポートは印を落とす。**

    ポートの並びを決めるのは学習サンプル（`Samples.ports`）である。プロファイルには
    **その期間に 1 度も抽出されなかったポート**が入っていることがある（実測：09-12 の
    版で 193）。番号を振れないので落とす——**気候値の層が無いだけで B1 は正しく出る。**
    """
    names = dataset.port_keys(_strings(table, "system_id"), _strings(table, "station_id"))
    order = np.asarray(ports, dtype=np.str_)
    if len(order) == 0:
        return np.zeros(len(names), dtype=np.int64), np.zeros(len(names), dtype=np.bool_)
    index = np.searchsorted(order, names)
    safe = np.clip(index, 0, len(order) - 1)
    inside = np.asarray((index < len(order)) & (order[safe] == names), dtype=np.bool_)
    dow = dataset.dow_type_indices(_strings(table, "dow_type")).astype(np.int64)
    return climatology.cell_key(safe.astype(np.int64), dow, _ints(table, "slot15")), inside


def fit(
    profile_table: pa.Table,
    *,
    ports: Sequence[str],
    target: Target,
    min_samples: int = climatology.MIN_CELL_SAMPLES,
    min_days: int = climatology.MIN_CELL_DAYS,
) -> climatology.Table:
    """プロファイルの累計を B2 の表にする。**下限と率の決め方は `table_of` に任せる。**

    `total` と `counted` はどちらも**格子点の数**である（重みが無いので同じ）。
    """
    profile.require_schema(profile_table, profile.PROFILE_SCHEMA)
    key, inside = cells(profile_table, ports)
    size = len(ports) * len(DOW_TYPE_ORDER) * climatology.SLOTS_PER_DAY
    taken = key[inside]
    points = np.bincount(taken, weights=_floats(profile_table, "n")[inside], minlength=size)
    return climatology.table_of(
        n_ports=len(ports),
        total=points,
        positive=np.bincount(
            taken, weights=_floats(profile_table, ok_column(target))[inside], minlength=size
        ),
        counted=points.astype(np.int64),
        days=np.bincount(
            taken, weights=_floats(profile_table, "n_days")[inside], minlength=size
        ).astype(np.int64),
        min_samples=min_samples,
        min_days=min_days,
    )


# ── 自分の日を引く ────────────────────────────────────────────
@dataclass(frozen=True)
class DayCells:
    """1 日ぶんの寄与（`daily.parquet`）。**セル番号の昇順**に並べてある。"""

    key: Int64
    n: Int16
    ok: Mapping[str, Int16]

    def take(self, wanted: Int64, target: Target) -> tuple[Bools, Float64, Float64]:
        """セルごとに `(その日が寄与したか, 点の数, ラベルが 1 の数)`。無ければ 0。"""
        zero = np.zeros(len(wanted), dtype=np.float64)
        if len(self.key) == 0:
            return np.zeros(len(wanted), dtype=np.bool_), zero, zero.copy()
        index = np.clip(np.searchsorted(self.key, wanted), 0, len(self.key) - 1)
        found = np.asarray(self.key[index] == wanted, dtype=np.bool_)
        return (
            found,
            np.asarray(np.where(found, self.n[index], 0), dtype=np.float64),
            np.asarray(np.where(found, self.ok[target.name][index], 0), dtype=np.float64),
        )


@dataclass(frozen=True)
class Dailies:
    """学習期間の**日ごと**の寄与。`profile` から自分の日を引くのに使う。"""

    #: JST の暦日の通し番号（`date.toordinal()`）→ その日ぶん
    by_day: Mapping[int, DayCells]

    def covers(self, samples: Samples) -> Bools:
        """**その行の日ぶんを引けるか。** 引けない日の行は混合の当てはめから外す。"""
        known = np.asarray(sorted(self.by_day), dtype=np.int32)
        return np.asarray(np.isin(samples.day, known), dtype=np.bool_)


def day_cells(daily: pa.Table, ports: Sequence[str]) -> DayCells:
    """`daily.parquet` を引ける形にする。**セル番号で並べ替える。**"""
    profile.require_schema(daily, profile.DAILY_SCHEMA)
    key, inside = cells(daily, ports)
    taken = key[inside]
    order = np.argsort(taken, kind="stable")
    return DayCells(
        key=np.asarray(taken[order], dtype=np.int64),
        n=np.asarray(_ints(daily, "n")[inside][order], dtype=np.int16),
        ok={
            target.name: np.asarray(_ints(daily, ok_column(target))[inside][order], dtype=np.int16)
            for target in dataset.TARGETS
        },
    )


def own_day(dailies: Dailies, samples: Samples, target: Target) -> climatology.Own:
    """**その行の日ぶんを丸ごと**引く量（`climatology.predict_without` に渡す）。

    **引けない日の行は 0 を返す**（引かない）。そのまま混ぜると B2 が自分の答えを見る
    ので、**呼ぶ側は `Dailies.covers` で先に絞る**（`FromProfile.blend_rows`）。
    """
    key = climatology.cell_key(
        samples.port.astype(np.int64),
        samples.dow_type.astype(np.int64),
        climatology.slot15(samples),
    )
    total = np.zeros(len(samples), dtype=np.float64)
    positive = np.zeros(len(samples), dtype=np.float64)
    days = np.zeros(len(samples), dtype=np.int64)
    for ordinal, cell in dailies.by_day.items():
        rows = np.asarray(samples.day == ordinal, dtype=np.bool_)
        found, points, hits = cell.take(key[rows], target)
        total[rows], positive[rows], days[rows] = points, hits, found
    return climatology.Own(
        total=total, positive=positive, counted=total.astype(np.int64), days=days
    )


# ── B2 の作り方（`climatology.Source` の実装）──────────────────
@dataclass(frozen=True)
class FromProfile:
    """**ポートプロファイルから作る**（W5 の PR D）。

    `profile` は `profiles/date=D/profile.parquet`——**D 当日までの累計**である
    （`features/profile.py` の `source_day` と同じ作法）。**どの日で作るかは版が決めて
    いる**ので、学習期間を絞っても作り直せない：呼ぶ側が「**学習の最終日の版**」を
    渡すこと（`jobs/climate.py`）。渡し間違えると検証期間が混ざる。
    """

    #: どの日の版か（報告用。**当日までを含む**）
    day: date
    profile: pa.Table
    dailies: Dailies
    #: `dailies` を作ったときのポートの並び。**サンプルと違えば例外にする**
    ports: tuple[str, ...]

    def table(self, samples: Samples, target: Target, keep: Bools) -> climatology.Table:  # noqa: ARG002
        """**`keep` は見ない。** どの日までを含むかはプロファイルの版が決めている。"""
        if samples.ports != self.ports:
            raise PortsMismatchError("プロファイルを読んだときとポートの並びが違います")
        return fit(self.profile, ports=self.ports, target=target)

    def leave_out(
        self,
        table: climatology.Table,
        samples: Samples,
        target: Target,
        fallback: Float64,
    ) -> climatology.Applied:
        return climatology.predict_without(
            table, samples, fallback, own_day(self.dailies, samples, target)
        )

    def blend_rows(self, samples: Samples) -> Bools:
        return self.dailies.covers(samples)

    def describe(self) -> str:
        return f"プロファイル（profiles/date={self.day}、引ける日 {len(self.dailies.by_day)}）"


def _strings(table: pa.Table, name: str) -> Strings:
    return np.array(table.column(name).to_pylist(), dtype=np.str_)


def _ints(table: pa.Table, name: str) -> Int64:
    return np.asarray(
        table.column(name).combine_chunks().to_numpy(zero_copy_only=False), dtype=np.int64
    )


def _floats(table: pa.Table, name: str) -> Float64:
    return np.asarray(
        table.column(name).combine_chunks().to_numpy(zero_copy_only=False), dtype=np.float64
    )
