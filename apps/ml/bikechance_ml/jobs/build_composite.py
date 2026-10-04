"""合成器を組んで置く（W6 の PR H、W6 プラン §6.8・§8.9）。

**当てはめない。** 置いてある 3 つの部品——LightGBM の森と門の表（`fit_lightgbm` が置いた）、
B3（`fit_baseline --upload` が置いた。§8.9 の手順 1）——を読み、1 つの成果物にする
（`models/composite.py`）。**部品は置いてあるバイト列のまま入れる**ので、合成器の B3 は
M4 の後 1 週間 shadow に置く B3 と同じものになる（W6-25）。

**shadow で悪かったセルを削れる**（`--drop-file`、W6-12）。削れるのは LightGBM に行くセル
だけで、足すことはできない（`models/composite.py` が止める）。

**境目の差を測って出す**（§5.10、完了条件 3）：B3 の最終学習日（`--measure-day` で変えられる）の
学習サンプルで、森を歩いた行の |P_LGBM − P_B3| と、隣のセルが B3 の行の分布。

使い方（環境変数は `.env` から読み込んでから）:

    set -a; . ./.env; set +a
    cd apps/ml
    ./.venv/bin/python -m bikechance_ml.jobs.build_composite \\
        --lightgbm lgbm-v1-20261008 --b3 baseline-b3-v0-20261012 \\
        --drop-file .cache/composite/drop.txt \\
        --out .cache/composite/{version}.json.gz \\
        --card ../../docs/model_cards/{version}.md --upload --register

**置くのも登録するのも明示したときだけ**（`--upload`・`--register`）。登録には `--upload` と
カード（`--card`）が要り、**境目の差を測っていない合成器は登録しない**。**配信中（active・shadow）
の名前では置かない**（契約 38）。昇格は人が `promote_model_version` で行う（契約 39）。

`--drop-file` は 1 行 1 セル（`hellocycling bike 30 0`。`#` から後は注記）。`--local` の下は
Storage のパスをそのまま並べたもの（手元で組むときと検査。Storage には触らない）。
"""

import argparse
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Final, Protocol

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from bikechance_ml.baselines import artifact as baseline_artifact
from bikechance_ml.config import read_storage_config
from bikechance_ml.eval import border
from bikechance_ml.eval.dataset import BUCKET_LABELS, TARGETS
from bikechance_ml.features.constants import HORIZONS_MIN
from bikechance_ml.features.grid import JST, features_path
from bikechance_ml.io.supabase import PARQUET_BUCKET, open_storage
from bikechance_ml.jobs.cards import LABEL_PATTERN, card_reference, expand
from bikechance_ml.models import artifact as lightgbm_artifact
from bikechance_ml.models import composite, registry
from bikechance_ml.models import gates as gate_tables
from bikechance_ml.models.gates import CellKey

#: 境目の差を測る学習サンプルの基準時刻（その日の正午 JST）。B3 は日付しか読まない。
MEASURE_TIME: Final[time] = time(12, 0)

#: 削るセルの 1 行の形（`系統 ターゲット 水平 バケツ`）。
DROP_FIELDS: Final[int] = 4


class OptionError(ValueError):
    """通らない指定。**当てはめも置くこともしない。**"""


class MissingPartError(RuntimeError):
    """部品が置かれていない。**代わりをでっち上げない。**"""


class ComposePort(Protocol):
    """入出力の口。**部品と学習サンプルを読み、合成器を置いて登録する**（`SupabaseIo`）。"""

    def download(self, bucket: str, path: str) -> bytes | None: ...
    def find_model(self, model_version: str) -> registry.Registered | None: ...
    def upload(self, bucket: str, path: str, body: bytes, content_type: str) -> None: ...
    def register_model_version(self, row: Mapping[str, object]) -> str: ...


