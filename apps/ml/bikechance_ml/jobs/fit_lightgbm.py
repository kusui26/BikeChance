"""LightGBM v1 の候補を当てはめ、門の表を決め、選んだ 1 つを置いて登録する（W6 の PR F）。

**ここは副作用の置き場所。** 行列の作り方は `models/matrix.py`、成果物は
`models/artifact.py`、門の表は `models/gates.py`、候補の比べ方は `eval/candidates.py`、
報告書の §12 は `eval/candidate_report.py` にある（CLAUDE.md §3）。

**v1 は LightGBM 単体では配らない**（W6 の契約 33）。セルごとに、LightGBM が門の相手
（**本番と同じ作り方の B3**。W6-10、契約 36）を 10% 以上改善したところだけ LightGBM を
配る。**その門の表はここで決め、森の隣に置く**（契約 35）。合成器（PR H）がそれを読み、
1 つの成果物に焼き付ける。

**候補は本数と学習率の組で受ける**（契約 37）。同じ行列・同じ `Dataset` から候補を順に
育て、**同じ検証の行の上で**並べる（報告書の §12）。**置くのは `--choose` で選んだ 1 つ
だけ**で、選ぶのは人である（報告書の ⑩、D-32 の (c) を読んでから）。

使い方（環境変数は `.env` から読み込んでから）:

    set -a; . ./.env; set +a
    cd apps/ml
    # 候補を並べる（置かない）
    ./.venv/bin/python -m bikechance_ml.jobs.fit_lightgbm \\
        --to 2026-10-07 --train-days 27 --eval-days 2 \\
        --candidates 50:0.15,100:0.15,300:0.05 --report /tmp/fit.md
    # 選んだ 1 つを置いて登録する（**確認を取ってから**）
    ./.venv/bin/python -m bikechance_ml.jobs.fit_lightgbm \\
        --to 2026-10-08 --train-days 28 --eval-days 2 --candidates 100:0.15 \\
        --card ../../docs/model_cards/{version}.md --upload --register

**28 日ぶんは手元（8 GB）に載らない**ので、本番の当てはめは Actions の
`fit-lightgbm.yml` で回す（W6-18）。

**当てはめた木は numpy の形に平たくしてから配る**（W4 の PR E′）。配信のランタイムに
OpenMP が無く `import lightgbm` が落ちるため（W4 プラン §12 の 126）。**平たくした森が
`Booster.predict` と同じ値を出すことを、成果物を書く前に照合する**
（`refuse_if_different`）。1 つでも違えば書かない。

**天気の被覆が日によって違えば、当てはめる前に止まる**（W4 の PR I）。承知のうえで
混ぜるなら `--allow-mixed-weather` を付ける——**付けた事実は
`model_versions.metrics.weather` に残る。**

**早期終了は使わない。** 検証の行は門を決めるのに使う。本数の選択にまで使うと、その行で
B3 と比べる意味がさらに薄れる（同じ行で選んで測るほど良く見える。W6-14）。本数は候補
ごとに固定し、並べて人が選ぶ。
"""

import argparse
import hashlib
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final

import lightgbm as lgb
import numpy as np

from bikechance_ml.baselines import climatology
from bikechance_ml.config import read_storage_config
from bikechance_ml.eval import candidate_report, candidates, gates, harness, metrics, report
from bikechance_ml.eval.candidates import (
    Candidate,
    CandidateError,
    EceSummary,
    ForestFacts,
    Importance,
    Summary,
)
from bikechance_ml.eval.dataset import TARGETS, Samples, Target
from bikechance_ml.eval.split import DaySplit, mask_of, split_days
from bikechance_ml.features import coverage
from bikechance_ml.features.arrays import Bools, Features, Float64
from bikechance_ml.features.constants import FEATURE_SET, HORIZONS_MIN
from bikechance_ml.features.fingerprint import Fingerprint
from bikechance_ml.io.supabase import SupabaseIo, open_storage
from bikechance_ml.jobs import climate
from bikechance_ml.jobs.cards import LABEL_PATTERN, card_reference, expand
from bikechance_ml.jobs.fit_baseline import DEFAULT_TRAIN_DAYS
from bikechance_ml.jobs.window import (
    Window,
    day_reader,
    days_between,
    fingerprints,
    matrix_of,
    read_window,
    samples_of,
)
from bikechance_ml.models import artifact as lightgbm_artifact
from bikechance_ml.models import forest, matrix, registry
from bikechance_ml.models import gates as gate_tables
from bikechance_ml.models.registry import LIGHTGBM_KIND

#: 版の付け方。**最後の学習日**を入れる（いつまでのデータで作ったかが名前で分かる）。
VERSION_PREFIX: Final[str] = "lgbm-v1"

#: 既定の候補（設計どおりの 300 本・0.05。v0 と同じ森）。
DEFAULT_CANDIDATES: Final[str] = "300:0.05"

#: ハイパーパラメータ（開発プラン §7.2 の初期値）。**学習率は候補が持つ**（契約 37）。
#: **乱数の種を固定する**（§7.5）。
PARAMS: Final[Mapping[str, object]] = {
    "objective": "binary",
    "num_leaves": 127,
    "min_data_in_leaf": 1000,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 10.0,
    "seed": 20260910,
    "deterministic": True,
    "force_row_wise": True,
    "verbose": -1,
}


#: 平たくした森と `Booster.predict` が「同じ」とみなせる幅（確率の絶対差）。
#:
#: **倍精度の足し算の順が違うぶん**（300 本の葉の値を numpy は対で、LightGBM は順に
#: 足す）は 1e-13 くらい。**枝を 1 つ間違えたときの差**は 1e-1 くらい。その間に取る。
MAX_DISAGREEMENT: Final[float] = 1e-9

#: 照合に足す「実データが踏まない枝」の行数（全欠損・全 0・未知のカテゴリを各この数）。
STRESS_ROWS: Final[int] = 2_000

#: 未知のカテゴリとして渡す値。**語彙のどれよりも大きい**（ビット集合の外に落ちる）。
UNSEEN_CATEGORY: Final[float] = 9.0e4

#: ⑦ 1 行 1 木の秒を測る行（系統ごとの上限）と回数。**最小を取る**——機械のぶれは
#: 遅い側にしか出ない（`models/forest.py` の `BLOCK_ROWS` を決めたときと同じ作法）。
TIMING_ROWS: Final[int] = 50_000
TIMING_REPEATS: Final[int] = 3


class ForestMismatchError(RuntimeError):
    """平たくした森が `Booster.predict` と違う値を出した。**成果物を書かない。**"""


class OptionError(ValueError):
    """引数の組み合わせが通らない。**当てはめる前に止める**（28 日を回してから捨てない）。"""


class MissingLastDayError(RuntimeError):
    """`--to` の日の学習サンプルが無い。**検証日が 1 日ずれた窓で、黙って当てはめない。**"""


# ── 指定 ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class Outputs:
    """手元に書くもの（`{version}` は版の名前に置き換える）。"""

    out: str | None
    gates: str | None
    report: str | None
    card: str | None


