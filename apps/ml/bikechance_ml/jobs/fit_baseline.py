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

**当てはめ直しは 1 行である**（W5 の PR M、W5-16）。期間を書かなければ
「**昨日までの `DEFAULT_TRAIN_DAYS` 日**」になる。

使い方（環境変数は `.env` から読み込んでから）:

    set -a; . ./.env; set +a
    cd apps/ml
    ./.venv/bin/python -m bikechance_ml.jobs.fit_baseline --upload

期間を指定したいときだけ書く（`--from` と `--days` は**どちらか一方**）:

    … --days 14 --upload              # 昨日までの 14 日
    … --from 2026-09-07 --to 2026-09-12 --local .cache --upload

`--local` の下は Storage のパスをそのまま並べたもの（`features/date=…` と
`profiles/date=…`）。**`--no-profile` を付けると従来どおり学習サンプルから作る**
（入れ替える前後を比べるため）。

**同じ日に 2 度 `--upload` しない。** 版の名前は**最終学習日**なので、期間を変えて
回し直すと**同じ名前の成果物を上書きする**——`model_versions` の行は動かないまま
**配る値だけが変わる**（昇格を経ずに。W5 プラン §12 の 157）。確かめたいだけなら
`--out` を使う。
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
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
from bikechance_ml.features.grid import jst_yesterday
from bikechance_ml.io.supabase import SupabaseIo, open_storage
from bikechance_ml.jobs import climate
from bikechance_ml.jobs.evaluate_baselines import days_between, load_days
from bikechance_ml.models.registry import MODEL_BUCKET

#: 成果物の Content-Type。gzip した JSON。
CONTENT_TYPE: Final[str] = "application/gzip"

#: 版の付け方。**最後の学習日**を入れる（いつまでのデータで作ったかが名前で分かる）。
VERSION_PREFIX: Final[str] = "baseline-b3-v0"

#: 期間を書かなかったときに読む日数（W5-16）。
#:
#: **PR D が「窓を延ばす理由」を消した。** 以前は B2（気候値）のセルを埋めるために
#: `features/` の日数を延ばすしかなかったが、**いま B2 の深さはプロファイル（28 日）が
#: 持つ**。`features/` を読むのは **B1（1 ターゲット 120 セル）と B3（係数 4 つ）だけ**で、
#: 1 日 205 万行あれば 1 セルあたり 1.7 万行になる——**7 日でも過剰である。**
#:
#: **窓が伸び続けないことが、当てはめ直しを日課にできる条件**でもある（実測：6 日・
#: 1,230 万行で 7 分 44 秒。日数に比例して伸びる）。
DEFAULT_TRAIN_DAYS: Final[int] = 7


class WindowError(ValueError):
    """期間の指定が噛み合っていない。**黙ってどちらかを選ばない。**"""


def window(
    *, start: str | None, end: str | None, days: int | None, now: datetime
) -> tuple[date, date]:
    """当てはめる期間を決める。**既定は「昨日までの 7 日」**（W5-16）。

    **`--from` と `--days` は同時に指定できない。** どちらも「始まり」を決めるもので、
    両方あると片方が黙って無視される（`parseArrival` が `at` と `in_min` を排他に
    しているのと同じ理由）。

    **`--to` の既定が「今日」ではなく「昨日」**なのは、**当日の `features/` がまだ
    無いから**である（日次ジョブが翌朝に作る）。今日を渡しても `load_days` が黙って
    飛ばすので害は無いが、**既定は実在する日にしておく**。
    """
    if start is not None and days is not None:
        raise WindowError("--from と --days は同時に指定できません（期間の決め方は 1 つ）")
    span = days if days is not None else DEFAULT_TRAIN_DAYS
    last = date.fromisoformat(end) if end else jst_yesterday(now)
    # 両端を含めるので 1 を引く（7 日 ＝ 6 日前から昨日まで）
    first = date.fromisoformat(start) if start else last - timedelta(days=span - 1)
    if first > last:
        raise WindowError(f"始まり（{first}）が終わり（{last}）より後です")
    return first, last


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
    parser.add_argument("--from", dest="start", default=None, help="JST の暦日（含む）")
    parser.add_argument("--to", dest="end", default=None, help="JST の暦日（含む。既定は昨日）")
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help=f"--to までの日数（既定 {DEFAULT_TRAIN_DAYS}）。--from とは同時に指定できない",
    )
    parser.add_argument("--local", default=None, help="Storage の代わりに読む場所")
    parser.add_argument("--out", default=None, help="成果物の書き出し先")
    parser.add_argument(
        "--upload", action="store_true", help="Storage に置く（**同じ名前があれば上書きする**）"
    )
    parser.add_argument(
        "--no-profile", action="store_true", help="気候値を学習サンプルの行から作る（従来）"
    )
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    options = _arguments(argv)
    try:
        first, last = window(
            start=options.start, end=options.end, days=options.days, now=datetime.now(UTC)
        )
    # **黙って既定に落ちない。** 期間を間違えたまま配る成果物を作るほうが高くつく
    except WindowError as error:
        print(f"期間の指定が違います: {error}", file=sys.stderr)
        return 2
    days = days_between(first, last)
    # **どの期間で当てはめたかを最初に言う。** 既定で走らせたときに、何日ぶんを
    # 読もうとしているかが目で分かる（読めなかった日は `load_days` が別に言う）
    print(f"学習期間 {first} 〜 {last}（{len(days)} 日）", file=sys.stderr)
    return _fit(options, days)


def _fit(options: argparse.Namespace, days: Sequence[date]) -> int:
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
    _report(artifact, body, samples, chosen)
    return 0


def _report(artifact: Artifact, body: bytes, samples: Samples, chosen: climatology.Source) -> None:
    print(
        f"{artifact.describe()} / {len(body):,} バイト / "
        f"パス {artifact_path(artifact.model_version)}"
    )
    print(
        f"ポート {samples.n_ports:,} / 学習 {len(samples):,} 行 / 気候値 {chosen.describe()}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(run())
