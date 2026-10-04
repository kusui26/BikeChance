"""合成器（W6 の PR H、開発プランの D-33。W6-08・09・11・12、契約 33・35）。

**セルごとに、門を越えたセルは LightGBM、ほかは B3 を配る。** セルは `system × ターゲット ×
水平 × 台数バケツ` で、バケツは**当てる側の台数**（`bike` は `bikes`、`dock` は `docks`）。
門の表は当てはめのときに検証日で決めたもの（PR F）で、コードには書かない（契約 35）。

**成果物は自己完結**（W6-08）。3 つの部品——B3（書式 2）・森・門の表——を、**置いてあった
ファイルのバイト列のまま**入れ、部品ごとに SHA-256 を持つ。読むときは突き合わせてから、
部品ごとの読み手で読む（**部品の書式の正は 1 か所のまま**）。**門の表と森の組は読むたびに
確かめる**——candidate の名前は置き直せるので、名前だけでは組が決まらない。

**書くのは、ほかから決まらないものだけ**（版の名前・作った時刻・削ったセル・部品）。
`feature_set`・`train_days`・部品の版の名前は部品から決まるので書かない——書くと正が 2 つに
なり、食い違ったときにどちらが本当か分からなくなる（`models/forest.py` と同じ考え）。

**shadow で悪かったセル（W6-12）は、門の表を書き換えずに「削ったセル」の並びで持つ。**
削れるのは LightGBM に行くセルだけで、**足すことはできない**（組むときに止める）。

**予測は、全行で B3 を出し、門で選んだ行だけ森を歩く。** 森も行列も行ごとに独立なので、
選んだ行の確率は LightGBM 単体と、残りの行は B3 単体と、ビット単位で同じになる（検査が
固定する）。**プロファイルを読めなかった周期は全セル B3**（契約 33）：登録簿の
`for_cycle` が `b3_only()` の形に替える。
"""

import base64
import gzip
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final

import numpy as np
import pyarrow as pa

from bikechance_ml.baselines import artifact as baseline_artifact
from bikechance_ml.eval.dataset import BUCKET_LABELS, TARGETS, Target, bucket_index
from bikechance_ml.eval.gates import horizon_index
from bikechance_ml.features.arrays import Bools, Float64, Int16, Int64
from bikechance_ml.features.constants import HORIZONS_MIN
from bikechance_ml.json_shape import as_dict, as_int, as_list, as_str, field
from bikechance_ml.models import artifact as lightgbm_artifact
from bikechance_ml.models import forest as tree
from bikechance_ml.models import gates as gate_tables
from bikechance_ml.models.gates import CellKey
from bikechance_ml.models.matrix import build as build_matrix
from bikechance_ml.models.predictor import BaselinePredictor, Prediction

#: 成果物の書式の版。**読み方を変えたら上げる。**
FORMAT_VERSION: Final[int] = 1

#: `model_versions.kind` に入る値（0055）。
KIND: Final[str] = "composite"

#: 成果物の Content-Type（`models` バケットの許可リストと揃える）。
CONTENT_TYPE: Final[str] = "application/gzip"

#: 版の名前の頭（`composite-v1-<B3 の最終学習日>`。W6-08）。
VERSION_PREFIX: Final[str] = "composite-v1"

#: B3 の部品が持つべき書式（W6-08 の「B3 の中身（v2）」）。版 1 は開く山が 1 GB を超える。
B3_FORMAT_VERSION: Final[int] = 2

#: 部品の名前（成果物の `parts` の鍵）。
B3_PART: Final[str] = "b3"
LIGHTGBM_PART: Final[str] = "lightgbm"
GATES_PART: Final[str] = "gates"
PARTS: Final[tuple[str, ...]] = (B3_PART, LIGHTGBM_PART, GATES_PART)

#: 1 周期の行き先（`inference_log.detail.composite.route`）。
ROUTE_GATED: Final[str] = "gated"
ROUTE_B3_ONLY: Final[str] = "b3_only"


class CompositeFormatError(ValueError):
    """合成器の成果物が読めない、または部品の組と辻褄が合わない。**配らない。**"""


def artifact_path(model_version: str) -> str:
    """`models` バケット内のパス。**版がそのままファイル名**になる。"""
    return f"{KIND}/{model_version}.json.gz"


def model_version_for(b3_train_days: Sequence[str], label: str | None = None) -> str:
    """版の名前。**B3 の最終学習日**を入れ、印があれば間に挟む。

    例：`composite-v1-20261012`・`composite-v1-rehearsal-20261005`（`lgbm-v1` と同じ規則）。
    """
    marked = f"-{label}" if label else ""
    return f"{VERSION_PREFIX}{marked}-{max(b3_train_days).replace('-', '')}"