@dataclass(frozen=True)
class Request:
    """コマンドの指定。**組み合わせを確かめ終えたもの**（`request_of`）。"""

    days: tuple[date, ...]
    eval_days: int
    purge_days: int
    candidates: tuple[Candidate, ...]
    #: 置く・登録する・カードと門の表を書く候補。**候補が 1 つなら、それ**
    chosen: Candidate | None
    label: str | None
    local: Path | None
    outputs: Outputs
    upload: bool
    register: bool
    from_profiles: bool
    allow_mixed_weather: bool


def request_of(options: argparse.Namespace) -> Request:
    """指定を確かめて `Request` にする。**通らない組み合わせは、ここで止める。**"""
    wanted = candidates.parse_candidates(options.candidates)
    chosen = chosen_of(wanted, options.choose)
    refuse_unwritable(options, chosen)
    return Request(
        days=_days_of(options),
        eval_days=options.eval_days,
        purge_days=options.purge_days,
        candidates=wanted,
        chosen=chosen,
        label=options.label,
        local=Path(options.local) if options.local else None,
        outputs=Outputs(options.out, options.gates, options.report, options.card),
        upload=options.upload,
        register=options.register,
        from_profiles=not options.no_profile,
        allow_mixed_weather=options.allow_mixed_weather,
    )


def _days_of(options: argparse.Namespace) -> tuple[date, ...]:
    return window_days(
        options.start, options.end, options.train_days, options.eval_days, options.purge_days
    )


def chosen_of(wanted: Sequence[Candidate], choose: str | None) -> Candidate | None:
    """置く候補。**`--choose` が無ければ、候補が 1 つのときだけそれ**（2 つ以上は人が選ぶ）。"""
    if choose is None:
        return wanted[0] if len(wanted) == 1 else None
    one = candidates.parse_one(choose)
    if one not in wanted:
        raise OptionError(f"--choose {choose} が --candidates の中にありません")
    return one


def refuse_unwritable(options: argparse.Namespace, chosen: Candidate | None) -> None:
    """**置く・登録するときの決まり。** 置いていない成果物と、カードの無い版は登録しない。"""
    if options.register and not options.upload:
        raise OptionError(
            "--register は --upload と一緒に使います（置いていない成果物を登録しない）"
        )
    if options.register and not options.card:
        raise OptionError("--register にはモデルカード（--card）が要ります（CLAUDE.md §6）")
    if options.upload and chosen is None:
        raise OptionError("候補が 2 つ以上あるときは、置く候補を --choose で選びます")
    if options.label is not None and LABEL_PATTERN.match(options.label) is None:
        raise OptionError(
            f"--label は英小文字で始まる英小文字と数字（20 字まで）です: {options.label!r}"
        )


def window_days(
    start: str | None, end: str, train_days: int | None, eval_days: int, purge_days: int
) -> tuple[date, ...]:
    """読む暦日。**始まりは `--from` か `--train-days` のどちらか 1 つで決める。**

    `--train-days N` は「学習 N 日・パージ・検証」がちょうど `--to` で終わる窓
    （W6-14 は学習 28 日・パージ 1 日・検証 2 日）。両方あると片方が黙って無視される。
    """
    last = _day(end)
    first = _start(start, last, train_days, eval_days + purge_days)
    if first > last:
        raise OptionError(f"始まり（{first}）が終わり（{last}）より後です")
    return days_between(first, last)


def _start(start: str | None, last: date, train_days: int | None, rest: int) -> date:
    if start is not None and train_days is None:
        return _day(start)
    if start is None and train_days is not None and train_days >= 1:
        return last - timedelta(days=train_days + rest - 1)
    raise OptionError("窓の始まりは --from か --train-days（1 以上）のどちらか 1 つで決めます")


def _day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise OptionError(f"日付は YYYY-MM-DD で書きます: {text!r}") from error


def model_version_for(days: Sequence[date], label: str | None = None) -> str:
    """版の名前。**最後の学習日**を入れ、印があれば間に挟む（`lgbm-v1-rehearsal-20261005`）。"""
    marked = f"-{label}" if label else ""
    return f"{VERSION_PREFIX}{marked}-{days[-1]:%Y%m%d}"


def baseline_days(fit_days: Sequence[date]) -> tuple[date, ...]:
    """**門の相手の B3 を当てはめる日**（W6-10、契約 36）：学習窓の最後の 7 暦日。

    本番の B3（`fit_baseline`）は「昨日までの `DEFAULT_TRAIN_DAYS` 日」で当てはめる。
    **暦日で数えるのも同じ**で、読めなかった日はそのまま抜ける（日を足して埋めない）。
    """
    last = fit_days[-1]
    return tuple(day for day in fit_days if (last - day).days < DEFAULT_TRAIN_DAYS)


# ── 当てはめる ────────────────────────────────────────────────
def dataset_for(
    built: matrix.Matrix, samples: Samples, fit_mask: Bools, target: Target
) -> lgb.Dataset:
    """1 ターゲットぶんの `Dataset`。**候補の間で使い回す**（違うのは本数と学習率だけ）。

    重みは §6.2 の逆抽出確率（`samples.weight`）。**使い回しても作り直しても同じ森が出る**
    （2026-09-27 に 3 組の本数と学習率で確かめた：モデルの文字列が完全に一致）。候補ごとに
    作り直すと、28 日ぶんのビン分けを候補の数だけ払う。
    """
    return lgb.Dataset(
        built.values,
        label=samples.y(target)[fit_mask].astype(np.float64),
        weight=samples.weight[fit_mask].astype(np.float64),
        feature_name=list(built.columns),
        categorical_feature=list(matrix.categorical_indices()),
        free_raw_data=False,
    )


def params_for(candidate: Candidate, target: Target) -> dict[str, object]:
    """候補とターゲットのパラメータ。**単調制約**は当てる側の台数に対して非減少（§7.2）。"""
    return {
        **PARAMS,
        "learning_rate": candidate.learning_rate,
        "monotone_constraints": list(matrix.monotone_constraints(target.name)),
    }


def train_one(dataset: lgb.Dataset, target: Target, candidate: Candidate) -> lgb.Booster:
    """1 ターゲット × 1 候補を当てはめる。**本数は固定**（早期終了を使わない理由は冒頭）。"""
    return lgb.train(params_for(candidate, target), dataset, num_boost_round=candidate.trees)


@dataclass(frozen=True)
class Checked:
    """照合の結果。**モデルカードと `model_versions.metrics` に残す。**"""

    n_rows: int
    max_gap: float


@dataclass(frozen=True)
class Grown:
    """1 ターゲット × 1 候補の森。**木（`Booster`）は捨て、要るものだけ持って帰る。**"""

    flat: forest.Forest
    check: Checked
    importance: tuple[Importance, ...]


