#!/usr/bin/env python3
"""配っている成果物を開いて、**曜日種別ごとに使えるセルを数える**（W5 プラン §8.4）。

**数字だけでなく、配っている物そのものを開く。** W5 の着手前にこれをして、
「配信中の確率は 1 システム 1 水平あたり 4〜6 個の値しか取らない」——つまり
**気候値が 100% B1 に落ちていた**ことが分かった（W5 プラン §2.3c）。報告書は
「当てはめたときの表」を語るが、**配信が読むのは Storage に置いた成果物**である。

**曜日種別で割って数えるのが要。** セルの鍵は `(ポート, 曜日種別, 15 分枠)` で、
`sat` と `sun_holiday` は**その種別の日が下限（`MIN_CELL_DAYS`）に届くまで 1 つも
立たない**。総数だけ見ていると、**平日のセルが埋まっているぶんで週末が空であることが
隠れる**——2026-09-13 に昇格した版がまさにそれだった。

**「0 が正しい」ときもある。** 下限は 2026-09-23 に 3 日になった（D-30）。その日数に
届いていない曜日種別は、**0 であるのが正しい**（2 日ぶんの B2 は配らないほうが当たって
いた。W5 プラン §12 の 177）。だから**成果物が使ったプロファイル**
（`profiles/date=<学習の最終日>`）を開き、**曜日種別ごとの日数と下限を突き合わせて**判定する。

使い方（環境変数は `.env` から読み込んでから）:

    cd apps/ml
    ./.venv/bin/python ../../scripts/inspect-artifact.py                    # active な版
    ./.venv/bin/python ../../scripts/inspect-artifact.py --version <版>
    ./.venv/bin/python ../../scripts/inspect-artifact.py --file <成果物>   # 手元のもの

**合格**（W5 プラン §8.4）：**下限の日数に届いた曜日種別には 0 でないセルがあり、
届いていない曜日種別には 1 つも無い**。満たさなければ終了コード 1 を返すので、昇格の
手順にそのまま挟める。プロファイルが読めなければ、**3 曜日種別すべてにセルがあること**
を求める（前の判定）。

**書式の版と大きさ、B2 の使えるセルの数と率の範囲も出す**（W6 の PR B）。当てはめ直しの
あとに「書式 2 で置けたか」「セルの数と率が崩れていないか」を目で見るためである
（W6 プラン §8.1 の 2）。版 1 と版 2 のどちらも開ける（契約 30）。率が 0〜1 の外なら
読み手が開く前に止めるので、ここに出るのは必ず 0〜1 の中である。
"""

import argparse
import sys
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from bikechance_ml.baselines import artifact as baseline_artifact
from bikechance_ml.baselines import climatology
from bikechance_ml.baselines.climatology import SLOTS_PER_DAY
from bikechance_ml.config import read_storage_config
from bikechance_ml.features import profile
from bikechance_ml.features.arrays import Bools, Int64
from bikechance_ml.features.calendar import DOW_TYPE_ORDER
from bikechance_ml.features.grid import profile_path
from bikechance_ml.io.supabase import PARQUET_BUCKET, open_storage
from bikechance_ml.models import registry


