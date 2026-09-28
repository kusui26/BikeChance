"""下限の候補を 1 日の行で測り、W6 プラン §6.1 の基準で土日祝の下限 K を選ぶ（PR A、D-37）。

**基準はここに書かない。** 判定は `eval/b2_effect.py`（プランの基準をそのまま写したもの）が
持ち、このファイルは「読む」「並べる」「書き出す」だけを持つ（CLAUDE.md §3）。**読むだけで、
Storage にも DB にも何も書かない。**

使い方（9/28 の朝。候補の版は `fit_baseline --weekend-days k --out` で先に作っておく）:

    set -a; . ./.env; set +a
    cd apps/ml
    ./.venv/bin/python -m bikechance_ml.jobs.measure_floor --day 2026-09-27 --dow sun_holiday \\
        --candidate 3=.cache/floor/k3.json.gz --candidate 4=.cache/floor/k4.json.gz \\
        --candidate 5=.cache/floor/k5.json.gz --reference-version baseline-b3-v0-20260923

**候補の厚さを先に確かめる。** 候補 k は、`--dow` の配る側の下限が k で、その曜日種別の
厚さ（`b2_max_days`）もちょうど k 日でなければ止める——違う版で測って K を決めない。
`--dow all` は行を絞らない（EDA #8 の再現に使う。厚さは確かめない）。

**参考の版だけでも測れる**（候補を渡さなければ K は選ばない）。配っている版を同じ物差しで
測り直すとき（W6 プラン §13.7 の再現、§6.1 の「その後」）に使う:

    … --day 2026-09-24 --dow weekday --reference-version baseline-b3-v0-20260923
"""

import argparse
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq

from bikechance_ml.baselines.artifact import Artifact, from_bytes
from bikechance_ml.config import read_storage_config
from bikechance_ml.eval import b2_effect
from bikechance_ml.eval.b2_effect import Effect
from bikechance_ml.features.calendar import DOW_TYPE_ORDER
from bikechance_ml.features.grid import JST, features_path
from bikechance_ml.io.supabase import SupabaseIo, open_storage
from bikechance_ml.jobs import climate
from bikechance_ml.models import registry

#: 行を曜日種別で絞らないときの `--dow`。
ALL_DOW_TYPES: Final[str] = "all"


class InputError(ValueError):
    """測る材料が揃っていない・形が違う。**揃わないまま K を出さない。**"""


@dataclass(frozen=True)
class Measured:
    """1 回の測りの全部。**報告書はこれだけから書く。**"""

    day: date
    dow: str
    effects: Mapping[int, Effect]
    #: 候補の見出し（版の名前と、読んだファイル）。**同じ学習窓なら版の名前が重なる**
    #: （k = 5 と配信中の `…0923`）ので、名前だけでは見分けられない
    names: Mapping[int, str]
    #: 参考の版の見出しと測り
    reference: tuple[str, Effect] | None
    chosen: int | None


def parse_candidates(values: Sequence[str]) -> dict[int, Path]:
    """`k=path` を並べたもの。**同じ k は 2 度渡せない。** 無ければ空（参考の版だけを測る）。"""
    found: dict[int, Path] = {}
    for value in values:
        days, _, path = value.partition("=")
        if not days.isdigit() or not path:
            raise InputError(f"--candidate は k=パス の形で渡す（{value}）")
        if int(days) in found:
            raise InputError(f"--candidate の k = {days} が 2 度ある")
        found[int(days)] = Path(path)
    return found


def check_candidates(candidates: Mapping[int, Artifact], dow: str) -> None:
    """**候補の版が本当に k 日の版か**を、行を読む前に確かめる（`--dow all` は確かめない）。"""
    if dow == ALL_DOW_TYPES:
        return
    for days, one in sorted(candidates.items()):
        wrong = b2_effect.check_candidate(one, days, dow)
        if wrong is not None:
            raise InputError(f"候補の版が k = {days} になっていない：{wrong}")