def grow(
    dataset: lgb.Dataset, target: Target, candidate: Candidate, blocks: Sequence[Features]
) -> Grown:
    """育てて平たくし、**`Booster.predict` と照合してから**返す。重要度も木を捨てる前に取る。"""
    booster = train_one(dataset, target, candidate)
    flat, check = flatten_and_verify(booster, f"{candidate.name} / {target.name}", blocks)
    gains = np.asarray(booster.feature_importance(importance_type="gain"), dtype=np.float64)
    return Grown(flat, check, candidates.top_importance(booster.feature_name(), gains))


def fit_candidates(
    window: Window,
    samples: Samples,
    split: DaySplit,
    wanted: Sequence[Candidate],
    blocks: Sequence[Features],
) -> dict[str, dict[Candidate, Grown]]:
    """ターゲット → 候補 → 森。**学習の行列は 1 度だけ作る**（W5 プラン §12 の 168）。

    ターゲットで違うのはラベルと重みだけで、候補で違うのは本数と学習率だけである。
    `Dataset` はターゲットごとに 1 つ作り、そのターゲットの候補を育て終えたら捨てる。
    """
    fit_mask = mask_of(samples, split.fit)
    fitting = matrix_of(window, split.fit)
    return {
        target.name: _grow_target(fitting, samples, fit_mask, target, wanted, blocks)
        for target in TARGETS
    }


def _grow_target(
    fitting: matrix.Matrix,
    samples: Samples,
    fit_mask: Bools,
    target: Target,
    wanted: Sequence[Candidate],
    blocks: Sequence[Features],
) -> dict[Candidate, Grown]:
    dataset = dataset_for(fitting, samples, fit_mask, target)
    return {one: grow(dataset, target, one, blocks) for one in wanted}


# ── 平たくして照合する ────────────────────────────────────────
def flatten_and_verify(
    booster: lgb.Booster, name: str, blocks: Sequence[Features]
) -> tuple[forest.Forest, Checked]:
    """木を平たくし、**`Booster.predict` と同じ値が出ることを確かめてから**返す。"""
    built = forest.flatten(booster.dump_model())
    return built, refuse_if_different(booster, built, name, blocks)


def refuse_if_different(
    booster: lgb.Booster, built: forest.Forest, name: str, blocks: Sequence[Features]
) -> Checked:
    """**1 行でも違えば止める。** 「同じはず」を宣言で済ませない（W4-19）。

    ここを通らなかった森は成果物にならないので、**配信側と学習側がずれた状態の
    成果物は生まれ得ない**。差は必ず残す（0 でも記録する）。

    **ブロックのまま受け取って 1 つずつ照合する。** つなげてから渡すと
    `np.vstack` が**検証行ぶんの写し**を作る——28 日ぶんの当てはめでは、それだけで
    0.55 GB が増える（W5 プラン §12 の 168）。**見ている行は同じ**で、
    最大の差はブロックごとの最大の最大である。
    """
    gap = 0.0
    counted = 0
    for values in blocks:
        theirs = np.asarray(booster.predict(values), dtype=np.float64)
        ours = forest.probability(built, values)
        gap = max(gap, float(np.abs(theirs - ours).max()))
        counted += len(values)
    print(f"{name}: {counted:,} 行で照合、最大の差 {gap:.3e}", file=sys.stderr)
    if gap > MAX_DISAGREEMENT:
        raise ForestMismatchError(
            f"{name}: 平たくした森が Booster.predict と最大 {gap:.3e} 違います"
            f"（許容 {MAX_DISAGREEMENT:.0e}）。成果物は書きません"
        )
    return Checked(n_rows=counted, max_gap=gap)


def verification_blocks(values: Features) -> tuple[Features, ...]:
    """照合に使う行。**実データに、実データが踏まない枝を足す。**

    検証日の行だけでは「未知のカテゴリ」「全部欠損」「ちょうど 0」に当たる枝を
    一度も通らないことがある。**配信で最初に踏むのがその枝**では困るので、
    ここで作って足す。

    ~~1 つにつなげて返す~~ → **並びのまま返す**（2026-09-19）。つなげると実データぶんの
    写しができ、**削りたいものをそこで作ってしまう**（§12 の 168）。
    """
    sample = values[:STRESS_ROWS]
    return (values, _all_missing(sample), _all_zero(sample), _unseen(sample))


def _all_missing(sample: Features) -> Features:
    """全列が欠損。**`default_left` の枝**をすべて通す。"""
    return np.full_like(sample, np.nan)


def _all_zero(sample: Features) -> Features:
    """全列が 0。**`missing_type = Zero` の枝**を通す。"""
    return np.zeros_like(sample)


def _unseen(sample: Features) -> Features:
    """カテゴリ列だけ知らない値に差し替える。**ビット集合の外**へ落とす。"""
    changed = sample.copy()
    changed[:, list(matrix.categorical_indices())] = UNSEEN_CATEGORY
    return changed


def predict_on(forests: Mapping[str, forest.Forest], built: matrix.Matrix) -> dict[str, Float64]:
    """検証期間の行に予測を付ける。**行の並びは `samples.take(mask)` と同じ。**

    **配る側と同じ経路**（`models/forest.py`）で出す。`Booster.predict` の値ではない
    ので、表に載る数字は**実際に配信で出る数字**である。
    """
    return {name: lightgbm_artifact.predict(one, built) for name, one in forests.items()}


# ── ⑦ 1 行 1 木の秒 ──────────────────────────────────────────
def row_tree_seconds(
    forests: Mapping[str, forest.Forest], values: Features, evaluated: Samples
) -> dict[str, float]:
    """**当てはめた機械で** 1 行 1 木を歩く秒（系統ごと）。検証の行が無い系統は出さない。

    2 ターゲットの森をまとめて歩いた最小を、行 × 木（2 ターゲットの和）で割る。
    """
    trees = sum(len(one) for one in forests.values())
    picked = {
        name: np.flatnonzero(evaluated.system == index)[:TIMING_ROWS]
        for index, name in enumerate(evaluated.systems)
    }
    return {
        name: _fastest(forests, values[rows]) / (rows.size * trees)
        for name, rows in picked.items()
        if rows.size > 0 and trees > 0
    }


def _fastest(forests: Mapping[str, forest.Forest], block: Features) -> float:
    return min(_walk_seconds(forests, block) for _ in range(TIMING_REPEATS))


def _walk_seconds(forests: Mapping[str, forest.Forest], block: Features) -> float:
    started = time.perf_counter()
    for one in forests.values():
        forest.probability(one, block)
    return time.perf_counter() - started


# ── まとめる ──────────────────────────────────────────────────
@dataclass(frozen=True)
class CandidateFit:
    """1 つの候補の当てはめ。**森・照合・素性を一緒に運ぶ。**"""

    candidate: Candidate
    forests: Mapping[str, forest.Forest]
    checks: Mapping[str, Checked]
    facts: ForestFacts


