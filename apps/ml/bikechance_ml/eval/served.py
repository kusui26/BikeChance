"""配った確率を実測と突き合わせる（W5 プラン §6.12 の PR L、開発プラン §8.5）。

**オフラインと同じ関数を通す。** ラベルは `features/labels.py`、除外は
`features/exclude.py`、成績は `eval/metrics.py` を呼ぶ。ここで書き直すと
**同じモデルの Brier が 2 通り出る**（開発プラン §8.5 の 1〜3）。

**標本ではなく全数である。** オフラインは抽出した行の上で測るので逆抽出確率の重みが
要るが、こちらは**配った全ポート・全サイクル**なので重みが無い。

**だから比べる相手はオフラインの「重み付き」のほう**である。重みは「標本から母集団の
値を推定する」ためのもので、**その推定値と、全数で数えた値が同じものを指す**。重み無しの
平均には標本の偏りが残るので、ずれるのが正しい（W5 プラン §12 の 165）。

**水平の起点は `generated_at`。** `jobs/infer.py` は基準時刻を 5 分格子に落とし、
その時刻を `station_forecasts.generated_at` に書く。`/v1` の `forecastHorizon` も
そこから数える（W4 プラン §12 の 114）。だから `p[i]` が当てているのは
**`generated_at + horizons_min[i]`** の時点であり、ラベルもその時刻で引く。

> **`base_observed_at` ではない。** 予測ログの**列**にあるのは基準観測の時刻で、
> `generated_at` は**覚え書き**にしか無い（W4 プラン §6.8 は「結合に使わないもの
> だけを覚え書きに」と決めたが、**水平の起点は結合に使う**）。読む側が覚え書きから
> 拾う。実測では 2 つは最大 5 分ずれる。

**格子は学習と同じ `build_grid` で作る。** 基準時刻は JST 00:00 起点の 288 点、
水平は 5 分の倍数なので、**目標時刻も同じ格子の上に乗る**——`Grid.shifted(h)` が
そのまま使える。添字の算術をここで書き直さない。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

import numpy as np
import pyarrow as pa

from bikechance_ml.baselines import persistence
from bikechance_ml.eval import metrics
from bikechance_ml.eval.dataset import BUCKET_LABELS, TARGETS, Target, bucket_index
from bikechance_ml.eval.slices import ALL
from bikechance_ml.features import asof, exclude, labels
from bikechance_ml.features.arrays import Bools, Float64, Int8, Int16, Int32, Int64
from bikechance_ml.features.constants import HORIZONS_MIN, MISSING
from bikechance_ml.features.grid import Grid

#: 確率を整数で持つときの倍率（`jobs/infer.py` の `PROBABILITY_SCALE` と同じ値）。
PROBABILITY_SCALE: Final[float] = 1000.0

#: 表に出すバケツ。**割ったものと割らないものを 1 つの並びにする。**
BUCKETS: Final[tuple[str, ...]] = (*BUCKET_LABELS, ALL)


class HorizonMismatchError(ValueError):
    """予測ログの水平が `HORIZONS_MIN` と違う。**ずれた配列を黙って読まない。**

    配列の何番目が何分先かが狂うだけで形は正しいので、**読むときには気づけない**。
    ログ側も書く前に同じ検査をしている（`jobs/forecast_log.py`）。
    """


@dataclass(frozen=True)
class Served:
    """1 日・1 システム・1 版ぶんの「配った確率」。

    `p_x1000[target][h][station][cycle]` が確率 ×1000。**水平を先頭に置く**ので、
    1 つの水平を切り出すと連続した `(ポート, サイクル)` の行列になる。
    """

    system_id: str
    model_version: str
    day: date
    #: 台帳ではなく**ログに現れたポート**の並び（昇順）。
    stations: tuple[str, ...]
    #: そのサイクル・そのポートの確率がログに在ったか。`(ポート, サイクル)`
    present: Bools
    p_x1000: Mapping[str, Int16]
    #: ログが在ったサイクルの数（288 が満点）。
    n_cycles: int


@dataclass(frozen=True)
class Truth:
    """実測を格子へ写したもの。**`features/build.py` の `_grid_state` と同じ作り方。**"""

    feature_row: Int32
    label_row: Int32
    bikes: Int16
    docks: Int16
    flags: Int16
    observed_at_ms: Int64
    label_bikes: Int16
    label_docks: Int16
    label_flags: Int16
    label_observed_ms: Int64
    phantom: Bools


@dataclass(frozen=True)
class MetricRow:
    """`model_daily_metrics` の 1 行。"""

    system_id: str
    model_version: str
    metric_date: date
    target: str
    h_min: int
    bucket: str
    n: int
    positives: float
    brier: float
    log_loss: float
    ece: float
    ece_uniform: float
    brier_b0: float
    skill_vs_b0: float | None

    def as_row(self) -> dict[str, object]:
        """`upsert_model_daily_metrics` に渡す形。"""
        return {
            "model_version": self.model_version,
            "metric_date": self.metric_date.isoformat(),
            "system_id": self.system_id,
            "target": self.target,
            "h_min": self.h_min,
            "bucket": self.bucket,
            "n": self.n,
            "positives": round(self.positives, 6),
            "brier": round(self.brier, 8),
            "log_loss": round(self.log_loss, 8),
            "ece": round(self.ece, 8),
            "ece_uniform": round(self.ece_uniform, 8),
            "brier_b0": round(self.brier_b0, 8),
            "skill_vs_b0": None if self.skill_vs_b0 is None else round(self.skill_vs_b0, 6),
        }


def check_horizons(horizons: Sequence[int], where: str) -> None:
    """ログの水平が `HORIZONS_MIN` と同じか。**違えば読まない。**"""
    if tuple(horizons) != HORIZONS_MIN:
        raise HorizonMismatchError(f"{where} の水平が {HORIZONS_MIN} と違います: {tuple(horizons)}")


def to_truth(table: pa.Table, keys: Sequence[tuple[str, str]], grid: Grid) -> Truth:
    """観測を格子へ写す。**`as_of_feature` と `as_of_label` を使い分ける。**

    特徴量側は `fetched_at <= t`、ラベル側は `observed_at <= t + h`——**規則は
    `features/asof.py` の 1 か所**で、ここは呼ぶだけである（開発プラン §6.2）。
    """
    observations = asof.to_observations(table, keys)
    feature_row = asof.as_of_feature(observations, grid.times_ms)
    label_row = asof.as_of_label(observations, grid.times_ms)
    return Truth(
        feature_row=feature_row,
        label_row=label_row,
        bikes=asof.gather(observations.bikes, feature_row, MISSING),
        docks=asof.gather(observations.docks, feature_row, MISSING),
        flags=asof.gather(observations.flags, feature_row, MISSING),
        observed_at_ms=asof.gather(observations.observed_at_ms, feature_row, 0),
        label_bikes=asof.gather(observations.bikes, label_row, MISSING),
        label_docks=asof.gather(observations.docks, label_row, MISSING),
        label_flags=asof.gather(observations.flags, label_row, MISSING),
        label_observed_ms=asof.gather(observations.observed_at_ms, label_row, 0),
        phantom=exclude.phantom_mask(keys),
    )


def base_exclusion(truth: Truth, grid: Grid, present: Bools) -> exclude.Excluded:
    """基準時刻の側の除外。**配信が既に通した行を、実測の側からも見る。**

    ログに在る行は `build_now` が通したものだが、**同じ規則を保存済みの観測に当てて
    測り直す**。食い違いはそのまま内訳に出るので、「配信と評価が違うものを見ている」
    ことに気づける。
    """
    times = np.asarray(grid.times_ms, dtype=np.int64)[None, :]
    span = grid.day_slice()
    return exclude.exclude_at_base(
        feature_row=truth.feature_row[:, span],
        observed_at_ms=truth.observed_at_ms[:, span],
        grid_ms=times[:, span],
        bikes=truth.bikes[:, span],
        docks=truth.docks[:, span],
        flags=truth.flags[:, span],
        phantom=truth.phantom,
        alive=present,
    )


def target_exclusion(
    truth: Truth, grid: Grid, base: exclude.Excluded, horizon: int
) -> exclude.Excluded:
    """1 つの水平で残る行。**目標側の除外は `exclude_at_target` に任せる。**"""
    times = np.asarray(grid.times_ms, dtype=np.int64)[None, :]
    day, target = grid.day_slice(), grid.shifted(horizon)
    return exclude.exclude_at_target(
        alive=base.keep,
        feature_row=truth.feature_row[:, day],
        label_row=truth.label_row[:, target],
        observed_at_ms=truth.label_observed_ms[:, target],
        target_ms=times[:, target],
        bikes=truth.label_bikes[:, target],
        docks=truth.label_docks[:, target],
    )


def label_of(truth: Truth, grid: Grid, target: Target, horizon: int) -> Int8:
    """`t + h` のラベル。**`features/labels.py` を通す**（規則を書き直さない）。"""
    span = grid.shifted(horizon)
    flags = truth.label_flags[:, span]
    if target.name == "bike":
        return labels.y_bike(truth.label_bikes[:, span], flags)
    return labels.y_dock(truth.label_docks[:, span], flags)


def counts_of(truth: Truth, grid: Grid, target: Target) -> Int16:
    """基準時刻の台数（バケツと B0 の材料）。当てる側と同じ指標を使う（§12 の 100）。"""
    span = grid.day_slice()
    return truth.bikes[:, span] if target.counts == "bikes" else truth.docks[:, span]


def probability_of(served: Served, target: Target, index: int) -> Float64:
    """配った確率。**整数で配ったものをそのまま読む**（丸め戻さない）。"""
    return np.asarray(served.p_x1000[target.name][index], dtype=np.float64) / PROBABILITY_SCALE


def score_one(
    *,
    served: Served,
    target: Target,
    horizon: int,
    keep: Bools,
    bucket: Int8,
    probability: Float64,
    label: Int8,
    reference: Float64,
) -> list[MetricRow]:
    """1 つの水平を、バケツごとと全体で測る。**同じ行の上で全部を測る**（§4.4 の 30b）。"""
    rows: list[MetricRow] = []
    for number, name in enumerate(BUCKETS):
        mask = keep if name == ALL else np.asarray(keep & (bucket == number), dtype=np.bool_)
        if not mask.any():
            continue
        rows.append(
            _row(
                served=served,
                target=target,
                horizon=horizon,
                bucket=name,
                scores=metrics.score(label[mask], probability[mask]),
                brier_b0=metrics.brier(label[mask], reference[mask]),
            )
        )
    return rows


def _row(
    *,
    served: Served,
    target: Target,
    horizon: int,
    bucket: str,
    scores: metrics.Scores,
    brier_b0: float,
) -> MetricRow:
    return MetricRow(
        system_id=served.system_id,
        model_version=served.model_version,
        metric_date=served.day,
        target=target.name,
        h_min=horizon,
        bucket=bucket,
        n=scores.n,
        positives=scores.positives,
        brier=scores.brier,
        log_loss=scores.log_loss,
        ece=scores.ece,
        ece_uniform=scores.ece_uniform,
        brier_b0=brier_b0,
        skill_vs_b0=metrics.skill(scores.brier, brier_b0),
    )


@dataclass(frozen=True)
class Outcome:
    """1 日・1 版ぶんの成績と、落とした理由の内訳。"""

    rows: tuple[MetricRow, ...]
    #: 基準側は水平によらないので 1 度だけ、目標側は水平ごとに足した数
    dropped: dict[str, int]
    #: 除外を通って測れた行（`(ポート, サイクル, 水平)` の数、2 ターゲットぶんではない）
    n_kept: int


def _one_horizon(
    served: Served, truth: Truth, grid: Grid, base: exclude.Excluded, index: int
) -> tuple[list[MetricRow], exclude.Excluded]:
    """1 つの水平を 2 ターゲットぶん測る。**除外は 1 度だけ引く**（両者で同じ行）。"""
    horizon = HORIZONS_MIN[index]
    kept = target_exclusion(truth, grid, base, horizon)
    rows: list[MetricRow] = []
    for target in TARGETS:
        counts = counts_of(truth, grid, target)
        rows.extend(
            score_one(
                served=served,
                target=target,
                horizon=horizon,
                keep=kept.keep,
                bucket=bucket_index(counts),
                probability=probability_of(served, target, index),
                label=label_of(truth, grid, target, horizon),
                reference=persistence.predict(counts),
            )
        )
    return rows, kept


def evaluate(served: Served, truth: Truth, grid: Grid) -> Outcome:
    """1 日ぶんを測る。**水平ごとに解く**（1 度に載せるのは `(ポート, サイクル)` だけ）。"""
    base = base_exclusion(truth, grid, served.present)
    rows: list[MetricRow] = []
    dropped: dict[str, int] = dict(base.counts)
    n_kept = 0
    for index in range(len(HORIZONS_MIN)):
        part, kept = _one_horizon(served, truth, grid, base, index)
        rows.extend(part)
        n_kept += int(kept.keep.sum())
        for name, number in kept.counts.items():
            dropped[name] = dropped.get(name, 0) + number
    return Outcome(rows=tuple(rows), dropped=dropped, n_kept=n_kept)
