"""ベースラインの成果物（W3 プラン §5.10、開発プラン §8.2）。

**学習と配信で同じ実装を使うための入れ物。** `baselines/` の当てはめ結果（B1 の参照表・
B2 の気候値・B3 の係数）を 1 つの JSON に固め、Storage に置く。推論はこれを読み、
`conditional.predict` / `climatology.predict` / `blend.predict` を**そのまま**呼ぶ。
配信用に別の実装を書かない（CLAUDE.md §2 の原則 4）。

**ポートとシステムの並びも一緒に固める。** B1 も B2 も番号で引くので、並びが変われば
別のセルを指す。成果物に無いポートは気候値が引けないだけで、B1 には落とせる。

**gzip した JSON で持つ。** B2 のセルは日数が増えると数百万になり得るので、
キーと値を平たい配列で並べる（辞書だとキーの文字列だけで数十 MB になる）。
"""

import gzip
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np

from bikechance_ml.baselines import blend, climatology, conditional
from bikechance_ml.features.arrays import Bools, Float64, Int64

#: 成果物の書式の版。**読み方を変えたら上げる。**
FORMAT_VERSION: Final[int] = 1

#: 確率を丸める桁。0.0001 は確率 ×1000 の分解能より細かい。
RATE_DIGITS: Final[int] = 6

#: `model_versions.kind` に入る値。
KIND: Final[str] = "baseline"


def artifact_path(model_version: str) -> str:
    """`models` バケット内のパス。**版がそのままファイル名**になる。"""
    return f"{KIND}/{model_version}.json.gz"


@dataclass(frozen=True)
class TargetModel:
    """1 ターゲット（貸出 / 返却）ぶんの当てはめ結果。"""

    b1: conditional.Table
    b2: climatology.Table
    b3: blend.Blend


@dataclass(frozen=True)
class Artifact:
    """配信に要るものを全部入れた成果物。"""

    format_version: int
    model_version: str
    feature_set: str
    created_at: str
    train_days: tuple[str, ...]
    horizons_min: tuple[int, ...]
    systems: tuple[str, ...]
    #: `"{system_id}/{station_id}"` の並び。B2 のセルの番号がこれに対応する
    ports: tuple[str, ...]
    targets: Mapping[str, TargetModel]

    def describe(self) -> str:
        cells = {name: one.b2.cells for name, one in self.targets.items()}
        return f"{self.model_version}（学習 {len(self.train_days)} 日、気候値のセル {cells}）"


def to_bytes(artifact: Artifact) -> bytes:
    """gzip した JSON にする。**同じ成果物からは同じバイト列が出る。**"""
    document = {
        "format_version": artifact.format_version,
        "model_version": artifact.model_version,
        "feature_set": artifact.feature_set,
        "created_at": artifact.created_at,
        "train_days": list(artifact.train_days),
        "horizons_min": list(artifact.horizons_min),
        "systems": list(artifact.systems),
        "ports": list(artifact.ports),
        "targets": {name: _target_to_json(one) for name, one in artifact.targets.items()},
    }
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True).encode()
    return gzip.compress(encoded, mtime=0)


def from_bytes(body: bytes) -> Artifact:
    """gzip した JSON を読む。**書式の版が違えば例外にする。**"""
    document = json.loads(gzip.decompress(body).decode())
    version = int(document["format_version"])
    if version != FORMAT_VERSION:
        raise ValueError(f"成果物の書式が {version} で、この版（{FORMAT_VERSION}）では読めません")
    ports = tuple(str(one) for one in document["ports"])
    systems = tuple(str(one) for one in document["systems"])
    return Artifact(
        format_version=version,
        model_version=str(document["model_version"]),
        feature_set=str(document["feature_set"]),
        created_at=str(document["created_at"]),
        train_days=tuple(str(one) for one in document["train_days"]),
        horizons_min=tuple(int(one) for one in document["horizons_min"]),
        systems=systems,
        ports=ports,
        targets={
            name: _target_from_json(one, len(ports)) for name, one in document["targets"].items()
        },
    )