def rows_to_measure(table: pa.Table, dow: str, day: date) -> pa.Table:
    """測る行。**候補も参考も、この同じ行に当てる。** 0 行なら K を出さない。"""
    rows = table if dow == ALL_DOW_TYPES else b2_effect.arriving_on(table, dow)
    if rows.num_rows == 0:
        raise InputError(f"{day} の行のうち、到着が {dow} の行が 1 つも無い")
    return rows


def measure_each(artifacts: Mapping[int, Artifact], rows: pa.Table, day: date) -> dict[int, Effect]:
    """候補を k の昇順に、同じ行へ当てる。"""
    return {
        days: b2_effect.measure(one, rows, noon_of(day)) for days, one in sorted(artifacts.items())
    }


def noon_of(day: date) -> datetime:
    """`to_samples` に渡す基準の時刻。**値には効かない**（B1・B2・B3 は日を読まない）。"""
    return datetime.combine(day, time(hour=12), tzinfo=JST)


def render(measured: Measured) -> str:
    """報告書（Markdown）。**全体の表 → K → 組ごとの表**の順。"""
    lines = [
        f"# B2 の下限の測り（{measured.day}、到着が {measured.dow} の行）",
        "",
        "基準は W6 プラン §6.1（2026-09-25 に、9/27 のデータを見る前に書いた）。",
        "",
        "| 版 | 行 × ターゲット | B2 が引けた行 | 落とすと（全体） | 95% 区間 "
        "| 落とした確率以下の組 | B1 との差 | 効いた |",
        "|---|---:|---:|---:|---|---:|---:|---|",
        *[
            _row(f"k = {days}（{measured.names[days]}）", one)
            for days, one in measured.effects.items()
        ],
    ]
    if measured.reference is not None:
        name, one = measured.reference
        lines.append(_row(f"参考：{name}", one, judged=False))
    return "\n".join([*lines, "", _verdict(measured), "", *_pair_table(measured)]) + "\n"


def _row(label: str, one: Effect, *, judged: bool = True) -> str:
    rows = sum(pair.rows for pair in one.pairs)
    used = sum(pair.used * pair.rows for pair in one.pairs) / rows if rows else 0.0
    helped = ("○" if one.helped else "×") if judged else "（判定に使わない）"
    return (
        f"| {label} | {rows:,} | {used:.1%} | {one.gain:+.2%} | [{one.low:+.2%}, {one.high:+.2%}] "
        f"| {one.pairs_not_worse} / {len(one.pairs)} | {one.versus_b1:+.2%} | {helped} |"
    )


def _verdict(measured: Measured) -> str:
    if not measured.effects:
        return "（候補が無いので K は選ばない。参考の版を測っただけ）"
    if measured.chosen is None:
        return "**K ＝ 配らない**（5 日まで含めて、k 以上がすべて効いた k が無い。W6 プラン §6.1）"
    return f"**K ＝ {measured.chosen}**（k 以上がすべて効いた最小の k。W6 プラン §6.1）"


def _pair_table(measured: Measured) -> list[str]:
    """組ごとの表。**ECE は判定に使わず、並べるだけ**（Brier が較正の項を含む）。"""
    lines = [
        "## 組ごと",
        "",
        "| 版 | system | ターゲット | 行 | B1 | B3 | 落とした | 落とすと "
        "| ECE（B3） | ECE（落とした） |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    labelled = [(f"k = {days}", one) for days, one in measured.effects.items()]
    if measured.reference is not None:
        labelled.append((f"参考：{measured.reference[0]}", measured.reference[1]))
    for label, one in labelled:
        lines.extend(
            f"| {label} | {pair.system} | {pair.target} | {pair.rows:,} | {pair.b1:.5f} "
            f"| {pair.b3:.5f} | {pair.dropped:.5f} | {pair.gain:+.2%} | {pair.ece_b3:.4f} "
            f"| {pair.ece_dropped:.4f} |"
            for pair in one.pairs
        )
    return lines


# ── 読む ──────────────────────────────────────────────────────
def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="下限の候補を 1 日の行で測り、K を選ぶ")
    parser.add_argument("--day", required=True, help="測る日（JST の暦日。学習サンプルの日）")
    parser.add_argument(
        "--dow", required=True, choices=(*DOW_TYPE_ORDER, ALL_DOW_TYPES), help="到着の曜日種別"
    )
    parser.add_argument("--candidate", action="append", default=[], help="k=成果物（何度でも）")
    reference = parser.add_mutually_exclusive_group()
    reference.add_argument("--reference-version", default=None, help="参考に並べる登録簿の版")
    reference.add_argument("--reference-file", default=None, help="参考に並べる手元の成果物")
    parser.add_argument("--local", default=None, help="学習サンプルを Storage の代わりに読む場所")
    parser.add_argument("--out", default=None, help="報告書の書き出し先（省略すると標準出力）")
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    options = _arguments(argv)
    try:
        measured = _measure(options)
    except (InputError, registry.UnknownModelError, registry.MissingArtifactError) as error:
        print(f"測れません: {error}", file=sys.stderr)
        return 2
    text = render(measured)
    if options.out is None:
        print(text)
    else:
        Path(options.out).write_text(text, encoding="utf-8")
        print(f"書き出しました: {options.out}", file=sys.stderr)
    print(_verdict(measured), file=sys.stderr)
    return 0