@dataclass(frozen=True)
class Request:
    """1 回の組み立ての指定（`_arguments` を確かめたもの）。"""

    lightgbm: str
    b3: str
    label: str | None
    dropped: tuple[CellKey, ...]
    measure_day: date | None
    skip_border: bool
    out: str | None
    card: str | None
    upload: bool
    register: bool
    local: Path | None


@dataclass(frozen=True)
class Made:
    """組んだ合成器と、そのバイト列と、境目の差（測らなかったら None）。"""

    artifact: composite.CompositeArtifact
    body: bytes
    border: border.Border | None

    @property
    def version(self) -> str:
        return self.artifact.model_version


# ── 指定 ──────────────────────────────────────────────────────
def parse_drops(lines: Iterable[str]) -> tuple[CellKey, ...]:
    """削るセルの並び（1 行 1 セル、`#` から後は注記）。**重複は 1 つにして昇順に並べる。**"""
    cells: set[CellKey] = set()
    for number, line in enumerate(lines, start=1):
        text = line.split("#", 1)[0].strip()
        if text:
            cells.add(_drop_cell(text, number))
    return tuple(sorted(cells))


def _drop_cell(text: str, number: int) -> CellKey:
    fields = text.split()
    if len(fields) != DROP_FIELDS:
        raise OptionError(
            f"--drop-file の {number} 行目は「系統 ターゲット 水平 バケツ」の 4 つです"
        )
    system, target, horizon, bucket = fields
    known = {one.name for one in TARGETS}
    if target not in known or not horizon.isdigit() or int(horizon) not in HORIZONS_MIN:
        raise OptionError(f"--drop-file の {number} 行目のターゲットか水平が違います: {text}")
    if bucket not in BUCKET_LABELS:
        raise OptionError(f"--drop-file の {number} 行目のバケツが違います: {text}")
    return (system, target, int(horizon), bucket)


def request_of(options: argparse.Namespace) -> Request:
    """指定を確かめて、1 回の組み立ての形にする。"""
    refuse_bad_options(options)
    drops = Path(options.drop_file).read_text().splitlines() if options.drop_file else []
    return Request(
        lightgbm=options.lightgbm,
        b3=options.b3,
        label=options.label,
        dropped=parse_drops(drops),
        measure_day=date.fromisoformat(options.measure_day) if options.measure_day else None,
        skip_border=options.skip_border,
        out=options.out,
        card=options.card,
        upload=options.upload,
        register=options.register,
        local=Path(options.local) if options.local else None,
    )


def refuse_bad_options(options: argparse.Namespace) -> None:
    """**登録はカードと境目の差を伴う**（CLAUDE.md §6、完了条件 3）。印は `lgbm-v1` と同じ規則。"""
    if options.label is not None and LABEL_PATTERN.match(options.label) is None:
        raise OptionError(
            f"--label は英小文字で始まる英小文字と数字（20 字まで）です: {options.label!r}"
        )
    if options.register and not (options.upload and options.card):
        raise OptionError(
            "--register には --upload と --card が要ります（カードの無い版を登録しない）"
        )
    if options.register and options.skip_border:
        raise OptionError(
            "--register するなら境目の差を測ります（--skip-border は手元の確かめだけ）"
        )


# ── 組む ──────────────────────────────────────────────────────
def read_object(source: ComposePort, local: Path | None, bucket: str, path: str) -> bytes:
    """1 つ読む。`--local` なら手元のその場所だけを見る（**Storage に触らない**）。"""
    if local is not None:
        found = local / path
        body = found.read_bytes() if found.exists() else None
    else:
        body = source.download(bucket, path)
    if body is None:
        raise MissingPartError(f"置かれていません: {bucket}/{path}")
    return body


def read_parts(source: ComposePort, request: Request) -> dict[str, bytes]:
    """3 つの部品のバイト列（**置いてあったまま**）。"""
    paths = {
        composite.B3_PART: baseline_artifact.artifact_path(request.b3),
        composite.LIGHTGBM_PART: lightgbm_artifact.artifact_path(request.lightgbm),
        composite.GATES_PART: gate_tables.gates_path(request.lightgbm),
    }
    return {
        name: read_object(source, request.local, registry.MODEL_BUCKET, path)
        for name, path in paths.items()
    }


