"""LightGBM v0 を当てはめて登録する（W4 プラン §6.5、開発プラン §7.2）。

**ここは副作用の置き場所。** 行列の作り方は `models/matrix.py`、成果物は
`models/artifact.py`、評価は `eval/` にある（CLAUDE.md §3）。

**v0 の目的は配線の確認**である（W4-07）。学習が通り、成果物が読め、`/ml/infer` が
その版で確率を出せるところまでを見る。**精度は問わない**が、B3 と比べた数字は
同じ物差しで記録する（`eval/harness.py` に相乗りする）。

使い方（環境変数は `.env` から読み込んでから）:

    set -a; . ./.env; set +a
    cd apps/ml
    ./.venv/bin/python -m bikechance_ml.jobs.fit_lightgbm \\
        --from 2026-09-07 --to 2026-09-09 --eval-days 1 \\
        --local .cache/features --report ../../docs/260910_eda_03_lightgbm_v0.md \\
        --card ../../docs/model_cards/lgbm-v0-20260907.md --upload --register

**当てはめた木は numpy の形に平たくしてから配る**（PR E′）。配信のランタイムに
OpenMP が無く `import lightgbm` が落ちるため（§12 の 126）。**平たくした森が
`Booster.predict` と同じ値を出すことを、成果物を書く前に照合する**
（`refuse_if_different`）。1 つでも違えば書かない。

**天気の被覆が日によって違えば、当てはめる前に止まる**（PR I）。`feature_set` は
「列が在る」しか語らないので、同じ `v3` でも天気が 1 件も入っていない日がある
（W4 プラン §8.5.3）。承知のうえで混ぜるなら `--allow-mixed-weather` を付ける
——**付けた事実は `model_versions.metrics.weather` に残る。**

**早期終了は使わない。** 開発プラン §7.2 は「検証セットで 100 ラウンド」と書いているが、
それは 学習 → パージ → 検証 → テスト の 4 分割が取れる前提である。いまは完全な暦日が
3 日しか無く、検証を早期終了に使うと**その集合で B3 と比べる意味が消える**（モデル選択に
使った集合で測ることになる）。v0 は**本数を固定**し、4 分割が取れる 9/16 以降に見直す。
"""

import argparse
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final

import lightgbm as lgb
import numpy as np
import pyarrow as pa

from bikechance_ml.config import read_storage_config
from bikechance_ml.eval import harness, report
from bikechance_ml.eval.dataset import TARGETS, Samples, Target, to_samples
from bikechance_ml.eval.split import DaySplit, mask_of, split_days
from bikechance_ml.features import coverage
from bikechance_ml.features.arrays import Bools, Float64
from bikechance_ml.features.constants import FEATURE_SET, HORIZONS_MIN
from bikechance_ml.io.supabase import SupabaseIo, open_storage
from bikechance_ml.jobs.evaluate_baselines import Loaded, days_between, load_days
from bikechance_ml.models import artifact as lightgbm_artifact
from bikechance_ml.models import forest, matrix
from bikechance_ml.models.registry import LIGHTGBM_KIND, MODEL_BUCKET

#: 版の付け方。**最後の学習日**を入れる（いつまでのデータで作ったかが名前で分かる）。
VERSION_PREFIX: Final[str] = "lgbm-v0"

#: 表に出す名前。`eval/harness.py` の B0〜B3 と並ぶ。
MODEL_NAME: Final[str] = "LGBM"

#: 木の本数。**固定**（早期終了を使わない理由は冒頭）。
NUM_BOOST_ROUND: Final[int] = 300