def _measure(options: argparse.Namespace) -> Measured:
    """候補を読み、**厚さを確かめてから**行を読み、同じ行に候補と参考を当てる。"""
    day = date.fromisoformat(options.day)
    paths = parse_candidates(options.candidate)
    candidates = {k: _read_file(path) for k, path in paths.items()}
    if not candidates and options.reference_file is None and options.reference_version is None:
        raise InputError("候補（--candidate）も参考の版も無い——測るものが無い")
    check_candidates(candidates, options.dow)
    table, reference = _read_inputs(options, day)
    rows = rows_to_measure(table, options.dow, day)
    effects = measure_each(candidates, rows, day)
    return Measured(
        day=day,
        dow=options.dow,
        effects=effects,
        names={k: f"{one.model_version}、{paths[k].name}" for k, one in candidates.items()},
        reference=_measure_reference(reference, rows, day),
        chosen=b2_effect.choose_floor(effects),
    )


def _read_inputs(
    options: argparse.Namespace, day: date
) -> tuple[pa.Table, tuple[str, Artifact] | None]:
    """学習サンプルの 1 日と参考の版。**手元だけで足りるなら Storage を開かない。**"""
    local = Path(options.local) if options.local else None
    if local is not None and options.reference_version is None:
        return _read_features(None, day, local), _reference(None, options)
    with open_storage(read_storage_config()) as source:
        return _read_features(source, day, local), _reference(source, options)


def _measure_reference(
    reference: tuple[str, Artifact] | None, rows: pa.Table, day: date
) -> tuple[str, Effect] | None:
    """参考の版（配信中の版など）。**候補と同じ行に当てる**が、厚さは確かめない（判定に使わない）。"""
    if reference is None:
        return None
    label, artifact = reference
    return label, b2_effect.measure(artifact, rows, noon_of(day))


def _read_features(source: SupabaseIo | None, day: date, local: Path | None) -> pa.Table:
    body = climate.read_bytes(source, features_path(day), local)
    if body is None:
        raise InputError(f"{day} の学習サンプルがまだ無い（build_features の後に測る）")
    return pq.read_table(pa.BufferReader(body))


def _reference(
    source: SupabaseIo | None, options: argparse.Namespace
) -> tuple[str, Artifact] | None:
    """参考の版と見出し。**どこから読んだか**（ファイルか、登録簿のどの状態か）も添える。"""
    if options.reference_file:
        path = Path(options.reference_file)
        one = _read_file(path)
        return f"{one.model_version}（{path.name}）", one
    if options.reference_version is None or source is None:
        return None
    found = registry.named(source, options.reference_version)
    body = source.download(registry.MODEL_BUCKET, found.artifact_path)
    if body is None:
        raise registry.MissingArtifactError(f"成果物がありません: {found.artifact_path}")
    return f"{found.model_version}（登録簿の {found.status}）", from_bytes(body)


def _read_file(path: Path) -> Artifact:
    if not path.exists():
        raise InputError(f"成果物が無い: {path}")
    return from_bytes(path.read_bytes())


if __name__ == "__main__":
    raise SystemExit(run())