def make(source: ComposePort, request: Request, now: datetime) -> Made:
    """読んで組み、境目の差を測る。**置けない名前なら測る前に止める**（契約 38）。"""
    made = composite.assemble(
        read_parts(source, request),
        created_at=now.isoformat(),
        dropped=request.dropped,
        label=request.label,
    )
    _refuse_other_names(made, request)
    if request.upload:
        registry.refuse_serving(source, made.model_version)
    measured = None if request.skip_border else measure_border(source, request, made)
    return Made(artifact=made, body=composite.to_bytes(made), border=measured)


def _refuse_other_names(made: composite.CompositeArtifact, request: Request) -> None:
    """**ファイルの中の版の名前が、指定と同じか**（置き間違えた部品を使わない）。"""
    found = (made.b3.model_version, made.lightgbm.model_version)
    if found != (request.b3, request.lightgbm):
        raise MissingPartError(f"部品の中の版が指定と違います: B3 {found[0]}・LightGBM {found[1]}")


def measure_border(
    source: ComposePort, request: Request, made: composite.CompositeArtifact
) -> border.Border:
    """境目の差（§5.10）。**B3 の最終学習日**の学習サンプルで、両系統の全行に出す。"""
    day = request.measure_day or date.fromisoformat(max(made.b3.train_days))
    body = read_object(source, request.local, PARQUET_BUCKET, features_path(day))
    table = pq.read_table(pa.BufferReader(body))
    tables = {
        system_id: table.filter(pc.equal(table.column("system_id"), system_id))
        for system_id in made.b3.systems
    }
    return border.measure(made, datetime.combine(day, MEASURE_TIME, tzinfo=JST), tables)


# ── 書く・置く ────────────────────────────────────────────────
def write_outputs(request: Request, made: Made) -> None:
    """手元に書く（合成器とカード）。**登録より先に書く**——カードの無い版を登録しない。"""
    _write(expand(request.out, made.version), made.body, "合成器")
    _write(expand(request.card, made.version), render_card(made).encode(), "モデルカード")


def _write(path: str | None, body: bytes, label: str) -> None:
    if path is None:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    print(f"{label}を書きました: {target}", file=sys.stderr)


def publish(source: ComposePort, request: Request, made: Made) -> None:
    """置いて登録する。**置く直前にもう 1 度登録簿を引く**（`registry.upload_artifact`）。"""
    if not request.upload:
        return
    path = composite.artifact_path(made.version)
    registry.upload_artifact(source, made.version, path, made.body, composite.CONTENT_TYPE)
    if request.register:
        source.register_model_version(to_registration(made, expand(request.card, made.version)))


def to_registration(made: Made, card_path: str | None) -> dict[str, object]:
    """`register_model_version` に渡す形。**`candidate` としてしか登録できない**（0038）。"""
    artifact = made.artifact
    return {
        "model_version": made.version,
        "kind": composite.KIND,
        "status": "candidate",
        "feature_set": artifact.feature_set,
        "artifact_path": composite.artifact_path(made.version),
        "train_days": list(artifact.train_days),
        "metrics": to_metrics(made),
        "card_path": card_reference(card_path),
        "note": (
            f"W6 の PR H。B3 {artifact.b3.model_version} と LightGBM "
            f"{artifact.lightgbm.model_version} の合成器（D-33）"
        ),
    }


def to_metrics(made: Made) -> dict[str, object]:
    """登録簿の `metrics`。**部品の版と SHA-256・門のセルの数・削ったセル・境目の差。**"""
    artifact = made.artifact
    return {
        "format_version": artifact.format_version,
        "sha256": composite.sha256_of(made.body),
        "bytes": len(made.body),
        "components": _components(artifact),
        "cells": _cell_counts(artifact),
        "dropped": [list(one) for one in artifact.dropped],
        "border": None if made.border is None else made.border.as_json(),
    }


