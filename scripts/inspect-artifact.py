#!/usr/bin/env python3
"""配っている成果物を開いて、**曜日種別ごとに使えるセルを数える**（W5 プラン §8.4）。

**数字だけでなく、配っている物そのものを開く。** W5 の着手前にこれをして、
「配信中の確率は 1 システム 1 水平あたり 4〜6 個の値しか取らない」——つまり
**気候値が 100% B1 に落ちていた**ことが分かった（W5 プラン §2.3c）。報告書は
「当てはめたときの表」を語るが、**配信が読むのは Storage に置いた成果物**である。

**曜日種別で割って数えるのが要。** セルの鍵は `(ポート, 曜日種別, 15 分枠)` で、
`sat` と `sun_holiday` は**その種別の日が 2 つ揃うまで 1 つも立たない**
（`MIN_CELL_DAYS`）。総数だけ見ていると、**平日のセルが埋まっているぶんで
週末が空であることが隠れる**——2026-09-13 に昇格した版がまさにそれだった。

使い方（環境変数は `.env` から読み込んでから）:

    cd apps/ml
    ./.venv/bin/python ../../scripts/inspect-artifact.py                    # active な版
    ./.venv/bin/python ../../scripts/inspect-artifact.py --version <版>
    ./.venv/bin/python ../../scripts/inspect-artifact.py --file <成果物>   # 手元のもの

**合格**：**3 曜日種別すべてに 0 でないセルがある**（W5 プラン §8.4）。満たさなければ
終了コード 1 を返すので、昇格の手順にそのまま挟める。
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import numpy as np

from bikechance_ml.baselines import artifact as baseline_artifact
from bikechance_ml.baselines.climatology import SLOTS_PER_DAY
from bikechance_ml.config import read_storage_config
from bikechance_ml.features.arrays import Bools, Int64
from bikechance_ml.features.calendar import DOW_TYPE_ORDER
from bikechance_ml.io.supabase import open_storage
from bikechance_ml.models import registry

#: 3 曜日種別すべてにセルが要る（W5 プラン §8.4 の合格）。
WANTED: Final[tuple[str, ...]] = DOW_TYPE_ORDER


def dow_of(keys: Int64) -> Int64:
    """セルの鍵から曜日種別の番号を取り出す。**鍵の組み立ては `cell_key` 1 か所。**"""
    return np.asarray((keys // SLOTS_PER_DAY) % len(DOW_TYPE_ORDER), dtype=np.int64)


def counts_by_dow(usable: Bools) -> dict[str, int]:
    """曜日種別ごとの使えるセルの数。"""
    keys = np.nonzero(usable)[0]
    found = np.bincount(dow_of(keys), minlength=len(DOW_TYPE_ORDER))
    return {name: int(found[index]) for index, name in enumerate(DOW_TYPE_ORDER)}


def header(one: baseline_artifact.Artifact) -> list[str]:
    """成果物そのものの素性。**どの期間で作ったかが読めること。**"""
    days = one.train_days
    return [
        f"{one.model_version}（書式 {one.format_version}、feature_set {one.feature_set}）",
        f"  学習 {days[0]}〜{days[-1]}（{len(days)} 日） / 作成 {one.created_at}",
        f"  ポート {len(one.ports):,} / システム {len(one.systems)} / 水平 {len(one.horizons_min)}",
    ]


def table(one: baseline_artifact.Artifact) -> list[str]:
    """曜日種別 × ターゲットの表。**1 ポートあたりの枠数（96）に対する割合も出す。**"""
    targets = sorted(one.targets)
    slots = len(one.ports) * SLOTS_PER_DAY
    rows = [
        "",
        "| 曜日種別 | " + " | ".join(targets) + " | 1 ポート × 96 枠に対する割合 |",
        "|---|" + "---:|" * (len(targets) + 1),
    ]
    for dow in DOW_TYPE_ORDER:
        counted = {name: counts_by_dow(one.targets[name].b2.usable)[dow] for name in targets}
        share = max(counted.values()) / slots if slots else 0.0
        cells = " | ".join(f"{counted[name]:,}" for name in targets)
        rows.append(f"| {dow} | {cells} | {share:.1%} |")
    return rows


def floors(one: baseline_artifact.Artifact) -> list[str]:
    """下限（件数・日数）。**どこまで信じた表かを一緒に出す**（§12 の 167）。"""
    return [
        "",
        *[
            f"  {name} の下限：{model.b2.min_samples} 件 {model.b2.min_days} 日"
            for name, model in sorted(one.targets.items())
        ],
    ]


def verdict(one: baseline_artifact.Artifact) -> tuple[bool, str]:
    """合格か。**3 曜日種別すべてに 0 でないセルがあること。**"""
    empty = sorted(
        {
            dow
            for model in one.targets.values()
            for dow, count in counts_by_dow(model.b2.usable).items()
            if count == 0
        }
    )
    if empty:
        return False, f"**不合格**：セルが 1 つも無い曜日種別があります（{', '.join(empty)}）"
    return True, "**合格**：3 曜日種別すべてにセルがあります"


def fetch(options: argparse.Namespace) -> bytes:
    """成果物のバイト列を取る。**手元のファイルか、Storage の版か。**"""
    if options.file:
        return Path(options.file).read_bytes()
    with open_storage(read_storage_config()) as source:
        wanted = options.version
        found = registry.named(source, wanted) if wanted else registry.active(source)
        print(f"（登録簿：{found.model_version} / {found.status}）", file=sys.stderr)
        body = source.download(registry.MODEL_BUCKET, found.artifact_path)
    if body is None:
        raise SystemExit(f"成果物が Storage にありません: {found.artifact_path}")
    return body


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="成果物を開いて曜日種別ごとのセルを数える")
    parser.add_argument("--version", default=None, help="登録簿の版（既定は active）")
    parser.add_argument("--file", default=None, help="Storage の代わりに読む成果物")
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    one = baseline_artifact.from_bytes(fetch(_arguments(argv)))
    passed, message = verdict(one)
    print("\n".join([*header(one), *floors(one), *table(one), "", message]))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