def assemble(
    candidate: Candidate,
    grown: Mapping[str, Mapping[Candidate, Grown]],
    values: Features,
    evaluated: Samples,
) -> CandidateFit:
    """ターゲット → 候補 → 森 から、1 つの候補ぶんを取り出して素性を足す。"""
    mine = {name: by_candidate[candidate] for name, by_candidate in grown.items()}
    forests = {name: one.flat for name, one in mine.items()}
    facts = ForestFacts(
        trees={name: len(one) for name, one in forests.items()},
        nodes={name: one.n_nodes for name, one in forests.items()},
        depth={name: one.max_depth for name, one in forests.items()},
        row_tree_seconds=row_tree_seconds(forests, values, evaluated),
        importance={name: one.importance for name, one in mine.items()},
    )
    return CandidateFit(candidate, forests, {name: one.check for name, one in mine.items()}, facts)


@dataclass(frozen=True)
class Fitted:
    """当てはめの結果ひとそろい。**一緒に旅するものを 1 つにまとめる。**

    指標（`outcome`・`summaries`）・森の照合（`fits`）・入力の素性（`weather`・`data`）は、
    **登録簿にもモデルカードにも評価の表にも**入る。別々の引数で配ると、足したときに
    片方だけに入る——`inference_log` で実際にそうなった（W4 プラン §8.5.8）。
    """

    #: 候補 → 当てはめ。**並びは `--candidates` に書いた順**
    fits: Mapping[Candidate, CandidateFit]
    outcome: harness.Outcome
    summaries: tuple[Summary, ...]
    #: 読んだ日ごとの天気の被覆（**パージ日も含む**。役割は `outcome.split` が持つ）
    weather: Mapping[date, coverage.Coverage]
    #: 読んだ日ごとのバイト列の素性（W6-17。**パージ日も含む**）
    data: Mapping[date, Fingerprint]

    def summary_of(self, candidate: Candidate) -> Summary:
        return next(one for one in self.summaries if one.candidate == candidate)


def fit_and_score(
    window: Window,
    samples: Samples,
    split: DaySplit,
    climate_source: climatology.Source,
    wanted: Sequence[Candidate],
) -> Fitted:
    """候補を当てはめて平たくし、**B3 と同じ検証行の上で**測り、候補ごとに門を当てる。

    `climate_source` は **B2 の作り方**で、**既定値を置かない**（W5 プラン §12 の 166）。
    門の相手の B3 は学習窓の最後の 7 暦日で当てはめる（W6-10、契約 36。`baseline_days`）。
    """
    built = matrix_of(window, split.evaluate)
    grown = fit_candidates(window, samples, split, wanted, verification_blocks(built.values))
    evaluated = samples.take(mask_of(samples, split.evaluate))
    fits = {one: assemble(one, grown, built.values, evaluated) for one in wanted}
    extra = {one.name: predict_on(fit.forests, built) for one, fit in fits.items()}
    days = baseline_days(split.fit)
    outcome = harness.run(samples, split, extra, climate=climate_source, baseline_days=days)
    summaries = tuple(
        candidates.summarize(one, outcome, evaluated, split.evaluate) for one in wanted
    )
    return Fitted(fits, outcome, summaries, window.weather, fingerprints(window))


# ── 選んだ候補を書き出せる形にする ─────────────────────────────
@dataclass(frozen=True)
class Chosen:
    """置く候補の書き出すものひとそろい。**成果物と門の表は組**（表が森の SHA-256 を持つ）。"""

    candidate: Candidate
    artifact: lightgbm_artifact.LightGbmArtifact
    body: bytes
    gates: gate_tables.GateTable
    gates_body: bytes


def choose(fitted: Fitted, candidate: Candidate, version: str) -> Chosen:
    """選んだ候補の成果物と門の表。**表は、この森のバイト列の SHA-256 を焼き付ける。**"""
    split = fitted.outcome.split
    artifact = build_artifact(fitted.fits[candidate], split.fit, version)
    body = lightgbm_artifact.to_bytes(artifact)
    table = gate_tables.from_judged(
        fitted.summary_of(candidate).judged,
        version,
        hashlib.sha256(body).hexdigest(),
        [day.isoformat() for day in split.evaluate],
    )
    return Chosen(candidate, artifact, body, table, gate_tables.to_bytes(table))


def build_artifact(
    fit: CandidateFit, days: Sequence[date], version: str
) -> lightgbm_artifact.LightGbmArtifact:
    one = fit.candidate
    return lightgbm_artifact.build(
        forests=fit.forests,
        model_version=version,
        feature_set=FEATURE_SET,
        created_at=datetime.now(UTC).isoformat(),
        train_days=[day.isoformat() for day in days],
        horizons_min=HORIZONS_MIN,
        params={**PARAMS, "learning_rate": one.learning_rate, "num_boost_round": one.trees},
    )


# ── 登録簿に残すもの ──────────────────────────────────────────
def to_metrics(fitted: Fitted, chosen: Chosen) -> dict[str, object]:
    """`model_versions.metrics` に入れる要約（v1）。**門・配る形・校正・意思決定・素性。**

    **照合の結果も残す**（W4-19）。配信は木を numpy で歩くので、「その森が
    `Booster.predict` と同じ値を出すことを何行で確かめたか」は登録簿に残すべき事実である。
    **気候値の作り方・天気の被覆・データの SHA-256 も残す**——`feature_set` は列が在る
    ことしか語らない（W5 プラン §12 の 166、W4 プラン §8.5.3、W6-17）。
    """
    summary = fitted.summary_of(chosen.candidate)
    return {
        **_basics(fitted, chosen.candidate),
        "gates": _to_gate_rows(summary, chosen),
        "served": _to_served_rows(summary.judged),
        "ece": _to_ece_rows(summary.ece),
        "decision": _to_decision_rows(summary.decisions),
        "forest_check": _to_check_rows(fitted.fits[chosen.candidate].checks),
        "weather": _to_weather_rows(fitted),
        "data": _to_data_rows(fitted),
    }


def _basics(fitted: Fitted, candidate: Candidate) -> dict[str, object]:
    outcome = fitted.outcome
    return {
        "split": outcome.split.describe(),
        "n_fit": outcome.n_fit,
        "n_eval": outcome.n_eval,
        "feature_set": FEATURE_SET,
        "candidate": {"trees": candidate.trees, "learning_rate": candidate.learning_rate},
        "num_boost_round": candidate.trees,
        "brier_weighted": _to_brier_rows(outcome, candidate.name),
        # **何を相手に測ったかを残す**（W5 プラン §12 の 166、W6-10）
        "climate": outcome.climate,
        "baseline": {
            "days": [day.isoformat() for day in outcome.baseline_days],
            "n_fit": outcome.n_baseline_fit,
        },
    }


def _to_brier_rows(outcome: harness.Outcome, model: str) -> dict[str, object]:
    """system × ターゲットの重み付き Brier を、**B3 と並べて**残す（門を当てる前の単体）。"""
    return {
        f"{one.slice.system}/{one.slice.target}": {
            "n": one.n,
            "b3": round(one.weighted[gates.REFERENCE].brier, 6),
            "lgbm": round(one.weighted[model].brier, 6),
        }
        for one in outcome.overall
    }