def _components(artifact: composite.CompositeArtifact) -> dict[str, dict[str, object]]:
    days = {
        composite.B3_PART: artifact.b3.train_days,
        composite.LIGHTGBM_PART: artifact.lightgbm.train_days,
    }
    versions = {
        composite.B3_PART: artifact.b3.model_version,
        composite.LIGHTGBM_PART: artifact.lightgbm.model_version,
        composite.GATES_PART: artifact.gates.model_version,
    }
    return {
        name: {
            "model_version": versions[name],
            "sha256": composite.sha256_of(body),
            "train_days": [min(days[name]), max(days[name])] if name in days else None,
        }
        for name, body in artifact.parts.items()
    }


def _cell_counts(artifact: composite.CompositeArtifact) -> dict[str, dict[str, int]]:
    """系統 × ターゲットごとの、森を歩くセルの数と門の表に載ったセルの数。"""
    walking = artifact.lightgbm_cells()
    counts: dict[str, dict[str, int]] = {}
    for one in artifact.gates.cells:
        group = counts.setdefault(f"{one.system}/{one.target}", {"lightgbm": 0, "table": 0})
        group["table"] += 1
        group["lightgbm"] += int(one.key in walking)
    return dict(sorted(counts.items()))


# ── カードと報告 ──────────────────────────────────────────────
def render_card(made: Made) -> str:
    """モデルカード（**登録する版には必ず付ける**。CLAUDE.md §6、`docs/model_cards/README.md`）。"""
    sections = [
        _card_header(made),
        _card_parts(made.artifact),
        _card_cells(made.artifact),
        _card_border(made.border),
        _card_serving(made.artifact),
    ]
    return "\n\n".join(sections) + "\n"


def _card_header(made: Made) -> str:
    artifact = made.artifact
    return "\n".join(
        [
            f"# {made.version}（合成器）",
            "",
            f"- 作った時刻：{artifact.created_at}（書式 {artifact.format_version}、"
            f"{len(made.body):,} バイト、SHA-256 `{composite.sha256_of(made.body)}`）",
            f"- 特徴量の版：{artifact.feature_set}"
            "（**森が全部の列を読むので、配信の版と一致しなければ配らない**）",
            f"- 学習日（和集合）：{artifact.train_days[0]} 〜 {artifact.train_days[-1]}"
            f"（{len(artifact.train_days)} 日）。**鮮度の警報は B3 の最終学習日で鳴る**（W6-08）",
            f"- LightGBM の最終学習日：{artifact.lightgbm_last_day}（警報では見ない。§5.12）",
        ]
    )


def _card_parts(artifact: composite.CompositeArtifact) -> str:
    rows = ["## 部品", "", "| 部品 | 版 | SHA-256 | 学習日 |", "|---|---|---|---|"]
    for name, item in _components(artifact).items():
        days = item["train_days"]
        span = f"{days[0]} 〜 {days[1]}" if isinstance(days, list) else "—"
        rows.append(f"| {name} | {item['model_version']} | `{item['sha256']}` | {span} |")
    return "\n".join(rows)


def _card_cells(artifact: composite.CompositeArtifact) -> str:
    rows = [
        "## 門（セル単位。W6-09、契約 35）",
        "",
        "| 系統/ターゲット | 森 | 表のセル |",
        "|---|---:|---:|",
    ]
    rows += [
        f"| {group} | {one['lightgbm']} | {one['table']} |"
        for group, one in _cell_counts(artifact).items()
    ]
    rows += [
        "",
        f"門の表は {artifact.gates.model_version} の検証日"
        f"（{'・'.join(artifact.gates.evaluate_days)}）で決めたもの。",
    ]
    dropped = [" ".join(str(part) for part in one) for one in artifact.dropped]
    rows.append("shadow で削ったセル（W6-12）：" + ("、".join(dropped) if dropped else "なし"))
    return "\n".join(rows)