#: ハイパーパラメータ（開発プラン §7.2 の初期値）。**乱数の種を固定する**（§7.5）。
PARAMS: Final[Mapping[str, object]] = {
    "objective": "binary",
    "learning_rate": 0.05,
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


class ForestMismatchError(RuntimeError):
    """平たくした森が `Booster.predict` と違う値を出した。**成果物を書かない。**"""


def model_version_for(days: Sequence[date]) -> str:
    return f"{VERSION_PREFIX}-{days[-1]:%Y%m%d}"


def train_one(table: pa.Table, samples: Samples, fit_mask: Bools, target: Target) -> lgb.Booster:
    """1 ターゲットぶんを当てはめる。**単調制約とカテゴリを渡す。**

    重みは §6.2 の逆抽出確率（`samples.weight`）。単調制約は「当てる対象と同じ側の
    台数に対して非減少」で、**異常な形を構造として防ぐ**（開発プラン §7.2）。
    """
    built = matrix.build(table.filter(pa.array(fit_mask)))
    dataset = lgb.Dataset(
        built.values,
        label=samples.y(target)[fit_mask].astype(np.float64),
        weight=samples.weight[fit_mask].astype(np.float64),
        feature_name=list(built.columns),
        categorical_feature=list(matrix.categorical_indices()),
        free_raw_data=False,
    )
    params = {
        **PARAMS,
        "monotone_constraints": list(matrix.monotone_constraints(target.name)),
    }
    return lgb.train(params, dataset, num_boost_round=NUM_BOOST_ROUND)


# ── 平たくして照合する ────────────────────────────────────────
@dataclass(frozen=True)
class Checked:
    """照合の結果。**モデルカードと `model_versions.metrics` に残す。**"""

    n_rows: int
    max_gap: float


def flatten_and_verify(
    booster: lgb.Booster, name: str, values: Float64
) -> tuple[forest.Forest, Checked]:
    """木を平たくし、**`Booster.predict` と同じ値が出ることを確かめてから**返す。"""
    built = forest.flatten(booster.dump_model())
    return built, refuse_if_different(booster, built, name, values)


def refuse_if_different(
    booster: lgb.Booster, built: forest.Forest, name: str, values: Float64
) -> Checked:
    """**1 行でも違えば止める。** 「同じはず」を宣言で済ませない（W4-19）。

    ここを通らなかった森は成果物にならないので、**配信側と学習側がずれた状態の
    成果物は生まれ得ない**。差は必ず残す（0 でも記録する）。
    """
    theirs = np.asarray(booster.predict(values), dtype=np.float64)
    ours = forest.probability(built, values)
    gap = float(np.abs(theirs - ours).max())
    print(f"{name}: {len(values):,} 行で照合、最大の差 {gap:.3e}", file=sys.stderr)
    if gap > MAX_DISAGREEMENT:
        raise ForestMismatchError(
            f"{name}: 平たくした森が Booster.predict と最大 {gap:.3e} 違います"
            f"（許容 {MAX_DISAGREEMENT:.0e}）。成果物は書きません"
        )
    return Checked(n_rows=len(values), max_gap=gap)


def verification_rows(values: Float64) -> Float64:
    """照合に使う行。**実データに、実データが踏まない枝を足す。**

    検証日の行だけでは「未知のカテゴリ」「全部欠損」「ちょうど 0」に当たる枝を
    一度も通らないことがある。**配信で最初に踏むのがその枝**では困るので、
    ここで作って足す。
    """
    sample = values[:STRESS_ROWS]
    return np.vstack([values, _all_missing(sample), _all_zero(sample), _unseen(sample)])


def _all_missing(sample: Float64) -> Float64:
    """全列が欠損。**`default_left` の枝**をすべて通す。"""
    return np.full_like(sample, np.nan)


def _all_zero(sample: Float64) -> Float64:
    """全列が 0。**`missing_type = Zero` の枝**を通す。"""
    return np.zeros_like(sample)


def _unseen(sample: Float64) -> Float64:
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


@dataclass(frozen=True)
class Fitted:
    """当てはめの結果ひとそろい。**一緒に旅するものを 1 つにまとめる。**

    指標（`outcome`）・森の照合（`checks`）・入力の素性（`weather`）は、**登録簿にも
    モデルカードにも評価の表にも**入る。別々の引数で配ると、足したときに片方だけに
    入る——`inference_log` で実際にそうなった（W4 プラン §8.5.8）。
    """

    forests: Mapping[str, forest.Forest]
    outcome: harness.Outcome
    checks: Mapping[str, Checked]
    #: 読んだ日ごとの天気の被覆（**パージ日も含む**。役割は `outcome.split` が持つ）
    weather: Mapping[date, coverage.Coverage]


def fit_and_score(loaded: Loaded, samples: Samples, split: DaySplit) -> Fitted:
    """当てはめて平たくして、**B3 と同じ検証行の上で**測る。"""
    table = loaded.table
    fit_mask = mask_of(samples, split.fit)
    built = matrix.build(table.filter(pa.array(mask_of(samples, split.evaluate))))
    rows = verification_rows(built.values)
    made = {
        target.name: flatten_and_verify(
            train_one(table, samples, fit_mask, target), target.name, rows
        )
        for target in TARGETS
    }
    forests = {name: one for name, (one, _) in made.items()}
    return Fitted(
        forests=forests,
        outcome=harness.run(samples, split, {MODEL_NAME: predict_on(forests, built)}),
        checks={name: check for name, (_, check) in made.items()},
        weather=loaded.weather,
    )


def to_metrics(fitted: Fitted) -> dict[str, object]:
    """`model_versions.metrics` に入れる要約。**全体の重み付き Brier と B3 との差。**

    **照合の結果も残す**（W4-19）。配信は木を numpy で歩くので、「その森が
    `Booster.predict` と同じ値を出すことを何行で確かめたか」は**登録簿に残すべき事実**である。

    **天気の被覆も残す**（PR I）。`feature_set` は列が在ることしか語らないので、
    **何割の行に天気が入っていたか**を日ごとに書く。`--allow-mixed-weather` で通した
    場合も、**そのことは `spread_pp` と `max_spread_pp` の並びに出る**。
    """
    outcome = fitted.outcome
    return {
        "split": outcome.split.describe(),
        "n_fit": outcome.n_fit,
        "n_eval": outcome.n_eval,
        "feature_set": FEATURE_SET,
        "num_boost_round": NUM_BOOST_ROUND,
        "brier_weighted": _to_brier_rows(outcome),
        "forest_check": _to_check_rows(fitted.checks),
        "weather": _to_weather_rows(fitted),
        "caveat": "学習日が少ない。配線の確認であって精度の評価ではない（W4-07）",
    }


def _to_brier_rows(outcome: harness.Outcome) -> dict[str, object]:
    """system × ターゲットの重み付き Brier を、**B3 と並べて**残す。"""
    return {
        f"{one.slice.system}/{one.slice.target}": {
            "n": one.n,
            "b3": round(one.weighted["B3"].brier, 6),
            "lgbm": round(one.weighted[MODEL_NAME].brier, 6),
        }
        for one in outcome.overall
    }


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


def build_artifact(
    forests: Mapping[str, forest.Forest], days: Sequence[date]
) -> lightgbm_artifact.LightGbmArtifact:
    return lightgbm_artifact.build(
        forests=forests,
        model_version=model_version_for(days),
        feature_set=FEATURE_SET,
        created_at=datetime.now(UTC).isoformat(),
        train_days=[one.isoformat() for one in days],
        horizons_min=HORIZONS_MIN,
        params={**PARAMS, "num_boost_round": NUM_BOOST_ROUND},
    )


def to_registration(
    artifact: lightgbm_artifact.LightGbmArtifact, fitted: Fitted, card_path: str | None
) -> dict[str, object]:
    """`register_model_version` に渡す形。**`candidate` としてしか登録できない。**"""
    return {
        "model_version": artifact.model_version,
        "kind": LIGHTGBM_KIND,
        "status": "candidate",
        "feature_set": artifact.feature_set,
        "artifact_path": lightgbm_artifact.artifact_path(artifact.model_version),
        "train_days": list(artifact.train_days),
        "metrics": to_metrics(fitted),
        "card_path": card_reference(card_path),
        "note": "W4 の PR E′。配線の確認（W4-07）。精度は問わない",
    }


def card_reference(card_path: str | None) -> str | None:
    """登録簿に残すモデルカードの場所。**リポジトリからの相対に直す。**

    `--card` はシェルから見た**書き出し先**なので、`apps/ml` で走らせると
    `../../docs/model_cards/…` になる。それをそのまま登録簿に入れると、
    **読む人が「どこ起点の相対か」を復元できない**（2026-09-10 に 1 度そうなった）。

    `.git` のある場所を上へ辿って、そこからの相対にする。見つからなければ
    渡された文字列をそのまま残す（**勝手に別の場所を指さない**）。
    """
    if card_path is None:
        return None
    resolved = Path(card_path).resolve()
    for parent in resolved.parents:
        if (parent / ".git").exists():
            return str(resolved.relative_to(parent))
    return card_path


# ── 実行 ──────────────────────────────────────────────────────
def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LightGBM v0 を当てはめる")
    parser.add_argument("--from", dest="start", required=True, help="JST の暦日（含む）")
    parser.add_argument("--to", dest="end", required=True, help="JST の暦日（含む）")
    parser.add_argument("--eval-days", type=int, default=1, help="検証に使う日数")
    parser.add_argument("--purge-days", type=int, default=1, help="学習と検証の間に空ける日数")
    parser.add_argument("--local", default=None, help="Storage の代わりに読む場所")
    parser.add_argument("--out", default=None, help="成果物の書き出し先")
    parser.add_argument("--report", default=None, help="評価の Markdown の出力先")
    parser.add_argument("--card", default=None, help="モデルカードの出力先")
    parser.add_argument("--upload", action="store_true", help="成果物を Storage に置く")
    parser.add_argument("--register", action="store_true", help="model_versions に登録する")
    parser.add_argument(
        "--allow-mixed-weather",
        action="store_true",
        help="天気の被覆が日で違っても当てはめる（**登録簿に残る**。W4 プラン §8.5.3）",
    )
    return parser.parse_args(argv)


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
    loaded: Loaded, *, eval_days: int, purge_days: int, allow_mixed_weather: bool
) -> tuple[Samples, DaySplit]:
    """分割を決め、**当てはめの前に天気の門を通す**（PR I）。

    門を `run` の中に直書きしないのは、**呼び出し側が呼ぶのをやめても気づけない**
    からである（PR E′ で 1 度そうなった）。ここを通す限り、検査は本物の経路を見る。

    **開く順も意味を持つ。** 分割 → 門 → `to_samples` の順にするのは、止めるなら
    行を整数に直す前に止めたいため（3 日ぶんで 618 万行。実測では **1.1 秒**で止まる）。
    """
    split = split_days(loaded.days, eval_days, purge_days)
    print(f"天気の被覆: {coverage.describe(loaded.weather)}", file=sys.stderr)
    refuse_mixed_weather(loaded.weather, split, allowed=allow_mixed_weather)
    samples = to_samples(loaded.table)
    print(f"{split.describe()} / 全 {len(samples):,} 行", file=sys.stderr)
    return samples, split


