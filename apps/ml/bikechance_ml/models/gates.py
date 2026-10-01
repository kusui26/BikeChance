"""門の表（W6 の PR F が書き、PR H の合成器の組み立てが読む。W6-09、契約 35）。

**門は当てはめのときに決め、コードに書かない。** セル（system × ターゲット × 水平 × 台数
バケツ）ごとに、LightGBM を配るか B3 を配るかを、**検証日で測った結果から**決めて表にする。
合成器はこの表をそのまま成果物に焼き付ける（W6-08）。

**形はここ 1 か所で決める。** 書く側（`jobs/fit_lightgbm.py`）と読む側（合成器の組み立て）が
同じ `to_bytes` と `from_bytes` を通るので、**片方だけ形を変える道が無い。**

**読むときに照合する。** 表は「当てはめたときのセルの切り方」で決めたものなので、水平の並びと
台数バケツの境目が**いまのコードと同じでなければ使わない**——境目が 1 つずれると、同じ名前の
セルが別の行を指し、**例外を出さずに別のところで LightGBM を配る。**

**表に無いセルは B3 を配る。** 検証日に 1 行も無かったセルは判定できないので、載せない。

**表は、決めたときの森と組でしか使わない。** 版の名前だけでは組が決まらない——candidate の
名前は置き直せるので（W6-19）、森だけ置き直して表の置き直しが落ちると、**同じ名前の下で
新しい森と古い表が組になる**。だから表に**森の成果物のバイト列の SHA-256** を焼き付け、
読む側が森と突き合わせる（`refuse_other_forest`）。

**置き場所**は LightGBM の成果物の隣（`lightgbm/<版>.gates.json.gz`）。`models` バケットは
gzip しか受けないので、中身は gzip した JSON にする（成果物と同じ）。
"""

import gzip
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final

from bikechance_ml.eval import gates as judging
from bikechance_ml.eval.dataset import BUCKET_EDGES, BUCKET_LABELS, TARGETS
from bikechance_ml.features.constants import HORIZONS_MIN

#: 表の書式の版。**読み方を変えたら上げる。**
FORMAT_VERSION: Final[int] = 1

#: 中身の種類（成果物と取り違えないための印）。
KIND: Final[str] = "lightgbm-gates"

#: `models` バケットの許可リストと揃える（0027）。
CONTENT_TYPE: Final[str] = "application/gzip"

#: 行き先。**LightGBM か B3 の 2 つだけ**（判定の対象外と 10% に届かないセルは B3）。
TO_LIGHTGBM: Final[str] = "lightgbm"
TO_B3: Final[str] = "b3"

#: 理由（**正は `eval/gates.py`**）。**`passed` だけが LightGBM に行く。**
PASSED: Final[str] = judging.PASSED
REASONS: Final[frozenset[str]] = frozenset({judging.PASSED, judging.BELOW, judging.EXCLUDED})

#: 数字を丸める桁。**同じ当てはめからは同じバイト列が出る**ようにする。
DIGITS: Final[int] = 8

#: SHA-256 の書き方（小文字の 16 進 64 桁。`hashlib` の `hexdigest` と同じ）。
_SHA256: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-f]{64}\Z")


class GateTableError(ValueError):
    """門の表が読めない、またはいまのコードのセルの切り方と食い違う。**使わない。**"""


#: セルの鍵（`system, ターゲット, 水平, バケツ`）。
type CellKey = tuple[str, str, int, str]


@dataclass(frozen=True)
class GateCell:
    """1 つのセルの判定と、**その根拠の数字**（重み付き Brier）。"""

    system: str
    target: str
    h_min: int
    bucket: str
    route: str
    reason: str
    n: int
    b0: float
    b3: float
    lgbm: float
    #: B3 に対する相対改善。**B3 の Brier が 0 なら None**
    improvement: float | None

    @property
    def key(self) -> CellKey:
        return (self.system, self.target, self.h_min, self.bucket)


@dataclass(frozen=True)
class GateTable:
    """1 つの LightGBM の版の門の表。"""

    format_version: int
    #: この表を決めた LightGBM の版（**その版の森と組でしか使わない**）
    model_version: str
    #: その森の成果物（`lightgbm/<版>.json.gz`）のバイト列の SHA-256。**読む側が森と突き合わせる**
    artifact_sha256: str
    #: 判定の規則（通る改善の下限・判定の対象にする B0 の Brier の下限・相手）
    rule: Mapping[str, object]
    #: 門を決めた検証日（JST の暦日）
    evaluate_days: tuple[str, ...]
    horizons_min: tuple[int, ...]
    bucket_labels: tuple[str, ...]
    bucket_edges: tuple[int, ...]
    cells: tuple[GateCell, ...]

    def lightgbm_cells(self) -> frozenset[CellKey]:
        """**LightGBM を配るセル。** ここに無いセルは B3 を配る。"""
        return frozenset(one.key for one in self.cells if one.route == TO_LIGHTGBM)