def _target_to_json(model: TargetModel) -> dict[str, object]:
    usable = np.nonzero(model.b2.usable)[0]
    return {
        "b1_rate": _rounded(model.b1.rate),
        "b1_seen": [bool(one) for one in model.b1.seen],
        "b1_fallback": _rounded(model.b1.fallback),
        "b1_n_systems": model.b1.n_systems,
        "b2_keys": [int(one) for one in usable],
        "b2_rate": _rounded(model.b2.rate[usable]),
        "b2_min_samples": model.b2.min_samples,
        "b2_min_days": model.b2.min_days,
        # **B3 の係数は丸めない。** 4 つずつしか無く、標準化の分母を丸めると
        # ゼロ除算になり得る（W3 プラン §12 の 104）。丸めるのは数が多い B1・B2 だけ
        "b3_intercept": float(model.b3.intercept),
        "b3_weights": _exact(model.b3.weights),
        "b3_center": _exact(model.b3.center),
        "b3_scale": _exact(model.b3.scale),
    }


def _target_from_json(document: object, n_ports: int) -> TargetModel:
    fields = _as_mapping(document)
    size = n_ports * _CLIMATOLOGY_CELLS_PER_PORT
    keys = np.asarray(_numbers(fields["b2_keys"]), dtype=np.int64)
    rate = np.zeros(size, dtype=np.float64)
    usable = np.zeros(size, dtype=np.bool_)
    rate[keys] = _numbers(fields["b2_rate"])
    usable[keys] = True
    return TargetModel(
        b1=conditional.Table(
            n_systems=int(str(fields["b1_n_systems"])),
            rate=np.asarray(_numbers(fields["b1_rate"]), dtype=np.float64),
            # 参照表の分子と分母は配信では使わない（leave-one-out は学習側だけ）
            total=np.zeros(0, dtype=np.float64),
            positive=np.zeros(0, dtype=np.float64),
            fallback=np.asarray(_numbers(fields["b1_fallback"]), dtype=np.float64),
            seen=np.asarray([bool(one) for one in _as_sequence(fields["b1_seen"])], dtype=np.bool_),
        ),
        b2=climatology.Table(
            n_ports=n_ports,
            rate=rate,
            total=np.zeros(0, dtype=np.float64),
            positive=np.zeros(0, dtype=np.float64),
            counted=np.zeros(0, dtype=np.int64),
            usable=usable,
            min_samples=int(str(fields["b2_min_samples"])),
            min_days=int(str(fields["b2_min_days"])),
        ),
        b3=blend.Blend(
            intercept=float(str(fields["b3_intercept"])),
            weights=np.asarray(_numbers(fields["b3_weights"]), dtype=np.float64),
            center=np.asarray(_numbers(fields["b3_center"]), dtype=np.float64),
            scale=np.asarray(_numbers(fields["b3_scale"]), dtype=np.float64),
        ),
    )


#: 1 ポートあたりの気候値のセル数（曜日種別 × 15 分枠）。`climatology._key` と同じ形。
_CLIMATOLOGY_CELLS_PER_PORT: Final[int] = 3 * climatology.SLOTS_PER_DAY


def _rounded(values: Float64 | Int64 | Bools) -> list[float]:
    """確率を丸める。**数が多い列だけ**（B1 の 120 個、B2 の数百万個）。"""
    return [round(float(one), RATE_DIGITS) for one in values]


def _exact(values: Float64) -> list[float]:
    """丸めずに書く。**係数はここを通す。**"""
    return [float(one) for one in values]


def _numbers(value: object) -> list[float]:
    return [float(str(one)) for one in _as_sequence(value)]


def _as_sequence(value: object) -> Sequence[object]:
    if not isinstance(value, list):
        raise TypeError("配列を期待した")
    return value


def _as_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise TypeError("オブジェクトを期待した")
    return value
