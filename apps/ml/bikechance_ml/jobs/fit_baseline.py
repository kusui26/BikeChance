"""配信するベースラインを当てはめて成果物にする（W3 プラン §5.10）。

**評価（`evaluate_baselines`）と違い、期間を分けない。** あちらは「LightGBM が超える
べき線がどこか」を測るので学習と検証を分ける。こちらは**配る値**を作るので、
手元にある日を全部使う。

**B3 の入力は leave-one-out で作る**（`eval/harness.py` と同じ）。そのまま当てはめると
B1・B2 が「自分の答えを見た」推定になり、係数が両者を信じすぎる（W3 プラン §12 の 102）。

使い方（環境変数は `.env` から読み込んでから）:

    set -a; . ./.env; set +a
    cd apps/ml
    ./.venv/bin/python -m bikechance_ml.jobs.fit_baseline \\
        --from 2026-09-06 --to 2026-09-08 --local .cache/features --upload
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final

import numpy as np
import pyarrow as pa

from bikechance_ml.baselines import blend, climatology, conditional
from bikechance_ml.baselines.artifact import FORMAT_VERSION, Artifact, TargetModel, to_bytes
from bikechance_ml.config import read_storage_config
from bikechance_ml.eval.dataset import TARGETS, Samples, Target, to_samples
from bikechance_ml.features.constants import FEATURE_SET, HORIZONS_MIN
from bikechance_ml.io.supabase import open_storage
from bikechance_ml.jobs.evaluate_baselines import days_between, load_days
from bikechance_ml.jobs.infer import MODEL_BUCKET, model_path

#: 成果物の Content-Type。gzip した JSON。
CONTENT_TYPE: Final[str] = "application/gzip"

#: 版の付け方。**最後の学習日**を入れる（いつまでのデータで作ったかが名前で分かる）。
VERSION_PREFIX: Final[str] = "baseline-b3-v0"


def model_version_for(days: Sequence[date]) -> str:
    return f"{VERSION_PREFIX}-{days[-1]:%Y%m%d}"


def fit_target(samples: Samples, target: Target) -> TargetModel:
    """1 ターゲットぶんを当てはめる。**全期間を使う。**"""
    everything = np.ones(len(samples), dtype=np.bool_)
    b1 = conditional.fit(samples, target, everything)
    b2 = climatology.fit(samples, target, everything)
    loo_b1 = conditional.predict_leave_one_out(b1, samples, target)
    loo_b2 = climatology.predict_leave_one_out(b2, samples, target, loo_b1)
    b3 = blend.fit(
        blend.design(loo_b1, loo_b2.probability, samples.h_min),
        samples.y(target),
        samples.weight,
    )
    return TargetModel(b1=b1, b2=b2, b3=b3)


def build_artifact(samples: Samples, ports: Sequence[str], days: Sequence[date]) -> Artifact:
    """成果物を組み立てる。**ポートの並びも一緒に固める**（B2 の番号が対応する）。"""
    return Artifact(
        format_version=FORMAT_VERSION,
        model_version=model_version_for(days),
        feature_set=FEATURE_SET,
        created_at=datetime.now(UTC).isoformat(),
        train_days=tuple(one.isoformat() for one in days),
        horizons_min=HORIZONS_MIN,
        systems=samples.systems,
        ports=tuple(ports),
        targets={target.name: fit_target(samples, target) for target in TARGETS},
    )


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="配信するベースラインを当てはめる")
    parser.add_argument("--from", dest="start", required=True, help="JST の暦日（含む）")
    parser.add_argument("--to", dest="end", required=True, help="JST の暦日（含む）")
    parser.add_argument("--local", default=None, help="Storage の代わりに読む場所")
    parser.add_argument("--out", default=None, help="成果物の書き出し先")
    parser.add_argument("--upload", action="store_true", help="Storage に置く")
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    options = _arguments(argv)
    days = days_between(date.fromisoformat(options.start), date.fromisoformat(options.end))
    local = Path(options.local) if options.local else None

    with open_storage(read_storage_config()) as source:
        table, found = load_days(None if local else source, days, local)
        samples = to_samples(table)
        ports = _port_names(table)
        artifact = build_artifact(samples, ports, found)
        body = to_bytes(artifact)
        if options.upload:
            source.upload(MODEL_BUCKET, model_path(artifact.model_version), body, CONTENT_TYPE)

    if options.out:
        Path(options.out).write_bytes(body)
    print(
        f"{artifact.describe()} / {len(body):,} バイト / パス {model_path(artifact.model_version)}"
    )
    print(f"ポート {len(ports):,} / 学習 {len(samples):,} 行", file=sys.stderr)
    return 0


def _port_names(table: pa.Table) -> tuple[str, ...]:
    """`(system_id, station_id)` の並び。**`to_samples` の番号と同じ順**にする。"""
    pairs = np.char.add(
        np.char.add(np.array(table.column("system_id").to_pylist(), dtype=np.str_), "/"),
        np.array(table.column("station_id").to_pylist(), dtype=np.str_),
    )
    return tuple(str(one) for one in np.unique(pairs))


if __name__ == "__main__":
    raise SystemExit(run())
