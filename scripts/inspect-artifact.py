#!/usr/bin/env python3
"""配っている成果物を開いて確かめる（W5 プラン §8.4、W6 の PR F）。

**ベースライン**は曜日種別ごとに使えるセルを数え、**LightGBM** は森と門の表を照合する
（下の「LightGBM の成果物」）。種類は登録簿の `kind` で分ける（`--file` なら中身の `kind`）。

## ベースライン：曜日種別ごとに使えるセルを数える

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

**下限は曜日種別ごとに見る**（W6 の PR A、D-37）。配る側の下限は曜日種別で違い、
「配らない」種別もある（土日祝は 9/28 に決める）。**配らない種別は 0 が正しい。**
下限の欄の無い成果物（PR A より前）は、全種別に `b2_min_days` を当てた形として読む。

**書式の版と大きさ、B2 の使えるセルの数と率の範囲も出す**（W6 の PR B）。当てはめ直しの
あとに「書式 2 で置けたか」「セルの数と率が崩れていないか」を目で見るためである
（W6 プラン §8.1 の 2）。版 1 と版 2 のどちらも開ける（契約 30）。率が 0〜1 の外なら
読み手が開く前に止めるので、ここに出るのは必ず 0〜1 の中である。

## LightGBM の成果物（W6 の PR F、W6 プラン §8.7 の 3）

**読めた＝いまのコードと一致。** 読み手（`models/artifact.py`）が書式の版・列の並び・
カテゴリ・語彙を照合し、違えば読まずに止める（木は位置で特徴量を見るので、ずれても例外は
出ず確率だけが変わる）。ここでは**特徴量の版**も照合し、**木の本数・節の数・最大の深さ**を出す。

**門の表も開く**（`lightgbm/<版>.gates.json.gz`。`--file` のときは `--gates`）。**この森と
組か**（表が持つ森の SHA-256）を確かめ、LightGBM に回すセルの数を system × ターゲットで出す。
**合格**：特徴量の版がいまと同じで、門の表があり、この森と組であること。

    ./.venv/bin/python ../../scripts/inspect-artifact.py --version lgbm-v1-20261008
    ./.venv/bin/python ../../scripts/inspect-artifact.py --file a.json.gz --gates a.gates.json.gz
"""

import argparse
import gzip
import json
import sys
from collections import Counter
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
from bikechance_ml.features.arrays import Bools
from bikechance_ml.features.calendar import DOW_TYPE_ORDER
from bikechance_ml.features.constants import FEATURE_SET
from bikechance_ml.features.grid import profile_path
from bikechance_ml.io.supabase import PARQUET_BUCKET, open_storage
from bikechance_ml.models import artifact as lightgbm_artifact
from bikechance_ml.models import gates as gate_tables
from bikechance_ml.models import registry


def counts_by_dow(usable: Bools) -> dict[str, int]:
    """曜日種別ごとの使えるセルの数。**鍵から種別を取り出す式は `climatology` の 1 か所。**"""
    keys = np.asarray(np.nonzero(usable)[0], dtype=np.int64)
    found = np.bincount(climatology.dow_of_cell(keys), minlength=len(DOW_TYPE_ORDER))
    return {name: int(found[index]) for index, name in enumerate(DOW_TYPE_ORDER)}


def fewest_cells(one: baseline_artifact.Artifact) -> dict[str, int]:
    """曜日種別ごとの、**ターゲットのうち少ないほう**のセル数（判定はこれで見る）。"""
    counted = [counts_by_dow(model.b2.usable) for model in one.targets.values()]
    return {dow: min(one_target[dow] for one_target in counted) for dow in DOW_TYPE_ORDER}


def floor_by_dow(one: baseline_artifact.Artifact) -> dict[str, int | None]:
    """曜日種別ごとの配る側の下限（日。None は配らない）。**ターゲットで違えば厳しいほう。**"""
    return {dow: _strictest(one, dow) for dow in DOW_TYPE_ORDER}