def _to_gate_rows(summary: Summary, chosen: Chosen) -> dict[str, object]:
    """門の表の場所と要約。**表の SHA-256 と、組になる森の SHA-256 も残す。**"""
    judged = summary.judged
    return {
        "path": gate_tables.gates_path(chosen.gates.model_version),
        "sha256": hashlib.sha256(chosen.gates_body).hexdigest(),
        "artifact_sha256": chosen.gates.artifact_sha256,
        "rule": dict(chosen.gates.rule),
        "cells": _counts(judged, gates.CELL_GATE),
        "horizons": _counts(judged, gates.HORIZON_GATE),
    }


def _counts(judged: gates.Judged, unit: str) -> dict[str, int]:
    scored = judged.cells if unit == gates.CELL_GATE else judged.horizons
    return {
        "passed": len(judged.passing(unit)),
        "judgeable": len(judged.judgeable(unit)),
        "total": len(scored),
    }


def _to_served_rows(judged: gates.Judged) -> dict[str, object]:
    """配る形の改善と森を歩く割合。**検証日ぜんぶ**と**1 日目で選び残りで測る**を並べる。"""
    return {
        "whole": {unit: _served_json(one) for unit, one in judged.served.items()},
        "holdout": {unit: _served_json(one) for unit, one in judged.holdout.items()},
    }


def _served_json(one: gates.Served) -> dict[str, float]:
    return {"improvement": round(one.improvement, 6), "forest_share": round(one.forest_share, 6)}


def _to_ece_rows(ece: EceSummary) -> dict[str, object]:
    return {
        "max_allowed": candidates.MAX_ECE,
        "passes": ece.passes,
        "horizon_max": round(ece.horizon, 6),
        "horizon_where": ece.horizon_where,
        "bucket": {label: round(value, 6) for label, value in ece.bucket.items()},
        "dow_type_max": round(ece.dow_type, 6),
        "dow_type_where": ece.dow_type_where,
    }


def _to_decision_rows(decisions: Sequence[candidates.GroupDecision]) -> dict[str, object]:
    """意思決定の指標（W6-16）。**いまの B3 と、セル単位の門で配る形**を並べる。"""
    return {
        f"{one.system}/{one.target}": {
            "b3": _decision_json(one.reference),
            "served": _decision_json(one.served),
        }
        for one in decisions
    }


def _decision_json(one: metrics.Decision) -> dict[str, object]:
    return {
        "threshold": metrics.PROMISE_MIN,
        "precision": _rounded(one.precision),
        "coverage": round(one.coverage, 6),
        "bands": [
            {
                "name": band.name,
                "low": band.low,
                "share": round(band.share, 6),
                "realized": _rounded(band.realized),
            }
            for band in one.bands
        ],
    }


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def _to_check_rows(checks: Mapping[str, Checked]) -> dict[str, object]:
    """照合の結果を JSON にする。**許容も一緒に残す**（後から読む人が判断できるように）。"""
    return {
        "tolerance": MAX_DISAGREEMENT,
        "targets": {
            name: {"n_rows": one.n_rows, "max_gap": one.max_gap} for name, one in checks.items()
        },
    }


def _to_weather_rows(fitted: Fitted) -> dict[str, object]:
    """天気の被覆を JSON にする。**許容も一緒に残す**（`_to_check_rows` と同じ作法）。

    **「明示して通したか」という別の印は持たない。** `spread_pp` が `max_spread_pp`
    を超えていれば、それが「通した」ということである。**印を別に持つと、印だけ
    書き換えられる**（値そのものが証拠であるほうがよい）。
    """
    used = coverage.restrict(fitted.weather, fitted.outcome.split.used())
    return {
        "max_spread_pp": coverage.MAX_SPREAD_PP,
        "spread_pp": round(coverage.spread_pp(used.values()), 4),
        "by_day": {day.isoformat(): one.as_dict() for day, one in sorted(fitted.weather.items())},
    }


def _to_data_rows(fitted: Fitted) -> dict[str, object]:
    """読んだ日ごとのバイト列の SHA-256 と行数（W6-17）。**パージ日も含む**（読んだものぜんぶ）。"""
    return {
        "by_day": {
            day.isoformat(): {"rows": one.rows, "sha256": one.sha256}
            for day, one in sorted(fitted.data.items())
        }
    }


def to_registration(fitted: Fitted, chosen: Chosen, card_path: str | None) -> dict[str, object]:
    """`register_model_version` に渡す形。**`candidate` としてしか登録できない。**"""
    artifact = chosen.artifact
    return {
        "model_version": artifact.model_version,
        "kind": LIGHTGBM_KIND,
        "status": "candidate",
        "feature_set": artifact.feature_set,
        "artifact_path": lightgbm_artifact.artifact_path(artifact.model_version),
        "train_days": list(artifact.train_days),
        "metrics": to_metrics(fitted, chosen),
        "card_path": card_reference(card_path),
        "note": f"W6 の PR F。候補 {chosen.candidate.spec}。単体では配らない（契約 33）",
    }


# ── 実行 ──────────────────────────────────────────────────────
def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LightGBM v1 の候補を当てはめる（W6 の PR F）")
    _add_window(parser)
    _add_candidates(parser)
    _add_outputs(parser)
    _add_escapes(parser)
    return parser.parse_args(argv)