def _write(path: str | None, text: str, label: str) -> None:
    if path is None:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(f"{label}を書き出しました: {path}", file=sys.stderr)


def run(argv: Sequence[str] | None = None) -> int:
    options = _arguments(argv)
    days = days_between(date.fromisoformat(options.start), date.fromisoformat(options.end))
    local = Path(options.local) if options.local else None

    with open_storage(read_storage_config()) as source:
        loaded = _load(source, days, local)
        samples, split = prepare(
            loaded,
            eval_days=options.eval_days,
            purge_days=options.purge_days,
            allow_mixed_weather=options.allow_mixed_weather,
        )
        fitted = fit_and_score(loaded, samples, split)
        artifact = build_artifact(fitted.forests, split.fit)
        body = lightgbm_artifact.to_bytes(artifact)
        if options.upload:
            source.upload(
                MODEL_BUCKET,
                lightgbm_artifact.artifact_path(artifact.model_version),
                body,
                lightgbm_artifact.CONTENT_TYPE,
            )
        if options.register:
            source.register_model_version(to_registration(artifact, fitted, options.card))

    if options.out:
        Path(options.out).write_bytes(body)
    _write(
        options.report,
        report.render_markdown(
            fitted.outcome, _title(artifact), _NOTE, "fit_lightgbm", weather=fitted.weather
        ),
        "評価",
    )
    _write(options.card, render_card(artifact, fitted), "モデルカード")
    print(f"{artifact.describe()} / {len(body):,} バイト")
    return 0