def _strictest(one: baseline_artifact.Artifact, dow: str) -> int | None:
    floors = [model.b2.floor.serve[dow] for model in one.targets.values()]
    if any(floor is None for floor in floors):
        return None
    return max(floor for floor in floors if floor is not None)


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
    """下限（件数・曜日種別ごとの日数）と厚さ。**どこまで信じた表かを一緒に出す**（§12 の 167）。"""
    lines = [""]
    for name, model in sorted(one.targets.items()):
        lines.append(f"  {name} の下限：{model.b2.min_samples} 件・{model.b2.floor.describe()}")
        thickness = climatology.describe_thickness(model.b2.max_days)
        lines.append(f"  {name} の厚さ（曜日種別ごとの日数の最大）：{thickness}")
    return lines


def expectation(days: int | None, floor: int | None) -> str:
    """その曜日種別に**セルが立つはずか**。プロファイルが無ければ「立つはず」とみなす。"""
    if floor is None:
        return "配らない（0 が正しい）"
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
    floor = floor_by_dow(one)
    return [*rows, *[_table_row(one, dow, slots, days, floor[dow]) for dow in DOW_TYPE_ORDER]]


def _table_row(
    one: baseline_artifact.Artifact,
    dow: str,
    slots: int,
    days: Mapping[str, int] | None,
    floor: int | None,
) -> str:
    """曜日種別 1 つぶんの行。**セルの数・割合・プロファイルの日数・期待**を並べる。"""
    targets = sorted(one.targets)
    counted = {name: counts_by_dow(one.targets[name].b2.usable)[dow] for name in targets}
    share = max(counted.values()) / slots if slots else 0.0
    cells = " | ".join(f"{counted[name]:,}" for name in targets)
    seen = None if days is None else days.get(dow, 0)
    shown = "—" if seen is None else str(seen)
    return f"| {dow} | {cells} | {share:.1%} | {shown} | {expectation(seen, floor)} |"


def verdict(one: baseline_artifact.Artifact, days: Mapping[str, int] | None) -> tuple[bool, str]:
    """合格か。**立つはずの種別にセルがあり、0 が正しい種別には 1 つも無いこと。**

    0 が正しいのは 2 通り：**配らない種別**（D-37）と、**下限に届いていない種別**。
    """
    cells, floor = fewest_cells(one), floor_by_dow(one)
    missing = [dow for dow in DOW_TYPE_ORDER if cells[dow] == 0 and _due(floor[dow], days, dow)]
    stray = [dow for dow in DOW_TYPE_ORDER if cells[dow] > 0 and _barred(floor[dow], days, dow)]
    if missing and days is None:
        return False, (
            f"**不合格**：配る種別なのにセルが 1 つも無い（{', '.join(missing)}）。"
            "プロファイルが読めないので、配る種別すべてに求めた"
        )
    if missing:
        return False, f"**不合格**：下限に届いているのにセルが無い（{_with_floor(missing, floor)}）"
    if stray:
        return False, (
            f"**不合格**：配らないか下限に届いていないのにセルがある（{_with_floor(stray, floor)}）"
            "——別のプロファイルか別の下限で作った成果物かもしれません"
        )
    waiting = [_why_zero(dow, floor[dow], days) for dow in DOW_TYPE_ORDER if cells[dow] == 0]
    note = f"（{'、'.join(waiting)}ので 0 が正しい）" if waiting else ""
    return True, f"**合格**：立つはずの曜日種別にはすべてセルがあります{note}"


def _why_zero(dow: str, floor: int | None, days: Mapping[str, int] | None) -> str:
    """0 が正しい理由。**配らないのか、下限に届いていないのか**を分けて言う。"""
    if floor is None:
        return f"{dow} は配らない"
    seen = "—" if days is None else f"{days.get(dow, 0)} 日"
    return f"{dow} は下限 {floor} 日に届いていない（{seen}）"