def dow_of(keys: Int64) -> Int64:
    """セルの鍵から曜日種別の番号を取り出す。**鍵の組み立ては `cell_key` 1 か所。**"""
    return np.asarray((keys // SLOTS_PER_DAY) % len(DOW_TYPE_ORDER), dtype=np.int64)


def counts_by_dow(usable: Bools) -> dict[str, int]:
    """曜日種別ごとの使えるセルの数。"""
    keys = np.nonzero(usable)[0]
    found = np.bincount(dow_of(keys), minlength=len(DOW_TYPE_ORDER))
    return {name: int(found[index]) for index, name in enumerate(DOW_TYPE_ORDER)}


def fewest_cells(one: baseline_artifact.Artifact) -> dict[str, int]:
    """曜日種別ごとの、**ターゲットのうち少ないほう**のセル数（判定はこれで見る）。"""
    counted = [counts_by_dow(model.b2.usable) for model in one.targets.values()]
    return {dow: min(one_target[dow] for one_target in counted) for dow in DOW_TYPE_ORDER}


def floor_days(one: baseline_artifact.Artifact) -> int:
    """成果物に書かれた下限（日）。**ターゲットで違えば厳しいほう**を使う。"""
    return max(model.b2.min_days for model in one.targets.values())


def profile_days(source: registry.ReadsModels, day: str) -> dict[str, int] | None:
    """**成果物が使ったプロファイル**の、曜日種別ごとの日数（セルに寄与した日の最大）。"""
    path = profile_path(date.fromisoformat(day), profile.PROFILE_NAME)
    body = source.download(PARQUET_BUCKET, path)
    if body is None:
        return None
    table = pq.read_table(pa.BufferReader(body), columns=["dow_type", "n_days"])
    grouped = table.group_by("dow_type").aggregate([("n_days", "max")])
    kinds = grouped.column("dow_type").to_pylist()
    days = grouped.column("n_days_max").to_pylist()
    return {str(kind): int(most) for kind, most in zip(kinds, days, strict=True)}


def header(one: baseline_artifact.Artifact, size_bytes: int) -> list[str]:
    """成果物そのものの素性。**どの期間・どの書式で作り、どれだけの大きさか。**"""
    days = one.train_days
    return [
        f"{one.model_version}（書式 {one.format_version}、feature_set {one.feature_set}、"
        f"{size_bytes:,} B）",
        f"  学習 {days[0]}〜{days[-1]}（{len(days)} 日） / 作成 {one.created_at}",
        f"  ポート {len(one.ports):,} / システム {len(one.systems)} / 水平 {len(one.horizons_min)}",
    ]


def climatology_cells(one: baseline_artifact.Artifact) -> list[str]:
    """B2 の使えるセルの数と、率の範囲。**書式を替えても数と値が崩れていないかを見る。**"""
    return ["", *[cells_line(name, model.b2) for name, model in sorted(one.targets.items())]]


def cells_line(name: str, table: climatology.Table) -> str:
    """1 ターゲットぶん。**率の範囲は使えるセルだけで取る**（使えないセルの 0 を混ぜない）。"""
    rates = table.rate[table.usable]
    total = table.usable.size
    share = rates.size / total if total else 0.0
    span = f"{float(rates.min()):.6f}〜{float(rates.max()):.6f}" if rates.size else "—"
    return f"  {name} の B2：使えるセル {rates.size:,} / {total:,}（{share:.1%}） / 率 {span}"


def floors(one: baseline_artifact.Artifact) -> list[str]:
    """下限（件数・日数）。**どこまで信じた表かを一緒に出す**（§12 の 167）。"""
    return [
        "",
        *[
            f"  {name} の下限：{model.b2.min_samples} 件 {model.b2.min_days} 日"
            for name, model in sorted(one.targets.items())
        ],
    ]


def expectation(days: int | None, floor: int) -> str:
    """その曜日種別に**セルが立つはずか**。プロファイルが無ければ「立つはず」とみなす。"""
    if days is None:
        return "立つはず（プロファイル無し）"
    return "立つはず" if days >= floor else "0 が正しい"


def table(one: baseline_artifact.Artifact, days: Mapping[str, int] | None) -> list[str]:
    """曜日種別 × ターゲットの表。**プロファイルの日数と、立つはずかを並べる。**"""
    targets = sorted(one.targets)
    slots = len(one.ports) * SLOTS_PER_DAY
    rows = [
        "",
        "| 曜日種別 | "
        + " | ".join(targets)
        + " | 1 ポート × 96 枠に対する割合 | プロファイルの日数 | 期待 |",
        "|---|" + "---:|" * (len(targets) + 2) + "---|",
    ]
    for dow in DOW_TYPE_ORDER:
        counted = {name: counts_by_dow(one.targets[name].b2.usable)[dow] for name in targets}
        share = max(counted.values()) / slots if slots else 0.0
        cells = " | ".join(f"{counted[name]:,}" for name in targets)
        seen = None if days is None else days.get(dow, 0)
        shown = "—" if seen is None else str(seen)
        rows.append(
            f"| {dow} | {cells} | {share:.1%} | {shown} | {expectation(seen, floor_days(one))} |"
        )
    return rows


def verdict(one: baseline_artifact.Artifact, days: Mapping[str, int] | None) -> tuple[bool, str]:
    """合格か。**立つはずの種別にセルがあり、0 が正しい種別には 1 つも無いこと。**"""
    cells, floor = fewest_cells(one), floor_days(one)
    missing = [
        dow
        for dow in DOW_TYPE_ORDER
        if cells[dow] == 0 and (days is None or days.get(dow, 0) >= floor)
    ]
    stray = [
        dow
        for dow in DOW_TYPE_ORDER
        if cells[dow] > 0 and days is not None and days.get(dow, 0) < floor
    ]
    if missing and days is None:
        where = ", ".join(missing)
        return False, (
            f"**不合格**：セルが 1 つも無い（{where}）。"
            "プロファイルが読めないので 3 種別すべてを求めた"
        )
    if missing:
        where = ", ".join(missing)
        return False, f"**不合格**：下限（{floor} 日）に届いているのにセルが無い（{where}）"
    if stray:
        where = ", ".join(stray)
        return False, (
            f"**不合格**：下限（{floor} 日）に届いていないのにセルがある（{where}）"
            "——別のプロファイルで作った成果物かもしれません"
        )
    waiting = [dow for dow in DOW_TYPE_ORDER if cells[dow] == 0]
    note = f"（{', '.join(waiting)} は下限に届いていないので 0 が正しい）" if waiting else ""
    return True, f"**合格**：立つはずの曜日種別にはすべてセルがあります{note}"


def fetch(source: registry.ReadsModels, options: argparse.Namespace) -> bytes:
    """成果物のバイト列を取る。**手元のファイルか、Storage の版か。**"""
    if options.file:
        return Path(options.file).read_bytes()
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
    options = _arguments(argv)
    with open_storage(read_storage_config()) as source:
        body = fetch(source, options)
        one = baseline_artifact.from_bytes(body)
        days = profile_days(source, one.train_days[-1])
    if days is None:
        print(f"（プロファイル profiles/date={one.train_days[-1]} が読めません）", file=sys.stderr)
    passed, message = verdict(one, days)
    lines = [*header(one, len(body)), *climatology_cells(one), *floors(one), *table(one, days)]
    print("\n".join([*lines, "", message]))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