def gates_path(model_version: str) -> str:
    """`models` バケット内のパス。**LightGBM の成果物（`lightgbm/<版>.json.gz`）の隣。**"""
    return f"lightgbm/{model_version}.gates.json.gz"


def rule() -> dict[str, object]:
    """表に焼き付ける判定の規則。**値の正は `eval/gates.py`**（ここで数字を書かない）。"""
    return {
        "unit": judging.CELL_GATE,
        "reference": judging.REFERENCE,
        "min_improvement": judging.MIN_IMPROVEMENT,
        "judgeable_brier": judging.JUDGEABLE_BRIER,
    }


def from_judged(
    judged: judging.Judged,
    model_version: str,
    artifact_sha256: str,
    evaluate_days: Sequence[str],
) -> GateTable:
    """門を当てた結果（**セル単位**、検証日ぜんぶで決めたもの）から表を組む。"""
    cells = [cell_of(one) for one in judged.cells]
    return build(model_version, artifact_sha256, cells, evaluate_days, rule())


def cell_of(one: judging.Scored) -> GateCell:
    """1 つのセルの判定。**行き先は理由から決める**（`passed` だけが LightGBM）。"""
    if one.bucket is None:
        raise GateTableError(f"セル単位でない切り口です: {one.system} {one.target} {one.h_min}")
    return GateCell(
        system=one.system,
        target=one.target,
        h_min=one.h_min,
        bucket=one.bucket,
        route=TO_LIGHTGBM if one.reason == PASSED else TO_B3,
        reason=one.reason,
        n=one.n,
        b0=one.b0,
        b3=one.b3,
        lgbm=one.model,
        improvement=one.improvement,
    )


def build(
    model_version: str,
    artifact_sha256: str,
    cells: Sequence[GateCell],
    evaluate_days: Sequence[str],
    rule: Mapping[str, object],
) -> GateTable:
    """表を組む。**セルの切り方と、組になる森の SHA-256 を焼き付ける**（読む側が照合する）。"""
    table = GateTable(
        format_version=FORMAT_VERSION,
        model_version=model_version,
        artifact_sha256=artifact_sha256,
        rule=dict(rule),
        evaluate_days=tuple(evaluate_days),
        horizons_min=tuple(HORIZONS_MIN),
        bucket_labels=tuple(BUCKET_LABELS),
        bucket_edges=tuple(BUCKET_EDGES),
        cells=tuple(sorted((_rounded(one) for one in cells), key=lambda one: one.key)),
    )
    _refuse_inconsistent(table)
    return table


def _rounded(one: GateCell) -> GateCell:
    """数字を `DIGITS` 桁に丸める。**手元の表と、置いた表が同じ値を持つ**ようにする。"""
    return replace(
        one,
        b0=round(one.b0, DIGITS),
        b3=round(one.b3, DIGITS),
        lgbm=round(one.lgbm, DIGITS),
        improvement=None if one.improvement is None else round(one.improvement, DIGITS),
    )


def to_bytes(table: GateTable) -> bytes:
    """gzip した JSON にする。**同じ表からは同じバイト列が出る。**"""
    document = {
        "format_version": table.format_version,
        "kind": KIND,
        "model_version": table.model_version,
        "artifact_sha256": table.artifact_sha256,
        "rule": dict(table.rule),
        "evaluate_days": list(table.evaluate_days),
        "horizons_min": list(table.horizons_min),
        "bucket_labels": list(table.bucket_labels),
        "bucket_edges": list(table.bucket_edges),
        "cells": [_cell_json(one) for one in table.cells],
    }
    text = json.dumps(document, ensure_ascii=False, sort_keys=True)
    return gzip.compress(text.encode(), mtime=0)


def _cell_json(one: GateCell) -> dict[str, object]:
    return {
        "system": one.system,
        "target": one.target,
        "h_min": one.h_min,
        "bucket": one.bucket,
        "route": one.route,
        "reason": one.reason,
        "n": one.n,
        "b0": one.b0,
        "b3": one.b3,
        "lgbm": one.lgbm,
        "improvement": one.improvement,
    }


