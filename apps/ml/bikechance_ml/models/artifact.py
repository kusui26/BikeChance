"""LightGBM の成果物（W4 プラン §6.5、開発プラン §8.2）。

**`baselines/artifact.py` と同じ入れ物の役割。** 当てはめた結果を 1 つの gzip JSON に
固め、`models` バケットに置く。推論はこれを読み、**学習と同じ `models/matrix.py` を
通して**予測する（CLAUDE.md §2 の原則 4）。

**`lightgbm` をここで import する。** このモジュールを読み込むのは LightGBM の版を
配るときだけで、ベースラインを配っている間は読み込まれない（`models/registry.py` が
`kind` で分岐する）。依存が条件付きなので、import も条件付きになる。

成果物に**列の並びと語彙を焼き付ける**。読むときに `models/matrix.py` の現行と
突き合わせ、違えば止める。列が 1 つずれれば木は別の特徴量で分岐するが、**例外は出ず
確率だけが静かに変わる**ので、機械で照合する以外に気づく方法が無い。
"""

import gzip
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import lightgbm as lgb
import numpy as np
import pyarrow as pa

from bikechance_ml.features.arrays import Float64
from bikechance_ml.models.matrix import (
    CATEGORICAL_COLUMNS,
    MODEL_COLUMNS,
    VOCABULARIES,
    Matrix,
)
from bikechance_ml.models.matrix import build as build_matrix
from bikechance_ml.models.predictor import Prediction

#: 成果物の書式の版。**読み方を変えたら上げる。**
FORMAT_VERSION: Final[int] = 1

#: `model_versions.kind` に入る値。
KIND: Final[str] = "lightgbm"

#: 成果物の Content-Type（`models` バケットの許可リストと揃える）。
CONTENT_TYPE: Final[str] = "application/gzip"


class ArtifactMismatchError(RuntimeError):
    """成果物と、いまのコードの特徴量の作り方が食い違う。**配らない。**"""


@dataclass(frozen=True)
class LightGbmArtifact:
    """配信に要るものを全部入れた成果物。"""

    format_version: int
    model_version: str
    feature_set: str
    created_at: str
    train_days: tuple[str, ...]
    horizons_min: tuple[int, ...]
    #: 学習したときの列の並び。**推論はこれと現行を突き合わせる**
    columns: tuple[str, ...]
    categorical: tuple[str, ...]
    vocabularies: Mapping[str, tuple[str, ...]]
    params: Mapping[str, object]
    #: ターゲット名 → `Booster.model_to_string()`
    boosters: Mapping[str, str]

    def describe(self) -> str:
        trees = {name: text.count("\nTree=") for name, text in self.boosters.items()}
        return f"{self.model_version}（学習 {len(self.train_days)} 日、木 {trees}）"


def artifact_path(model_version: str) -> str:
    """`models` バケット内のパス。**版がそのままファイル名**になる。"""
    return f"{KIND}/{model_version}.json.gz"


def to_bytes(artifact: LightGbmArtifact) -> bytes:
    """gzip した JSON にする。**同じ成果物からは同じバイト列が出る。**"""
    document = {
        "format_version": artifact.format_version,
        "model_version": artifact.model_version,
        "kind": KIND,
        "feature_set": artifact.feature_set,
        "created_at": artifact.created_at,
        "train_days": list(artifact.train_days),
        "horizons_min": list(artifact.horizons_min),
        "columns": list(artifact.columns),
        "categorical": list(artifact.categorical),
        "vocabularies": {name: list(values) for name, values in artifact.vocabularies.items()},
        "params": dict(artifact.params),
        "boosters": dict(artifact.boosters),
    }
    return gzip.compress(json.dumps(document, ensure_ascii=False, sort_keys=True).encode(), mtime=0)


def from_bytes(body: bytes) -> LightGbmArtifact:
    """gzip した JSON を読む。**書式と特徴量の作り方を照合する。**"""
    document = json.loads(gzip.decompress(body).decode())
    version = int(document["format_version"])
    if version != FORMAT_VERSION:
        raise ValueError(f"成果物の書式が {version} で、この版（{FORMAT_VERSION}）では読めません")
    artifact = LightGbmArtifact(
        format_version=version,
        model_version=str(document["model_version"]),
        feature_set=str(document["feature_set"]),
        created_at=str(document["created_at"]),
        train_days=tuple(str(one) for one in document["train_days"]),
        horizons_min=tuple(int(one) for one in document["horizons_min"]),
        columns=tuple(str(one) for one in document["columns"]),
        categorical=tuple(str(one) for one in document["categorical"]),
        vocabularies={
            str(name): tuple(str(one) for one in values)
            for name, values in document["vocabularies"].items()
        },
        params=dict(document["params"]),
        boosters={str(name): str(text) for name, text in document["boosters"].items()},
    )
    _refuse_mismatch(artifact)
    return artifact