def _load(source: SupabaseIo, days: Sequence[date], local: Path | None) -> Loaded:
    """**全列を読む。** `evaluate_baselines` は 11 列に絞るが、LightGBM は 62 列を使う。"""
    return load_days(None if local else source, days, local, columns=None)


def _title(artifact: lightgbm_artifact.LightGbmArtifact) -> str:
    return f"LightGBM v0（{artifact.model_version}）と B0〜B3"


_NOTE: Final[str] = (
    "> **⚠️ この表は「配線が通っているか」の確認であって、精度の評価ではない**（W4-07）。\n>\n"
    "> 完全な暦日がまだ少なく、学習・パージ・検証を切ると**学習に使える日が 1〜2 日**しか\n"
    "> 残らない。**天気がどれだけ入っていたかは §2 に出る**——`feature_set` は列が在る\n"
    "> ことしか語らず、同じ `v3` でも 0% の日と 99.98% の日がある（W4 プラン §8.5.3）。\n>\n"
    "> **早期終了を使っていない**（本数は固定）。検証集合をモデル選択に使うと、\n"
    "> その集合で B3 と比べる意味が消えるためである。\n>\n"
    "> 測り直しは **9/16 以降**（完全な暦日が 9 日になり、学習 6 日・パージ 1 日・"
    "検証 2 日が取れる）。"
)


