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

**早期終了は使わない。** 開発プラン §7.2 は「検証セットで 100 ラウンド」と書いているが、
それは 学習 → パージ → 検証 → テスト の 4 分割が取れる前提である。いまは完全な暦日が
3 日しか無く、検証を早期終了に使うと**その集合で B3 と比べる意味が消える**（モデル選択に
使った集合で測ることになる）。v0 は**本数を固定**し、4 分割が取れる 9/16 以降に見直す。
"""

import argparse
import sys
from collections.abc import Mapping, Sequence
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
from bikechance_ml.models import matrix
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


def predict_on(
    boosters: Mapping[str, lgb.Booster], table: pa.Table, mask: Bools
) -> dict[str, Float64]:
    """検証期間の行に予測を付ける。**行の並びは `samples.take(mask)` と同じ。**"""
    built = matrix.build(table.filter(pa.array(mask)))
    return {name: lightgbm_artifact.predict(one, built) for name, one in boosters.items()}


def fit_and_score(
    table: pa.Table, samples: Samples, split: DaySplit
) -> tuple[Mapping[str, lgb.Booster], harness.Outcome]:
    """当てはめて、**B3 と同じ検証行の上で**測る。"""
    fit_mask = mask_of(samples, split.fit)
    eval_mask = mask_of(samples, split.evaluate)
    boosters = {target.name: train_one(table, samples, fit_mask, target) for target in TARGETS}
    extra = {MODEL_NAME: predict_on(boosters, table, eval_mask)}
    return boosters, harness.run(samples, split, extra)


def to_metrics(outcome: harness.Outcome) -> dict[str, object]:
    """`model_versions.metrics` に入れる要約。**全体の重み付き Brier と B3 との差。**"""
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
        "caveat": "学習日が少ない。配線の確認であって精度の評価ではない（W4-07）",
    }


def build_artifact(
    boosters: Mapping[str, lgb.Booster], days: Sequence[date]
) -> lightgbm_artifact.LightGbmArtifact:
    return lightgbm_artifact.build(
        boosters=boosters,
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
        "metrics": to_metrics(outcome),
        "card_path": card_path,
        "note": "W4 の PR E。配線の確認（W4-07）。精度は問わない",
    }


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
        boosters, outcome = fit_and_score(table, samples, split)
        artifact = build_artifact(boosters, split.fit)
        body = lightgbm_artifact.to_bytes(artifact)
        if options.upload:
            source.upload(
                MODEL_BUCKET,
                lightgbm_artifact.artifact_path(artifact.model_version),
                body,
                lightgbm_artifact.CONTENT_TYPE,
            )
        if options.register:
            source.register_model_version(to_registration(artifact, outcome, options.card))

    if options.out:
        Path(options.out).write_bytes(body)
    _write(
        options.report,
        report.render_markdown(outcome, _title(artifact), _NOTE, "fit_lightgbm"),
        "評価",
    )
    _write(options.card, render_card(artifact, outcome), "モデルカード")
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


def render_card(artifact: lightgbm_artifact.LightGbmArtifact, outcome: harness.Outcome) -> str:
    """モデルカード（開発プラン §7.3）。**登録の前提**なので、ここで必ず作る。"""
    metrics = to_metrics(outcome)
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
        f"| 作成 | {artifact.created_at} |",
        "",
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
