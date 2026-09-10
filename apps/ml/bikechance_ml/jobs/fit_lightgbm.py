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
from bikechance_ml.features.arrays import Bools, Float64
from bikechance_ml.features.constants import FEATURE_SET, HORIZONS_MIN
from bikechance_ml.io.supabase import SupabaseIo, open_storage
from bikechance_ml.jobs.evaluate_baselines import days_between, load_days
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


def fit_and_score(
    table: pa.Table, samples: Samples, split: DaySplit
) -> tuple[Mapping[str, forest.Forest], harness.Outcome, Mapping[str, Checked]]:
    """当てはめて平たくして、**B3 と同じ検証行の上で**測る。"""
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
    outcome = harness.run(samples, split, {MODEL_NAME: predict_on(forests, built)})
    return forests, outcome, {name: check for name, (_, check) in made.items()}


def to_metrics(outcome: harness.Outcome, checks: Mapping[str, Checked]) -> dict[str, object]:
    """`model_versions.metrics` に入れる要約。**全体の重み付き Brier と B3 との差。**

    **照合の結果も残す**（W4-19）。配信は木を numpy で歩くので、「その森が
    `Booster.predict` と同じ値を出すことを何行で確かめたか」は**登録簿に残すべき事実**である。
    """
    rows = {
        f"{one.slice.system}/{one.slice.target}": {
            "n": one.n,
            "b3": round(one.weighted["B3"].brier, 6),
            "lgbm": round(one.weighted[MODEL_NAME].brier, 6),
        }
        for one in outcome.overall
    }
    return {
        "split": outcome.split.describe(),
        "n_fit": outcome.n_fit,
        "n_eval": outcome.n_eval,
        "feature_set": FEATURE_SET,
        "num_boost_round": NUM_BOOST_ROUND,
        "brier_weighted": rows,
        "forest_check": _to_check_rows(checks),
        "caveat": "学習日が少ない。配線の確認であって精度の評価ではない（W4-07）",
    }


def _to_check_rows(checks: Mapping[str, Checked]) -> dict[str, object]:
    """照合の結果を JSON にする。**許容も一緒に残す**（後から読む人が判断できるように）。"""
    return {
        "tolerance": MAX_DISAGREEMENT,
        "targets": {
            name: {"n_rows": one.n_rows, "max_gap": one.max_gap} for name, one in checks.items()
        },
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
    artifact: lightgbm_artifact.LightGbmArtifact,
    outcome: harness.Outcome,
    checks: Mapping[str, Checked],
    card_path: str | None,
) -> dict[str, object]:
    """`register_model_version` に渡す形。**`candidate` としてしか登録できない。**"""
    return {
        "model_version": artifact.model_version,
        "kind": LIGHTGBM_KIND,
        "status": "candidate",
        "feature_set": artifact.feature_set,
        "artifact_path": lightgbm_artifact.artifact_path(artifact.model_version),
        "train_days": list(artifact.train_days),
        "metrics": to_metrics(outcome, checks),
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
    return parser.parse_args(argv)


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
        table, found = _load(source, days, local)
        samples = to_samples(table)
        split = split_days(found, options.eval_days, options.purge_days)
        print(f"{split.describe()} / 全 {len(samples):,} 行", file=sys.stderr)
        forests, outcome, checks = fit_and_score(table, samples, split)
        artifact = build_artifact(forests, split.fit)
        body = lightgbm_artifact.to_bytes(artifact)
        if options.upload:
            source.upload(
                MODEL_BUCKET,
                lightgbm_artifact.artifact_path(artifact.model_version),
                body,
                lightgbm_artifact.CONTENT_TYPE,
            )
        if options.register:
            source.register_model_version(to_registration(artifact, outcome, checks, options.card))

    if options.out:
        Path(options.out).write_bytes(body)
    _write(
        options.report,
        report.render_markdown(outcome, _title(artifact), _NOTE, "fit_lightgbm"),
        "評価",
    )
    _write(options.card, render_card(artifact, outcome, checks), "モデルカード")
    print(f"{artifact.describe()} / {len(body):,} バイト")
    return 0


def _load(
    source: SupabaseIo, days: Sequence[date], local: Path | None
) -> tuple[pa.Table, tuple[date, ...]]:
    """**全列を読む。** `evaluate_baselines` は 11 列に絞るが、LightGBM は 62 列を使う。"""
    return load_days(None if local else source, days, local, columns=None)


def _title(artifact: lightgbm_artifact.LightGbmArtifact) -> str:
    return f"LightGBM v0（{artifact.model_version}）と B0〜B3"


_NOTE: Final[str] = (
    "> **⚠️ この表は「配線が通っているか」の確認であって、精度の評価ではない**（W4-07）。\n>\n"
    "> 完全な暦日がまだ少なく、学習・パージ・検証を切ると**学習に使える日が 1〜2 日**しか\n"
    "> 残らない。**天気の 4 列は 2026-09-08 以降しか埋まらない**ので（W4 プラン §5.4）、\n"
    "> 学習日と検証日で入力の分布そのものが違う。\n>\n"
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
    ece = max((one.weighted[MODEL_NAME].ece for one in outcome.by_horizon), default=0.0)
    return [
        f"- **判定対象のバケツ**：{len(judgeable)} / {len(outcome.by_bucket)} 件",
        f"- **B3 を 10% 以上改善**：{len(improved)} 件",
        f"- **B3 を下回った**：{len(worse)} 件",
        f"- **ECE の最大（水平別）**：{ece:.5f}"
        f"（基準 {MAX_ECE} {'を満たす' if ece < MAX_ECE else 'を超える'}）",
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


def render_card(
    artifact: lightgbm_artifact.LightGbmArtifact,
    outcome: harness.Outcome,
    checks: Mapping[str, Checked],
) -> str:
    """モデルカード（開発プラン §7.3）。**登録の前提**なので、ここで必ず作る。"""
    metrics = to_metrics(outcome, checks)
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
        *_verification_lines(checks),
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
            "- **天気は 2026-09-08 以降しか無い**（W4 プラン §5.4）。学習日と検証日で"
            "入力の分布が違う",
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
