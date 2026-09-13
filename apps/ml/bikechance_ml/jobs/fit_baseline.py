"""配信するベースラインを当てはめて成果物にする（W3 プラン §5.10）。

**評価（`evaluate_baselines`）と違い、期間を分けない。** あちらは「LightGBM が超える
べき線がどこか」を測るので学習と検証を分ける。こちらは**配る値**を作るので、
手元にある日を全部使う。

**B3 の入力は自分のぶんを引いて作る**（`eval/harness.py` と同じ）。そのまま当てはめると
B1・B2 が「自分の答えを見た」推定になり、係数が両者を信じすぎる（W3 プラン §12 の 102）。

**B2 はプロファイルから作る**（W5 プラン §6.4 の PR D）。`profiles/date=D/profile.parquet`
（**D は学習の最終日**）が読めればそちら、読めなければ従来どおり学習サンプルの行から
作る。プロファイルから作ると 1 セルが 1 日あたり 3 点になり、**2 日目にはほぼ全セルが
下限を満たす**（実測：`features/` からは 1.62 行・中央 1 しか入らず、気候値が 100%
B1 に落ちていた。§2.3）。

使い方（環境変数は `.env` から読み込んでから）:

    set -a; . ./.env; set +a
    cd apps/ml
    ./.venv/bin/python -m bikechance_ml.jobs.fit_baseline \\
        --from 2026-09-07 --to 2026-09-12 --local .cache --upload

`--local` の下は Storage のパスをそのまま並べたもの（`features/date=…` と
`profiles/date=…`）。**`--no-profile` を付けると従来どおり学習サンプルから作る**
（入れ替える前後を比べるため）。
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final

import numpy as np

from bikechance_ml.baselines import blend, climatology, conditional
from bikechance_ml.baselines.artifact import (
    FORMAT_VERSION,
    Artifact,
    TargetModel,
    artifact_path,
    to_bytes,
)
from bikechance_ml.config import read_storage_config
from bikechance_ml.eval.dataset import TARGETS, Samples, Target, to_samples
from bikechance_ml.features.constants import FEATURE_SET, HORIZONS_MIN
from bikechance_ml.io.supabase import SupabaseIo, open_storage
from bikechance_ml.jobs import climate
from bikechance_ml.jobs.evaluate_baselines import days_between, load_days
from bikechance_ml.models.registry import MODEL_BUCKET

#: 成果物の Content-Type。gzip した JSON。
CONTENT_TYPE: Final[str] = "application/gzip"

#: 版の付け方。**最後の学習日**を入れる（いつまでのデータで作ったかが名前で分かる）。
VERSION_PREFIX: Final[str] = "baseline-b3-v0"


def model_version_for(days: Sequence[date]) -> str:
    return f"{VERSION_PREFIX}-{days[-1]:%Y%m%d}"


def fit_target(samples: Samples, target: Target, source: climatology.Source) -> TargetModel:
    """1 ターゲットぶんを当てはめる。**全期間を使う。**

    混合（B3）を当てはめるのは、**自分のぶんを引ける行**だけである（`blend_rows`）。
    プロファイルから作るときは「その日ぶんを引く」ので、`daily.parquet` が無い日は外れる。
    """
    everything = np.ones(len(samples), dtype=np.bool_)
    b1 = conditional.fit(samples, target, everything)
    b2 = source.table(samples, target, everything)
    fitted = samples.take(source.blend_rows(samples))
    loo_b1 = conditional.predict_leave_one_out(b1, fitted, target)
    loo_b2 = source.leave_out(b2, fitted, target, loo_b1)
    b3 = blend.fit(
        blend.design(loo_b1, loo_b2.probability, fitted.h_min),
        fitted.y(target),
        fitted.weight,
    )
    return TargetModel(b1=b1, b2=b2, b3=b3)


def build_artifact(samples: Samples, days: Sequence[date], source: climatology.Source) -> Artifact:
    """成果物を組み立てる。**ポートの並びも一緒に固める**（B2 の番号が対応する）。

    並びは `Samples.ports`——**番号を振ったのと同じ `np.unique` の結果**である。
    以前はここで同じ `np.unique` をもう一度呼んでいた（W5 プラン §12 の 142）。
    """
    return Artifact(
        format_version=FORMAT_VERSION,
        model_version=model_version_for(days),
        feature_set=FEATURE_SET,
        created_at=datetime.now(UTC).isoformat(),
        train_days=tuple(one.isoformat() for one in days),
        horizons_min=HORIZONS_MIN,
        systems=samples.systems,
        ports=samples.ports,
        targets={target.name: fit_target(samples, target, source) for target in TARGETS},
    )


def climate_source(
    source: SupabaseIo | None,
    days: Sequence[date],
    local: Path | None,
    samples: Samples,
    no_profile: bool = False,
) -> climatology.Source:
    """B2 の作り方を決める。**プロファイルが読めればそちら。**

    読むのは**最後の学習日の版**（`climate.load`）。`fit_baseline` は期間を分けないので、
    その版は学習日をすべて含み、**配信を始める日は必ずそれより後**である。
    """
    if no_profile:
        return climatology.FromSamples()
    found = climate.load(source, days, local, samples.ports)
    return found if found is not None else climatology.FromSamples()


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="配信するベースラインを当てはめる")
    parser.add_argument("--from", dest="start", required=True, help="JST の暦日（含む）")
    parser.add_argument("--to", dest="end", required=True, help="JST の暦日（含む）")
    parser.add_argument("--local", default=None, help="Storage の代わりに読む場所")
    parser.add_argument("--out", default=None, help="成果物の書き出し先")
    parser.add_argument("--upload", action="store_true", help="Storage に置く")
    parser.add_argument(
        "--no-profile", action="store_true", help="気候値を学習サンプルの行から作る（従来）"
    )
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    options = _arguments(argv)
    days = days_between(date.fromisoformat(options.start), date.fromisoformat(options.end))
    local = Path(options.local) if options.local else None

    with open_storage(read_storage_config()) as source:
        reader = None if local else source
        loaded = load_days(reader, days, local)
        samples = to_samples(loaded.table)
        chosen = climate_source(reader, loaded.days, local, samples, options.no_profile)
        # **天気の被覆は見ない。** ベースラインが読むのは 6 列で、天気はその中に無い
        # （`baselines/` は `Samples` しか触らない）。混ざっても値が変わらない
        artifact = build_artifact(samples, loaded.days, chosen)
        body = to_bytes(artifact)
        if options.upload:
            source.upload(MODEL_BUCKET, artifact_path(artifact.model_version), body, CONTENT_TYPE)

    if options.out:
        Path(options.out).write_bytes(body)
    print(
        f"{artifact.describe()} / {len(body):,} バイト / "
        f"パス {artifact_path(artifact.model_version)}"
    )
    print(
        f"ポート {samples.n_ports:,} / 学習 {len(samples):,} 行 / 気候値 {chosen.describe()}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