def _card_border(measured: border.Border | None) -> str:
    rows = ["## 境目の差（|P_LGBM − P_B3|。W6 プラン §5.10）", ""]
    if measured is None:
        return "\n".join([*rows, "**測っていない**（`--skip-border`。この版は登録しない）"])
    rows += ["| 行 | 数 | 中央 | 90% | 99% | 最大 |", "|---|---:|---:|---:|---:|---:|"]
    for label, one in (
        ("森を歩いた行", measured.walked),
        ("水平の隣が B3", measured.horizon_edge),
        ("バケツの隣が B3", measured.bucket_edge),
    ):
        values = " | ".join(_number(value) for value in (one.p50, one.p90, one.p99, one.largest))
        rows.append(f"| {label} | {one.rows:,} | {values} |")
    return "\n".join(rows)


def _number(value: float | None) -> str:
    return "—" if value is None else f"{value:.4f}"


def _card_serving(artifact: composite.CompositeArtifact) -> str:
    return "\n".join(
        [
            "## 配り方",
            "",
            f"- 全行で B3（{artifact.b3.model_version}）を出し、**門で選んだセルの行だけ** LightGBM"
            f"（{artifact.lightgbm.model_version}）の森を歩く。"
            "セルごとに部品とビット単位で同じ確率になる",
            "- **プロファイルを読めなかった周期は全セル B3**（契約 33）。"
            "記録の `detail.composite.route` が `b3_only` になる",
            "- 確度（W6-11）：森の行は目標セルの `prof_n_days` が配る側の下限"
            "（平日 3・土日祝 4）以上なら 3、B3 の行は B3 の気候値が引けたか",
            f"- 確かめ方：`scripts/inspect-artifact.py --version {artifact.model_version}`、"
            f"試し打ちは `/ml/infer?model={artifact.model_version}`",
        ]
    )


def describe(made: Made) -> str:
    """標準出力に出す要約。"""
    lines = [
        made.artifact.describe(),
        f"パス {composite.artifact_path(made.version)}（{len(made.body):,} バイト）",
    ]
    if made.border is not None:
        walked = made.border.walked
        median, largest = _number(walked.p50), _number(walked.largest)
        lines.append(f"境目の差（森の行 {walked.rows:,}）：中央 {median}・最大 {largest}")
    return "\n".join(lines)


# ── 実行 ──────────────────────────────────────────────────────
def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="合成器を組んで置く（W6 の PR H）")
    parser.add_argument("--lightgbm", required=True, help="LightGBM の版（森と門の表）")
    parser.add_argument("--b3", required=True, help="B3 の版（fit_baseline --upload が置いたもの）")
    parser.add_argument("--label", default=None, help="版の名前に挟む印（予行演習は rehearsal）")
    parser.add_argument("--drop-file", default=None, help="shadow で削るセル（1 行 1 セル）")
    parser.add_argument(
        "--measure-day", default=None, help="境目の差を測る日（既定は B3 の最終学習日）"
    )
    parser.add_argument(
        "--skip-border", action="store_true", help="境目の差を測らない（登録できない）"
    )
    parser.add_argument("--out", default=None, help="合成器の書き出し先（{version} が使える）")
    parser.add_argument(
        "--card", default=None, help="モデルカードの書き出し先（{version} が使える）"
    )
    parser.add_argument("--upload", action="store_true", help=registry.UPLOAD_HELP)
    parser.add_argument("--register", action="store_true", help="candidate として登録する")
    parser.add_argument("--local", default=None, help="Storage のパスを並べた手元の置き場")
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    try:
        request = request_of(_arguments(argv))
    except OptionError as error:
        print(f"指定が違います: {error}", file=sys.stderr)
        return 2
    with open_storage(read_storage_config()) as source:
        made = make(source, request, datetime.now(UTC))
        write_outputs(request, made)
        publish(source, request, made)
    print(describe(made))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