#: 採用基準（開発プラン §7.1）。相対改善がこれ以上なら合格。
ADOPTION_IMPROVEMENT: Final[float] = 0.10

#: 相対改善を判定に使ってよい Brier の下限（W3-16）。
JUDGEABLE_BRIER: Final[float] = 0.001

#: 校正の合格基準（開発プラン §7.1）。
MAX_ECE: Final[float] = 0.03


def _judgement_lines(outcome: harness.Outcome) -> list[str]:
    """**基準に照らして数える。** 合否の宣言はしない（v0 は判定の対象外）。"""
    judgeable = [one for one in outcome.by_bucket if one.weighted["B0"].brier >= JUDGEABLE_BRIER]
    improved = [
        one
        for one in judgeable
        if one.weighted["B3"].brier > 0
        and (one.weighted["B3"].brier - one.weighted[MODEL_NAME].brier) / one.weighted["B3"].brier
        >= ADOPTION_IMPROVEMENT
    ]
    worse = [one for one in judgeable if one.weighted[MODEL_NAME].brier > one.weighted["B3"].brier]
    # **判定は等頻度のほう**（W5-07）。等幅も並べる——2026-09-13 より前の記録はすべて
    # 等幅で、片方だけ書くと「悪くなった」のか「見えるようになった」のか分からない
    ece = max((one.weighted[MODEL_NAME].ece for one in outcome.by_horizon), default=0.0)
    uniform = max((one.weighted[MODEL_NAME].ece_uniform for one in outcome.by_horizon), default=0.0)
    return [
        f"- **判定対象のバケツ**：{len(judgeable)} / {len(outcome.by_bucket)} 件",
        f"- **B3 を 10% 以上改善**：{len(improved)} 件",
        f"- **B3 を下回った**：{len(worse)} 件",
        f"- **ECE の最大（水平別、等頻度 15）**：{ece:.5f}"
        f"（基準 {MAX_ECE} {'を満たす' if ece < MAX_ECE else 'を超える'}）",
        f"- **同（等幅 20。過去の記録と比べるため）**：{uniform:.5f}",
    ]


def _weather_limit(fitted: Fitted) -> str:
    """カードの「限界」に**測った数字**を書く。**日付を決め打ちにしない。**

    以前は「天気は 2026-09-08 以降しか無い」と書いてあった。**日付は動くのに文章は
    動かない**ので、貯まった日で当てはめ直したあとも同じ但し書きが残る。
    """
    used = coverage.restrict(fitted.weather, fitted.outcome.split.used())
    spread = coverage.spread_pp(used.values())
    if spread > coverage.MAX_SPREAD_PP:
        return (
            f"- **天気の被覆が学習日と検証日で {spread:.2f} ポイント違う**"
            f"（許容 {coverage.MAX_SPREAD_PP}）。`--allow-mixed-weather` で通してある"
        )
    return f"- **天気の被覆は揃っている**（学習日と検証日の差 {spread:.2f} ポイント）"


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