def sha256_of(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@dataclass(frozen=True)
class CompositeArtifact:
    """配信に要るものを全部入れた合成器。部品は**読んだ形**と**置いてあったバイト列**の両方を持つ。"""

    format_version: int
    model_version: str
    created_at: str
    b3: baseline_artifact.Artifact
    lightgbm: lightgbm_artifact.LightGbmArtifact
    gates: gate_tables.GateTable
    #: shadow で悪かったので B3 に戻したセル（W6-12）。**LightGBM に行くセルの一部だけ**（昇順）
    dropped: tuple[CellKey, ...]
    #: 部品の名前 → 置いてあったファイルのバイト列
    parts: Mapping[str, bytes]

    @property
    def feature_set(self) -> str:
        """**LightGBM の部品の版。** 森がすべての列を読むので、配信の版と一致しなければ配らない。"""
        return self.lightgbm.feature_set

    @property
    def train_days(self) -> tuple[str, ...]:
        """B3 と LightGBM の学習日の**和集合**（昇順。W6-08）。

        鮮度の見張り（0048）は `max(train_days)` を見るので、**B3 の最終学習日で鳴る**。
        """
        return tuple(sorted(set(self.b3.train_days) | set(self.lightgbm.train_days)))

    @property
    def lightgbm_last_day(self) -> str:
        """LightGBM の最終学習日。**警報では見ない**ので、報告とカードに出す（§5.12）。"""
        return max(self.lightgbm.train_days)

    def lightgbm_cells(self) -> frozenset[CellKey]:
        """**森を歩くセル。** 門の表で LightGBM に行くセルから、削ったセルを除いたもの。"""
        return self.gates.lightgbm_cells() - frozenset(self.dropped)

    def describe(self) -> str:
        cells = f"森 {len(self.lightgbm_cells())} セル（削った {len(self.dropped)}）"
        return (
            f"{self.model_version}（B3 {self.b3.model_version}・LightGBM "
            f"{self.lightgbm.model_version}・{cells}・学習 {len(self.train_days)} 日）"
        )


# ── 組む・読む ────────────────────────────────────────────────
def assemble(
    parts: Mapping[str, bytes],
    *,
    created_at: str,
    dropped: Sequence[CellKey] = (),
    model_version: str | None = None,
    label: str | None = None,
) -> CompositeArtifact:
    """3 つの部品のファイルから組む。**部品を読み、組と辻褄を確かめてから。**

    版の名前を渡さなければ、**B3 の最終学習日**と印から決める（`model_version_for`）。
    """
    missing = [name for name in PARTS if name not in parts]
    if missing or set(parts) != set(PARTS):
        raise CompositeFormatError(
            f"部品が揃っていません（足りない {missing}・全部 {sorted(parts)}）"
        )
    forest = lightgbm_artifact.from_bytes(parts[LIGHTGBM_PART])
    b3 = _b3_of(parts[B3_PART])
    composite = CompositeArtifact(
        format_version=FORMAT_VERSION,
        model_version=model_version or model_version_for(b3.train_days, label),
        created_at=created_at,
        b3=b3,
        lightgbm=forest,
        gates=_gates_of(parts[GATES_PART], parts[LIGHTGBM_PART], forest),
        dropped=tuple(dropped),
        parts={name: parts[name] for name in PARTS},
    )
    _refuse_inconsistent(composite)
    return composite


def _b3_of(body: bytes) -> baseline_artifact.Artifact:
    """B3 の部品。**書式 2 でなければ止める**（W6-08）。"""
    b3 = baseline_artifact.from_bytes(body)
    if b3.format_version != B3_FORMAT_VERSION:
        raise CompositeFormatError(
            f"B3 の部品は書式 {B3_FORMAT_VERSION} だけを受けます（{b3.model_version} は "
            f"{b3.format_version}）"
        )
    return b3


def _gates_of(
    body: bytes, forest_body: bytes, forest: lightgbm_artifact.LightGbmArtifact
) -> gate_tables.GateTable:
    """門の表の部品。**この森のために決めた表か**を確かめる（契約 35）。"""
    table = gate_tables.from_bytes(body)
    gate_tables.refuse_other_forest(table, forest_body)
    if table.model_version != forest.model_version:
        raise CompositeFormatError(
            f"門の表は {table.model_version} の、森は {forest.model_version} のものです"
        )
    return table


def _refuse_inconsistent(composite: CompositeArtifact) -> None:
    """**部品どうしの辻褄**：水平の並び・系統・削ったセル（足していないか・重複・並び）。"""
    expected = tuple(HORIZONS_MIN)
    if composite.b3.horizons_min != expected or composite.lightgbm.horizons_min != expected:
        raise CompositeFormatError("部品の水平の並びが、いまのコードと違います")
    unknown = {one.system for one in composite.gates.cells} - set(composite.b3.systems)
    if unknown:
        raise CompositeFormatError(f"門の表に、B3 の知らない系統があります: {sorted(unknown)}")
    _refuse_bad_drops(composite.dropped, composite.gates.lightgbm_cells())


def _refuse_bad_drops(dropped: Sequence[CellKey], lightgbm_cells: frozenset[CellKey]) -> None:
    """**削れるのは LightGBM に行くセルだけ**（W6-12：shadow の後は削るだけ。足さない）。"""
    if list(dropped) != sorted(set(dropped)):
        raise CompositeFormatError("削ったセルは重複なく昇順に並べます")
    added = [one for one in dropped if one not in lightgbm_cells]
    if added:
        raise CompositeFormatError(
            f"LightGBM に行かないセルは削れません（足すことになる）: {added}"
        )


def to_bytes(composite: CompositeArtifact) -> bytes:
    """gzip した JSON にする。**同じ合成器からは同じバイト列が出る。**"""
    document = {
        "format_version": composite.format_version,
        "kind": KIND,
        "model_version": composite.model_version,
        "created_at": composite.created_at,
        "dropped_cells": [list(one) for one in composite.dropped],
        "parts": {name: _part_json(body) for name, body in composite.parts.items()},
    }
    text = json.dumps(document, ensure_ascii=False, sort_keys=True)
    return gzip.compress(text.encode(), mtime=0)


def _part_json(body: bytes) -> dict[str, str]:
    return {"sha256": sha256_of(body), "base64": base64.b64encode(body).decode("ascii")}


def from_bytes(body: bytes) -> CompositeArtifact:
    """gzip した JSON を読む。**書式・種類・部品の SHA-256・組・辻褄を照合する。**"""
    document = as_dict(json.loads(gzip.decompress(body).decode()), "composite")
    if field(document, "kind", "composite") != KIND:
        raise CompositeFormatError(f"合成器ではありません（kind = {document.get('kind')}）")
    version = as_int(field(document, "format_version", "composite"), "format_version")
    if version != FORMAT_VERSION:
        raise CompositeFormatError(
            f"合成器の書式 {version} は、この版（{FORMAT_VERSION}）では読めません"
        )
    parts_doc = as_dict(field(document, "parts", "composite"), "parts")
    return assemble(
        {name: _part_of(parts_doc, name) for name in parts_doc},
        model_version=as_str(field(document, "model_version", "composite"), "model_version"),
        created_at=as_str(field(document, "created_at", "composite"), "created_at"),
        dropped=[
            _cell_key(one)
            for one in as_list(field(document, "dropped_cells", "composite"), "dropped_cells")
        ],
    )


def _part_of(parts: Mapping[str, object], name: str) -> bytes:
    """部品 1 つを取り出す。**SHA-256 が合わなければ止める**（書き換わった部品を使わない）。"""
    fields = as_dict(parts[name], name)
    try:
        body = base64.b64decode(as_str(field(fields, "base64", name), "base64"), validate=True)
    # `binascii.Error` は `ValueError` の子。ASCII でない文字は `ValueError` で来る
    except ValueError as cause:
        raise CompositeFormatError(f"部品 {name} が base64 として読めません") from cause
    if sha256_of(body) != as_str(field(fields, "sha256", name), "sha256"):
        raise CompositeFormatError(f"部品 {name} の SHA-256 が合いません")
    return body


def _cell_key(value: object) -> CellKey:
    """削ったセル 1 つ（`[system, ターゲット, 水平, バケツ]`）。"""
    items = as_list(value, "dropped_cells")
    if len(items) != len(("system", "target", "h_min", "bucket")):
        raise CompositeFormatError(f"削ったセルの形が違います: {items}")
    system, target, h_min, bucket = items
    return (
        as_str(system, "system"),
        as_str(target, "target"),
        as_int(h_min, "h_min"),
        as_str(bucket, "bucket"),
    )


# ── 予測 ──────────────────────────────────────────────────────
def routes(cells: frozenset[CellKey], system_id: str, table: pa.Table) -> dict[str, Bools]:
    """ターゲットごとに、**森を歩く行**（門の表で LightGBM に行くセルの行）。

    セルの引き方は**門を決めたときと同じ関数**（`eval.gates.horizon_index`・
    `eval.dataset.bucket_index`）を通す。境目の数え方が 1 つずれると、例外を出さずに
    別のセルで LightGBM を配ることになる。
    """
    horizon = horizon_index(_int16(table, "h_min"))
    return {target.name: _walks(cells, system_id, target, horizon, table) for target in TARGETS}


def _walks(
    cells: frozenset[CellKey], system_id: str, target: Target, horizon: Int64, table: pa.Table
) -> Bools:
    buckets = bucket_index(_int16(table, target.counts))
    return np.asarray(lookup(cells, system_id, target)[horizon, buckets], dtype=np.bool_)


def lookup(cells: frozenset[CellKey], system_id: str, target: Target) -> Bools:
    """1 つの系統・ターゲットの、**`水平 × バケツ` の表**（森を歩くセルが真）。"""
    found = np.zeros((len(HORIZONS_MIN), len(BUCKET_LABELS)), dtype=np.bool_)
    for system, name, h_min, bucket in cells:
        if system == system_id and name == target.name:
            found[HORIZONS_MIN.index(h_min), BUCKET_LABELS.index(bucket)] = True
    return found


def _int16(table: pa.Table, name: str) -> Int16:
    column = table.column(name).combine_chunks()
    return np.asarray(column.to_numpy(zero_copy_only=False), dtype=np.int16)


@dataclass(frozen=True)
class CompositePredictor:
    """合成器の口。**全行で B3 を出し、門で選んだ行だけ森を歩く。**"""

    artifact: CompositeArtifact
    #: 森を歩くか。**プロファイルを読めなかった周期は歩かない**（契約 33。`b3_only`）
    walks_forest: bool = True

    @property
    def model_version(self) -> str:
        return self.artifact.model_version

    @property
    def kind(self) -> str:
        return KIND

    @property
    def feature_set(self) -> str:
        return self.artifact.feature_set

    @property
    def route(self) -> str:
        """この形の行き先（`detail.composite.route`）。"""
        return ROUTE_GATED if self.walks_forest else ROUTE_B3_ONLY

    def b3_only(self) -> "CompositePredictor":
        """**全セル B3 の形**（同じ版の名前のまま、森を歩かない）。契約 33。"""
        return replace(self, walks_forest=False)

    def predict(self, system_id: str, at: datetime, table: pa.Table) -> Prediction:
        base = BaselinePredictor(self.artifact.b3).predict(system_id, at, table)
        if not self.walks_forest:
            return base
        walks = self.walks(system_id, table)
        forest = _walk_forest(self.artifact.lightgbm, walks, table)
        return _mixed(base, walks, forest, lightgbm_artifact.profile_informed(table))

    def walks(self, system_id: str, table: pa.Table) -> dict[str, Bools]:
        """ターゲットごとに森を歩く行。**全セル B3 の形では 1 行も歩かない。**"""
        if not self.walks_forest:
            return {target.name: np.zeros(table.num_rows, dtype=np.bool_) for target in TARGETS}
        return routes(self.artifact.lightgbm_cells(), system_id, table)

    def unknown_ports(self, system_id: str, table: pa.Table) -> int:
        """B3 の知らないポートの数（**B3 のセルでは気候値が引けず B1 だけになる**）。"""
        return BaselinePredictor(self.artifact.b3).unknown_ports(system_id, table)


def to_predictor(composite: CompositeArtifact) -> CompositePredictor:
    """成果物から配信用の口を作る。"""
    return CompositePredictor(artifact=composite)


def _walk_forest(
    forest: lightgbm_artifact.LightGbmArtifact, walks: Mapping[str, Bools], table: pa.Table
) -> dict[str, Float64]:
    """**選んだ行だけ**森を歩く。行列は両ターゲットの和の行で 1 回だけ作る。"""
    union = np.logical_or.reduce(list(walks.values()), initial=False)
    if not bool(np.any(union)):
        return {}
    built = build_matrix(table, keep=np.asarray(union, dtype=np.bool_))
    place = np.cumsum(union) - 1
    return {
        name: tree.probability(forest.forests[name], built.values[place[rows]])
        for name, rows in walks.items()
        if bool(rows.any())
    }


def _mixed(
    base: Prediction, walks: Mapping[str, Bools], forest: Mapping[str, Float64], informed: Bools
) -> Prediction:
    """B3 の確率の、**森を歩いた行だけ**を森の確率に替える。確度も同じ行だけ替える（W6-11）。"""
    probability = {
        name: _put(values, walks[name], forest.get(name))
        for name, values in base.probability.items()
    }
    return Prediction(
        probability=probability,
        informed={
            name: np.asarray(np.where(walks[name], informed, used), dtype=np.bool_)
            for name, used in base.informed.items()
        },
    )


def _put(values: Float64, rows: Bools, walked: Float64 | None) -> Float64:
    if walked is None:
        return values
    mixed = values.copy()
    mixed[rows] = walked
    return mixed