def _add_window(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from", dest="start", default=None, help="窓の最初の日（JST。含む）")
    parser.add_argument("--to", dest="end", required=True, help="窓の最後の日（JST。検証の最後）")
    parser.add_argument(
        "--train-days",
        type=int,
        default=None,
        help="学習の日数（--to から逆に数える。--from とはどちらか一方。W6-14 は 28）",
    )
    parser.add_argument("--eval-days", type=int, default=1, help="検証に使う日数（W6-14 は 2）")
    parser.add_argument("--purge-days", type=int, default=1, help="学習と検証の間に空ける日数")
    parser.add_argument("--local", default=None, help="Storage の代わりに読む場所")


def _add_candidates(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--candidates",
        default=DEFAULT_CANDIDATES,
        help="本数:学習率 をカンマで並べる（例 50:0.15,100:0.15,300:0.05。契約 37）",
    )
    parser.add_argument(
        "--choose", default=None, help="置く候補（本数:学習率）。候補が 2 つ以上なら置くのに要る"
    )
    parser.add_argument("--label", default=None, help="版の名前に挟む印（予行演習は rehearsal）")


def _add_outputs(parser: argparse.ArgumentParser) -> None:
    hint = "（{version} は版の名前に置き換わる）"
    parser.add_argument("--out", default=None, help=f"成果物の書き出し先{hint}")
    parser.add_argument("--gates", default=None, help=f"門の表の書き出し先{hint}")
    parser.add_argument("--report", default=None, help=f"報告書（Markdown）の出力先{hint}")
    parser.add_argument("--card", default=None, help=f"モデルカードの出力先{hint}")
    parser.add_argument("--upload", action="store_true", help=registry.UPLOAD_HELP)
    parser.add_argument(
        "--register",
        action="store_true",
        help="model_versions に candidate で登録する（--upload と --card が要る。昇格はしない）",
    )


def _add_escapes(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-profile",
        action="store_true",
        help="気候値を学習サンプルの行から作る（**2026-09-19 より前の記録と比べるとき**だけ）",
    )
    parser.add_argument(
        "--allow-mixed-weather",
        action="store_true",
        help="天気の被覆が日で違っても当てはめる（**登録簿に残る**。W4 プラン §8.5.3）",
    )


def refuse_mixed_weather(
    weather: Mapping[date, coverage.Coverage], split: DaySplit, *, allowed: bool
) -> None:
    """**被覆の違う日が混ざっていたら、当てはめる前に止める。**

    止めるのは当てはめの**前**である（5 分かけてから捨てない）。数えるのは学習日と
    検証日だけで、**パージ日は読むが捨てる**ので入れない（`DaySplit.used`）。

    `--allow-mixed-weather` で通せるが、**通した事実は消えない**：
    `model_versions.metrics.weather` に `spread_pp` と `max_spread_pp` が並ぶ。
    """
    used = coverage.restrict(weather, split.used())
    if not allowed:
        coverage.refuse_if_mixed(used)
        return
    print(f"天気の被覆が違うまま当てはめます: {coverage.describe(used)}", file=sys.stderr)


def prepare(
    window: Window, *, eval_days: int, purge_days: int, allow_mixed_weather: bool
) -> tuple[Samples, DaySplit]:
    """分割を決め、**当てはめの前に天気の門を通す**（PR I）。

    門を `run` の中に直書きしないのは、**呼び出し側が呼ぶのをやめても気づけない**
    からである（PR E′ で 1 度そうなった）。ここを通す限り、検査は本物の経路を見る。

    **開く順も意味を持つ。** 分割 → 門 → サンプルの順にするのは、止めるなら
    行を整数に直す前に止めたいため。**窓は表を持たない**ので、ここで止まるときは
    **表を 1 つも開いていない**。
    """
    split = split_days(window.days, eval_days, purge_days)
    print(f"天気の被覆: {coverage.describe(window.weather)}", file=sys.stderr)
    refuse_mixed_weather(window.weather, split, allowed=allow_mixed_weather)
    samples = samples_of(window)
    print(f"{split.describe()} / 全 {len(samples):,} 行", file=sys.stderr)
    return samples, split


@dataclass(frozen=True)
class Made:
    """1 回の当てはめで作ったもの。**選んでいなければ `chosen` は None**（並べるだけ）。"""

    fitted: Fitted
    version: str
    chosen: Chosen | None


def run(argv: Sequence[str] | None = None) -> int:
    try:
        request = request_of(_arguments(argv))
    # **黙って既定に落ちない。** 通らない指定のまま 28 日を回してから捨てない
    except (OptionError, CandidateError) as error:
        print(f"指定が違います: {error}", file=sys.stderr)
        return 2
    with open_storage(read_storage_config()) as source:
        made = make(source, request)
        # **登録より先に手元へ書く**——カードの無い版を登録しない（CLAUDE.md §6）
        write_outputs(request.outputs, made)
        publish(source, request, made)
    print(_describe(made))
    return 0


def make(source: SupabaseIo, request: Request) -> Made:
    """読んで、門を通し、当てはめ、選んだ候補を書き出せる形にする。"""
    window = _load(source, request)
    samples, split = prepare(
        window,
        eval_days=request.eval_days,
        purge_days=request.purge_days,
        allow_mixed_weather=request.allow_mixed_weather,
    )
    version = model_version_for(split.fit, request.label)
    if request.upload:
        # **置けない名前なら当てはめる前に止める**（W6-19、契約 38）。名前は学習日と印で決まる
        registry.refuse_serving(source, version)
    chosen_climate = _climate(source, request, split, samples)
    fitted = fit_and_score(window, samples, split, chosen_climate, request.candidates)
    picked = None if request.chosen is None else choose(fitted, request.chosen, version)
    return Made(fitted=fitted, version=version, chosen=picked)


def _load(source: SupabaseIo, request: Request) -> Window:
    """窓を開く。**表は作らない**——日ごとの中身と素性だけを持つ（`jobs/window.py`）。"""
    opened = read_window(day_reader(None if request.local else source, request.local), request.days)
    refuse_missing_end(opened, request.days)
    return opened


def refuse_missing_end(opened: Window, days: Sequence[date]) -> None:
    """**`--to` の日が無ければ止める。** 無い日を飛ばすと、検証日が 1 日前にずれる。

    ずれたまま当てはめると、学習の最終日も版の名前も 1 日ずれ、**短い窓の v1 が本物の
    名前で置かれる**（10/9 の朝は、10-08 の学習サンプルができてから回す。W6 プラン §8.7）。
    途中の日が無いのは飛ばす（収集の欠損は補わない。CLAUDE.md §6）が、言う。
    """
    missing = sorted(set(days) - set(opened.days))
    if missing:
        listed = "、".join(day.isoformat() for day in missing)
        print(f"学習サンプルが無い日（飛ばす。補わない）: {listed}", file=sys.stderr)
    if days[-1] not in opened.days:
        raise MissingLastDayError(
            f"--to の日（{days[-1]}）の学習サンプルがありません。検証日がずれるので当てはめません"
        )


def _climate(
    source: SupabaseIo, request: Request, split: DaySplit, samples: Samples
) -> climatology.Source:
    """**B2 は本番と同じ作り方**（`jobs/climate.py` の 1 か所を通す）。

    読むのは**門の相手の B3 を当てはめる日だけ**（W6-10）：学習の最終日の版の
    プロファイルと、その日たちの日次。検証日の版を渡すと、B2 が検証日の観測を見た
    状態で測ることになる（`jobs/climate.py`）。
    """
    chosen = climate.source_for(
        None if request.local else source,
        baseline_days(split.fit),
        request.local,
        samples.ports,
        from_profiles=request.from_profiles,
    )
    print(f"気候値（B2）の作り方: {chosen.describe()}", file=sys.stderr)
    return chosen


def write_outputs(outputs: Outputs, made: Made) -> None:
    """手元に書く。**選んでいなければ、報告書だけ**（成果物・門の表・カードは候補ごとのもの）。"""
    _write_text(expand(outputs.report, made.version), render_report(made), "報告書")
    if made.chosen is None:
        if any((outputs.out, outputs.gates, outputs.card)):
            print("置く候補を選んでいないので、成果物・門の表・カードは書きません", file=sys.stderr)
        return
    _write_bytes(expand(outputs.out, made.version), made.chosen.body, "成果物")
    _write_bytes(expand(outputs.gates, made.version), made.chosen.gates_body, "門の表")
    card = render_card(made.fitted, made.chosen)
    _write_text(expand(outputs.card, made.version), card, "モデルカード")


def publish(source: SupabaseIo, request: Request, made: Made) -> None:
    """置いて登録する。**置く直前にもう 1 度登録簿を引く**（`registry.upload_artifact`）。

    **森 → 門の表 → 登録の順。** 門の表で落ちても、前の表は前の森の SHA-256 を持つので、
    **新しい森と組にはならない**（`models/gates.refuse_other_forest`）。登録は両方を
    置けてから。DB の側も配信中の版を登録し直させない（0038）。
    """
    chosen = made.chosen
    if chosen is None or not request.upload:
        return
    _put(source, made.version, chosen)
    if request.register:
        card = expand(request.outputs.card, made.version)
        source.register_model_version(to_registration(made.fitted, chosen, card))


def _put(source: SupabaseIo, version: str, chosen: Chosen) -> None:
    """森、門の表の順に置く。**どちらも置く直前に登録簿を引く**（配信中の名前では置かない）。"""
    pieces = (
        (lightgbm_artifact.artifact_path(version), chosen.body, lightgbm_artifact.CONTENT_TYPE),
        (gate_tables.gates_path(version), chosen.gates_body, gate_tables.CONTENT_TYPE),
    )
    for path, body, content_type in pieces:
        registry.upload_artifact(source, version, path, body, content_type)


def _write_text(path: str | None, text: str, label: str) -> None:
    if path is not None:
        _write_bytes(path, text.encode(), label)


def _write_bytes(path: str | None, body: bytes, label: str) -> None:
    if path is None:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    print(f"{label}を書き出しました: {path}", file=sys.stderr)


def _describe(made: Made) -> str:
    if made.chosen is None:
        return (
            f"{made.version}：候補 {len(made.fitted.fits)} 個を並べました（置く候補は選んでいない）"
        )
    chosen = made.chosen
    passed = len(chosen.gates.lightgbm_cells())
    return (
        f"{chosen.artifact.describe()} / {len(chosen.body):,} バイト"
        f" / 門の表 {len(chosen.gates_body):,} バイト（LightGBM に回すセル {passed}）"
    )


# ── 報告書 ────────────────────────────────────────────────────
def render_report(made: Made) -> str:
    """§1〜§11（`eval/report.py`）の後ろに、**候補の比較の §12** を足す。"""
    fitted = made.fitted
    head = report.render_markdown(
        fitted.outcome, _title(made.version), _NOTE, "fit_lightgbm", weather=fitted.weather
    )
    facts = {one: fit.facts for one, fit in fitted.fits.items()}
    chosen = None if made.chosen is None else made.chosen.candidate
    tail = candidate_report.render(
        fitted.summaries, facts, chosen, fitted.data, fitted.outcome.split
    )
    return head + "\n".join(tail) + "\n"


def _title(version: str) -> str:
    return f"LightGBM v1 の候補（{version}）と B0〜B3"


_NOTE: Final[str] = (
    "> **v1 は LightGBM 単体では配らない**（契約 33）。配るのは門を当てた形（合成器。PR H）"
    "で、**候補の比較は §12 にある**。§3〜§11 の LightGBM の列は、**門を当てる前の単体**の"
    "成績である。\n>\n"
    "> **門の相手の B3 は本番と同じ作り方**（学習窓の最後の 7 暦日・学習の最終日の版の"
    "プロファイル。W6-10、契約 36）。学習窓が 7 日より長ければ、冒頭の「B0〜B3 の当てはめ」"
    "の行にその日が出る。\n>\n"
    "> **早期終了を使っていない**（本数は候補ごとに固定）。検証の行は門を決めるのに使うので、"
    "本数の選択にまで使わない。"
)


# ── モデルカード ──────────────────────────────────────────────
def render_card(fitted: Fitted, chosen: Chosen) -> str:
    """モデルカード（開発プラン §7.3）。**登録の前提**なので、登録する回は必ず作る。"""
    summary = fitted.summary_of(chosen.candidate)
    lines = [
        *_card_header(fitted, chosen),
        *_card_data(fitted),
        *_verification_lines(fitted.fits[chosen.candidate].checks),
        *_card_gates(summary, chosen),
        *_card_calibration(summary.ece),
        *_card_decisions(summary),
        *_card_overall(fitted.outcome, chosen.candidate.name),
        *_card_limits(fitted),
        *_card_usage(chosen.artifact.model_version),
    ]
    return "\n".join(lines) + "\n"


def _card_header(fitted: Fitted, chosen: Chosen) -> list[str]:
    artifact, outcome, one = chosen.artifact, fitted.outcome, chosen.candidate
    nodes = ", ".join(f"{name} {tree.n_nodes:,}" for name, tree in artifact.forests.items())
    return [
        f"# モデルカード：{artifact.model_version}",
        "",
        "| | |",
        "|---|---|",
        "| 種類 | LightGBM（`binary`、ターゲット別に 2 モデル） |",
        "| 状態 | `candidate`。**単体では配らない**（配るのは門を当てた合成器。W6-08、契約 33） |",
        f"| 候補 | {one.name}（本数 {one.trees}・学習率 {one.learning_rate:g}。早期終了なし） |",
        f"| 特徴量の版 | `{artifact.feature_set}` |",
        f"| 学習期間 | {_span(outcome.split.fit)}（{len(artifact.train_days)} 日） |",
        f"| 分割 | {outcome.split.describe()} |",
        f"| 件数 | 学習 {outcome.n_fit:,} 行 / 検証 {outcome.n_eval:,} 行 |",
        f"| 門の相手 | B3（{_span(outcome.baseline_days)}・{outcome.n_baseline_fit:,} 行。"
        "**本番と同じ作り方**。W6-10） |",
        f"| 列 | {len(artifact.columns)}（カテゴリ {len(artifact.categorical)}） |",
        f"| 成果物 | 木の構造そのもの（書式の版 {artifact.format_version}）。節 {nodes} |",
        f"| 門の表 | `{gate_tables.gates_path(artifact.model_version)}`"
        f"（森の SHA-256 `{chosen.gates.artifact_sha256[:12]}…` と組） |",
        f"| 作成 | {artifact.created_at} |",
        "",
    ]


def _span(days: Sequence[date]) -> str:
    return "—" if not days else f"{days[0]}〜{days[-1]}"


def _card_data(fitted: Fitted) -> list[str]:
    split = fitted.outcome.split
    return [
        "## 学習に使ったデータ",
        "",
        "**`feature_set` は「列が在る」しか語らない**（W4 プラン §8.5.3）。何割の行に"
        "天気が入っていたかを日ごとに出す。",
        "",
        *report.weather_block(fitted.weather, split),
        "",
        "**読んだバイト列の SHA-256**（W6-17）。`features/` は同じパスで作り直すことが"
        "あるので、パスだけでは何から当てはめたかが残らない。",
        "",
        *report.fingerprint_block(fitted.data, split),
        "",
    ]


def _verification_lines(checks: Mapping[str, Checked]) -> list[str]:
    """**配信の実装が学習と同じ値を出すことの確認**（W4-19）。カードに必ず載せる。

    配信は `lightgbm` を使わず木を numpy で歩くので（`models/forest.py`）、
    **「同じはず」ではなく「何行で確かめて、差がいくつだったか」**を残す。
    ここを通らなかった森は成果物にならない。
    """
    lines = [
        "## 配信の実装との一致（W4-19）",
        "",
        "**配信は `lightgbm` を読み込まない**（Vercel の Python ランタイムに OpenMP が無いため。"
        "W4 プラン §12 の 126）。木を numpy で歩くので、**当てはめた直後に "
        "`Booster.predict` と突き合わせている**。検証日の全行に、実データが踏まない枝"
        f"（全欠損・全 0・未知のカテゴリを各 {STRESS_ROWS:,} 行）を足した集合で測った。",
        "",
        "| ターゲット | 照合した行 | 最大の差 | 許容 |",
        "|---|---:|---:|---:|",
    ]
    lines.extend(
        f"| {name} | {one.n_rows:,} | {one.max_gap:.3e} | {MAX_DISAGREEMENT:.0e} |"
        for name, one in checks.items()
    )
    lines.extend(["", "**超えていれば成果物を書かない。** このカードがある＝通っている。", ""])
    return lines


def _card_gates(summary: Summary, chosen: Chosen) -> list[str]:
    judged = summary.judged
    whole = judged.served[gates.CELL_GATE]
    held = judged.holdout.get(gates.CELL_GATE)
    return [
        "## 門（セル単位。開発プラン §7.1、契約 35）",
        "",
        "**LightGBM を配るのは、門の相手（B3）を 10% 以上改善したセルだけ**で、判定の対象外"
        "（B0 の Brier が 0.001 未満）と届かないセルは B3 を配る。門は検証日ぜんぶで決め、"
        "表は森の隣に置いた（合成器がそのまま焼き付ける）。",
        "",
        f"- **越えたセル**：{len(judged.passing(gates.CELL_GATE))}"
        f"（判定の対象 {len(judged.judgeable(gates.CELL_GATE))} / 全 {len(judged.cells)}）",
        f"- **配る形の改善**（B3 比、4 組の平均）：検証日ぜんぶで {whole.improvement:+.2%}"
        f"・1 日目で選び残りで測ると {'—' if held is None else f'{held.improvement:+.2%}'}",
        f"- **森を歩く行**（費用の重み付き）：{whole.forest_share:.1%}",
        f"- **門の表**：`{gate_tables.gates_path(chosen.gates.model_version)}`"
        f"（LightGBM に回すセル {len(chosen.gates.lightgbm_cells())}）",
        "",
    ]


def _card_calibration(ece: EceSummary) -> list[str]:
    buckets = "・".join(f"{label} は {value:.4f}" for label, value in ece.bucket.items())
    verdict = "すべて基準未満" if ece.passes else "**基準を超えるところがある**（配る前に諮る）"
    return [
        "## 校正（W6-15）",
        "",
        f"**校正器は入れていない。** LightGBM そのものの ECE（等頻度 15、重み付き）が、"
        f"すべて {candidates.MAX_ECE} 未満なら配る。",
        "",
        f"- 水平別の最大：{ece.horizon:.4f}（{ece.horizon_where}）",
        f"- バケツ別（水平をまとめ、system × ターゲットのうち最大）：{buckets}",
        f"- 曜日種別の最大：{ece.dow_type:.4f}（{ece.dow_type_where}）",
        f"- **判定**：{verdict}",
        "",
    ]


def _card_decisions(summary: Summary) -> list[str]:
    return [
        "## 意思決定の指標（W6-16、開発プラン §7.3）",
        "",
        "**アプリの約束がどれだけ実現するか。** いまの B3 と、セル単位の門で配る形を並べる"
        "（3 段階は**割合 / 実現率**、重み付き）。",
        "",
        *candidate_report.decision_table(summary),
    ]


def _card_overall(outcome: harness.Outcome, model: str) -> list[str]:
    """**門を当てる前の単体**の全体の Brier。配る形の成績は「門」の節。"""
    lines = [
        "## 指標（全体、重み付き Brier。門を当てる前の LightGBM 単体）",
        "",
        "| system / ターゲット | n | B3 | LightGBM | 差 |",
        "|---|---:|---:|---:|---:|",
    ]
    for one in outcome.overall:
        b3 = one.weighted[gates.REFERENCE].brier
        lgbm = one.weighted[model].brier
        change = "—" if b3 == 0 else f"{(b3 - lgbm) / b3:+.1%}"
        lines.append(
            f"| {one.slice.system} / {one.slice.target} | {one.n:,} "
            f"| {b3:.5f} | {lgbm:.5f} | {change} |"
        )
    return [*lines, ""]


def _weather_limit(fitted: Fitted) -> str:
    """カードの「限界」に**測った数字**を書く。**日付を決め打ちにしない。**"""
    used = coverage.restrict(fitted.weather, fitted.outcome.split.used())
    spread = coverage.spread_pp(used.values())
    if spread > coverage.MAX_SPREAD_PP:
        return (
            f"- **天気の被覆が学習日と検証日で {spread:.2f} ポイント違う**"
            f"（許容 {coverage.MAX_SPREAD_PP}）。`--allow-mixed-weather` で通してある"
        )
    return f"- **天気の被覆は揃っている**（学習日と検証日の差 {spread:.2f} ポイント）"


def _card_limits(fitted: Fitted) -> list[str]:
    return [
        "## 既知の限界",
        "",
        _weather_limit(fitted),
        "- **検証日は平日だけ**（W6-14）。土日祝の当たり方は shadow の日で見る（W6-12）",
        "- **門は検証日で決めている**。同じ日で選んで測った改善は甘いので、"
        "「1 日目で選び残りで測る」を並べてある",
        "- **早期終了を使っていない**（本数は候補で固定。検証の行を門の決定に使うため）",
        "- **校正していない**（W6-15）。テスト期間が取れる W8 以降に確かめる",
        "- **3 折の時系列検証をしていない**（W6-14。W8 の週次で行う）",
        "- **`station_id` をカテゴリに入れていない**（21,000 カテゴリ。開発プラン §7.2）",
        "",
    ]


def _card_usage(version: str) -> list[str]:
    return [
        "## 使い方",
        "",
        "```bash",
        "# 試し打ち（**どこにも書かない**。LightGBM 単体は全行を歩く）",
        'curl -H "Authorization: Bearer $CRON_SECRET" \\',
        f"  'https://bike-chance.vercel.app/ml/infer/hellocycling?model={version}'",
        "```",
        "",
        "**LightGBM 単体を active にしない**（契約 33）。配るのは門を当てた合成器（PR H）。"
        "shadow に上げるのも、切り替えるのも**人が psql から** `promote_model_version()` を"
        "呼ぶ（CLAUDE.md §6、契約 39）。",
    ]


if __name__ == "__main__":
    raise SystemExit(run())