def render_card(artifact: lightgbm_artifact.LightGbmArtifact, fitted: Fitted) -> str:
    """モデルカード（開発プラン §7.3）。**登録の前提**なので、ここで必ず作る。"""
    outcome = fitted.outcome
    metrics = to_metrics(fitted)
    lines = [
        f"# モデルカード：{artifact.model_version}",
        "",
        "| | |",
        "|---|---|",
        "| 種類 | LightGBM（`binary`、ターゲット別に 2 モデル） |",
        "| 状態 | `candidate`（**配信しない**。W4-07） |",
        f"| 特徴量の版 | `{artifact.feature_set}` |",
        f"| 学習期間 | {', '.join(artifact.train_days)} |",
        f"| 分割 | {outcome.split.describe()} |",
        f"| 件数 | 学習 {outcome.n_fit:,} 行 / 検証 {outcome.n_eval:,} 行 |",
        f"| 列 | {len(artifact.columns)}（カテゴリ {len(artifact.categorical)}） |",
        f"| 木の本数 | {NUM_BOOST_ROUND}（**早期終了なし**） |",
        f"| 成果物 | 木の構造そのもの（書式の版 {artifact.format_version}）。"
        f"節 {', '.join(f'{name} {one.n_nodes:,}' for name, one in artifact.forests.items())} |",
        f"| 作成 | {artifact.created_at} |",
        "",
        "## 学習に使ったデータ",
        "",
        "**`feature_set` は「列が在る」しか語らない**（W4 プラン §8.5.3）。何割の行に"
        "天気が入っていたかを日ごとに出す。",
        "",
        *report.weather_block(fitted.weather, outcome.split),
        "",
        *_verification_lines(fitted.checks),
        "## 指標（全体、重み付き Brier）",
        "",
        "| system / ターゲット | n | B3 | LightGBM | 差 |",
        "|---|---:|---:|---:|---:|",
    ]
    for one in outcome.overall:
        b3 = one.weighted["B3"].brier
        lgbm = one.weighted[MODEL_NAME].brier
        change = "—" if b3 == 0 else f"{(b3 - lgbm) / b3:+.1%}"
        lines.append(
            f"| {one.slice.system} / {one.slice.target} | {one.n:,} "
            f"| {b3:.5f} | {lgbm:.5f} | {change} |"
        )
    lines.extend(
        [
            "",
            "## 採用基準（開発プラン §7.1、W3-16）",
            "",
            "**合否はバケツ別で決める。** `Brier ≥ 0.001` のすべての（水平, ターゲット, "
            "台数バケツ）で B3 を **10% 以上**改善し、ECE が 0.03 未満であること。",
            "",
            *_judgement_lines(outcome),
            "",
            "**v0 はこの基準で判定しない**（W4-07。配線の確認であって精度の評価ではない）。"
            "数だけ記録し、**9/16 以降に測り直す**。",
            "",
            "## 既知の限界",
            "",
            "- **学習日が少ない。** 完全な暦日が 3 日しか無く、パージを挟むと学習は 1〜2 日になる",
            _weather_limit(fitted),
            "- **早期終了を使っていない**（本数を固定）。検証集合をモデル選択に使わないため",
            "- **校正していない。** ECE は測るが isotonic は当てていない（開発プラン §7.4、W5）",
            "- **履歴プロファイルが無い**（`prof_*`。過去 28 日が要る。W5）",
            "- **`station_id` をカテゴリに入れていない**（21,000 カテゴリ。開発プラン §7.2）",
            "",
            "## 使い方",
            "",
            "```bash",
            "# 試し打ち（**どこにも書かない**）",
            "curl -H 'Authorization: Bearer $CRON_SECRET' \\",
            f"  '{'https://bike-chance.vercel.app/ml/infer/hellocycling'}"
            f"?model={artifact.model_version}'",
            "```",
            "",
            "**配信に切り替えるには `promote_model_version()` を人が呼ぶ**"
            "（CLAUDE.md §6。0038 はこの関数に誰にも grant していない）。",
            "",
            f"- 指標の全文：{metrics['split']}",
        ]
    )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(run())