def from_bytes(body: bytes) -> GateTable:
    """gzip した JSON を読む。**書式・種類・セルの切り方・中身の辻褄を照合する。**"""
    document = json.loads(gzip.decompress(body).decode())
    if document.get("kind") != KIND:
        raise GateTableError(f"門の表ではありません（kind = {document.get('kind')}）")
    if int(document["format_version"]) != FORMAT_VERSION:
        raise GateTableError(f"門の表の書式 {document['format_version']} は読めません")
    table = _table_of(document)
    _refuse_other_cuts(table)
    _refuse_inconsistent(table)
    return table


def _table_of(document: Mapping[str, object]) -> GateTable:
    return GateTable(
        format_version=_integer(document["format_version"]),
        model_version=str(document["model_version"]),
        artifact_sha256=str(document["artifact_sha256"]),
        rule=_mapping(document["rule"]),
        evaluate_days=tuple(str(one) for one in _list(document["evaluate_days"])),
        horizons_min=tuple(_integer(one) for one in _list(document["horizons_min"])),
        bucket_labels=tuple(str(one) for one in _list(document["bucket_labels"])),
        bucket_edges=tuple(_integer(one) for one in _list(document["bucket_edges"])),
        cells=tuple(_cell_of(_mapping(one)) for one in _list(document["cells"])),
    )


def _cell_of(one: Mapping[str, object]) -> GateCell:
    improvement = one["improvement"]
    return GateCell(
        system=str(one["system"]),
        target=str(one["target"]),
        h_min=_integer(one["h_min"]),
        bucket=str(one["bucket"]),
        route=str(one["route"]),
        reason=str(one["reason"]),
        n=_integer(one["n"]),
        b0=_number(one["b0"]),
        b3=_number(one["b3"]),
        lgbm=_number(one["lgbm"]),
        improvement=None if improvement is None else _number(improvement),
    )


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise GateTableError(f"並びのはずの欄が {type(value).__name__} です")
    return value


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise GateTableError(f"対応表のはずの欄が {type(value).__name__} です")
    return {str(key): one for key, one in value.items()}


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GateTableError(f"整数のはずの欄が {value!r} です")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise GateTableError(f"数のはずの欄が {value!r} です")
    return float(value)


def _refuse_other_cuts(table: GateTable) -> None:
    """**セルの切り方が、いまのコードと同じか**（水平の並び・バケツの名前と境目）。"""
    if table.horizons_min != tuple(HORIZONS_MIN):
        raise GateTableError(f"水平の並びが違います: {table.horizons_min}")
    if table.bucket_labels != tuple(BUCKET_LABELS) or table.bucket_edges != tuple(BUCKET_EDGES):
        raise GateTableError(
            f"台数バケツの切り方が違います: {table.bucket_labels} / {table.bucket_edges}"
        )


def refuse_other_forest(table: GateTable, artifact_body: bytes) -> None:
    """**表が、この森のために決めたものか**（読む側が森と組にする前に呼ぶ）。

    森の成果物は `created_at` を含むので、当てはめ直すたびにバイト列が変わる。**同じ名前で
    置き直した森には、前の表の SHA-256 が合わない。**
    """
    found = hashlib.sha256(artifact_body).hexdigest()
    if found != table.artifact_sha256:
        raise GateTableError(
            f"{table.model_version} の門の表は別の森のものです"
            f"（表 {table.artifact_sha256[:12]}… / 森 {found[:12]}…）"
        )


def _refuse_inconsistent(table: GateTable) -> None:
    """**中身の辻褄**：森の印が正しい形で、知らない値が無く、行き先が理由と合い、重複が無い。"""
    if _SHA256.match(table.artifact_sha256) is None:
        raise GateTableError(f"森の SHA-256 の書き方が違います: {table.artifact_sha256!r}")
    for one in table.cells:
        _refuse_unknown(one)
    keys = [one.key for one in table.cells]
    if len(set(keys)) != len(keys):
        raise GateTableError("同じセルが 2 度載っています")


def _refuse_unknown(one: GateCell) -> None:
    targets = {target.name for target in TARGETS}
    if (
        one.target not in targets
        or one.h_min not in HORIZONS_MIN
        or one.bucket not in BUCKET_LABELS
    ):
        raise GateTableError(f"知らないセルです: {one.key}")
    if one.reason not in REASONS or one.route not in (TO_LIGHTGBM, TO_B3):
        raise GateTableError(f"知らない理由か行き先です: {one.key} {one.reason} {one.route}")
    if (one.route == TO_LIGHTGBM) != (one.reason == PASSED):
        raise GateTableError(f"行き先が理由と合いません: {one.key} {one.reason} → {one.route}")