def _refuse_mismatch(artifact: LightGbmArtifact) -> None:
    """列の並び・カテゴリ・語彙が、いまの `models/matrix.py` と同じか。

    **1 つでも違えば配らない。** 木は位置で特徴量を見るので、並びがずれても例外は
    出ない。カテゴリの語彙がずれれば、同じ番号が別の意味になる。どちらも
    「確率だけが静かに変わる」壊れ方をする。
    """
    if artifact.columns != MODEL_COLUMNS:
        raise ArtifactMismatchError(
            f"列の並びが違います（成果物 {len(artifact.columns)} 列 / "
            f"いま {len(MODEL_COLUMNS)} 列）"
        )
    if artifact.categorical != CATEGORICAL_COLUMNS:
        raise ArtifactMismatchError(f"カテゴリ列が違います: {artifact.categorical}")
    current = {name: tuple(values) for name, values in VOCABULARIES.items()}
    if dict(artifact.vocabularies) != current:
        raise ArtifactMismatchError("カテゴリの語彙が違います（番号が別の意味になります）")


def load_boosters(artifact: LightGbmArtifact) -> Mapping[str, lgb.Booster]:
    """文字列から `Booster` を作る。**読み込みは版ごとに 1 度だけ**（呼ぶ側が持ち回る）。"""
    return {name: lgb.Booster(model_str=text) for name, text in artifact.boosters.items()}


def predict(booster: lgb.Booster, matrix: Matrix) -> Float64:
    """1 ターゲットぶんの確率。**学習と同じ列の並びで渡す。**"""
    return np.asarray(booster.predict(matrix.values), dtype=np.float64)


def build(
    boosters: Mapping[str, lgb.Booster],
    model_version: str,
    feature_set: str,
    created_at: str,
    train_days: Sequence[str],
    horizons_min: Sequence[int],
    params: Mapping[str, object],
) -> LightGbmArtifact:
    """当てはめた `Booster` を成果物にする。**列の並びと語彙を焼き付ける。**"""
    return LightGbmArtifact(
        format_version=FORMAT_VERSION,
        model_version=model_version,
        feature_set=feature_set,
        created_at=created_at,
        train_days=tuple(train_days),
        horizons_min=tuple(horizons_min),
        columns=MODEL_COLUMNS,
        categorical=CATEGORICAL_COLUMNS,
        vocabularies={name: tuple(values) for name, values in VOCABULARIES.items()},
        params=dict(params),
        boosters={name: one.model_to_string() for name, one in boosters.items()},
    )


@dataclass(frozen=True)
class LightGbmPredictor:
    """LightGBM の版。**学習と同じ `models/matrix.py` を通して予測する。**

    **`feature_set` が一致しなければ配れない。** LightGBM は 61 列すべてを読むので、
    版が違えば「同じ名前で意味の違う列」を見る（v1 は容量まわり、v2 は
    `minutes_since_last_change`、v3 は天気が変わった）。ベースラインと違って
    ここは厳密に照合する（`models/registry.py`）。
    """

    artifact: LightGbmArtifact
    boosters: Mapping[str, "lgb.Booster"]

    @property
    def model_version(self) -> str:
        return self.artifact.model_version

    @property
    def kind(self) -> str:
        return KIND

    @property
    def feature_set(self) -> str:
        return self.artifact.feature_set

    # `system_id` と `at` は使わない（**全ポート共通のモデル**なので、系統も日付も
    # 特徴量の列として渡っている）。それでも `Predictor` の署名に合わせて受け取る
    def predict(self, system_id: str, at: datetime, table: pa.Table) -> Prediction:  # noqa: ARG002
        """全ターゲットぶんの確率。**1 回の行列で 10 水平すべてを出す。**

        `informed` は常に真：LightGBM は欠損を木の中で扱うので、「本来の情報が
        引けなかった」に当たる状態が無い（B3 の気候値とは違う）。確度の意味づけは
        W5 の校正で見直す。
        """
        built = build_matrix(table)
        rows = len(built)
        return Prediction(
            probability={name: predict(booster, built) for name, booster in self.boosters.items()},
            informed={name: np.ones(rows, dtype=np.bool_) for name in self.boosters},
        )

    def unknown_ports(self, system_id: str, table: pa.Table) -> int:  # noqa: ARG002
        """**0。** 全ポート共通のモデル（Global Model）なので、知らないポートが無い。"""
        return 0


def to_predictor(artifact: LightGbmArtifact) -> LightGbmPredictor:
    """成果物から配信用の口を作る。**`Booster` の組み立てはここで 1 度だけ。**"""
    return LightGbmPredictor(artifact=artifact, boosters=load_boosters(artifact))