def _due(floor: int | None, days: Mapping[str, int] | None, dow: str) -> bool:
    """その種別に**セルが立つはずか**（配る種別で、下限に届いている）。"""
    return floor is not None and (days is None or days.get(dow, 0) >= floor)


def _barred(floor: int | None, days: Mapping[str, int] | None, dow: str) -> bool:
    """その種別に**セルがあってはいけないか**（配らない種別か、下限に届いていない）。"""
    return floor is None or (days is not None and days.get(dow, 0) < floor)


def _with_floor(dows: Sequence[str], floor: Mapping[str, int | None]) -> str:
    return ", ".join(f"{dow}：{_floor_text(floor[dow])}" for dow in dows)


def _floor_text(days: int | None) -> str:
    return "配らない" if days is None else f"下限 {days} 日"


# ── LightGBM の成果物（W6 の PR F）──────────────────────────────
def lightgbm_header(artifact: lightgbm_artifact.LightGbmArtifact, size_bytes: int) -> list[str]:
    """森そのものの素性。**読めたので、列の並び・カテゴリ・語彙はいまのコードと同じ。**"""
    days = artifact.train_days
    params = artifact.params
    return [
        f"{artifact.model_version}（書式 {artifact.format_version}、feature_set "
        f"{artifact.feature_set}、{size_bytes:,} B）",
        f"  学習 {days[0]}〜{days[-1]}（{len(days)} 日） / 作成 {artifact.created_at}",
        f"  本数 {params.get('num_boost_round', '—')}・学習率 {params.get('learning_rate', '—')}",
        f"  列 {len(artifact.columns)}（カテゴリ {len(artifact.categorical)}）："
        "並び・カテゴリ・語彙はいまのコードと一致（読めた）",
    ]


def forest_lines(artifact: lightgbm_artifact.LightGbmArtifact) -> list[str]:
    """ターゲットごとの木の本数・節の数・最大の深さ（**深さは歩く回数**。`models/forest.py`）。"""
    return [
        f"  {name} の森：木 {len(one):,} / 節 {one.n_nodes:,} / 最大の深さ {one.max_depth}"
        for name, one in sorted(artifact.forests.items())
    ]


def read_gates(source: registry.ReadsModels, model_version: str, local: str | None) -> bytes | None:
    """門の表のバイト列。**`--file` のときは `--gates` の手元のもの**（無ければ None）。"""
    if local is not None:
        return Path(local).read_bytes()
    return source.download(registry.MODEL_BUCKET, gate_tables.gates_path(model_version))


def gate_lines(table: gate_tables.GateTable) -> list[str]:
    """門の表の要約。**理由ごとの数**と、**LightGBM に回すセル**の system × ターゲット別。"""
    reasons = Counter(one.reason for one in table.cells)
    routed = Counter(
        (one.system, one.target) for one in table.cells if one.route == gate_tables.TO_LIGHTGBM
    )
    return [
        "",
        f"  門の表：検証日 {', '.join(table.evaluate_days)} / 規則 {dict(table.rule)}",
        "  セル：" + " / ".join(f"{name} {reasons[name]}" for name in sorted(gate_tables.REASONS)),
        *[
            f"  LightGBM に回すセル（{system} / {target}）：{count}"
            for (system, target), count in sorted(routed.items())
        ],
    ]


def lightgbm_verdict(
    artifact: lightgbm_artifact.LightGbmArtifact, gates_body: bytes | None, body: bytes
) -> tuple[str, gate_tables.GateTable | None]:
    """判定の文と、この森と組の門の表。**表が None なら不合格。**

    合格は、特徴量の版がいまと同じで、門の表があり、この森と組であること。
    """
    if artifact.feature_set != FEATURE_SET:
        return f"**不合格**：特徴量の版が {artifact.feature_set}（いまは {FEATURE_SET}）", None
    if gates_body is None:
        path = gate_tables.gates_path(artifact.model_version)
        return f"**不合格**：門の表がありません（{path}。合成器を組めない）", None
    try:
        table = gate_tables.from_bytes(gates_body)
        gate_tables.refuse_other_forest(table, body)
    except gate_tables.GateTableError as error:
        return f"**不合格**：門の表が使えません（{error}）", None
    return "**合格**：いまの特徴量の版で読め、門の表はこの森と組です", table


def inspect_lightgbm(source: registry.ReadsModels, body: bytes, gates: str | None) -> int:
    """LightGBM の成果物を開く。**読めなければ（照合に落ちれば）不合格**で 1 を返す。"""
    try:
        artifact = lightgbm_artifact.from_bytes(body)
    except (ValueError, lightgbm_artifact.ArtifactMismatchError) as error:
        print(f"**不合格**：いまのコードでは読めません（{error}）")
        return 1
    message, table = lightgbm_verdict(
        artifact, read_gates(source, artifact.model_version, gates), body
    )
    lines = [*lightgbm_header(artifact, len(body)), *forest_lines(artifact)]
    shown = [] if table is None else gate_lines(table)
    print("\n".join([*lines, *shown, "", message]))
    return 0 if table is not None else 1


# ── ベースラインの成果物 ──────────────────────────────────────
def inspect_baseline(source: registry.ReadsModels, body: bytes) -> int:
    """ベースラインの成果物を開く。**立つはずの曜日種別にセルがあるか**で判定する。"""
    one = baseline_artifact.from_bytes(body)
    days = profile_days(source, one.train_days[-1])
    if days is None:
        print(f"（プロファイル profiles/date={one.train_days[-1]} が読めません）", file=sys.stderr)
    passed, message = verdict(one, days)
    lines = [*header(one, len(body)), *climatology_cells(one), *floors(one), *table(one, days)]
    print("\n".join([*lines, "", message]))
    return 0 if passed else 1


# ── 取る ──────────────────────────────────────────────────────
def kind_of(body: bytes) -> str:
    """手元の成果物の種類。**LightGBM の成果物は中に `kind` を持つ**（ベースラインは持たない）。"""
    document = json.loads(gzip.decompress(body).decode())
    found = document.get("kind") if isinstance(document, dict) else None
    return str(found) if found else registry.BASELINE_KIND


def fetch(source: registry.ReadsModels, options: argparse.Namespace) -> tuple[str, bytes]:
    """成果物の種類とバイト列を取る。**手元のファイルか、Storage の版か。**

    登録簿から引いたときは、**行の `kind` と中身の `kind` が食い違えば止める**（登録の誤り）。
    """
    if options.file:
        body = Path(options.file).read_bytes()
        return kind_of(body), body
    wanted = options.version
    found = registry.named(source, wanted) if wanted else registry.active(source)
    print(f"（登録簿：{found.model_version} / {found.kind} / {found.status}）", file=sys.stderr)
    downloaded = source.download(registry.MODEL_BUCKET, found.artifact_path)
    if downloaded is None:
        raise SystemExit(f"成果物が Storage にありません: {found.artifact_path}")
    if kind_of(downloaded) != found.kind:
        raise SystemExit(f"登録簿は {found.kind}、成果物は {kind_of(downloaded)} と言っています")
    return found.kind, downloaded


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="配っている成果物を開いて確かめる")
    parser.add_argument("--version", default=None, help="登録簿の版（既定は active）")
    parser.add_argument("--file", default=None, help="Storage の代わりに読む成果物")
    parser.add_argument(
        "--gates",
        default=None,
        help="手元の LightGBM の門の表（無ければ Storage の lightgbm/<版>.gates.json.gz を読む）",
    )
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    options = _arguments(argv)
    with open_storage(read_storage_config()) as source:
        kind, body = fetch(source, options)
        if kind == registry.LIGHTGBM_KIND:
            return inspect_lightgbm(source, body, options.gates)
        return inspect_baseline(source, body)


if __name__ == "__main__":
    raise SystemExit(run())
