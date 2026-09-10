"""LightGBM の成果物（`models/artifact.py`）。

**主題は「読んだ成果物が、当てはめたときと同じ意味を持つこと」。** 木は位置で
特徴量を見るので、列の並びやカテゴリの語彙がずれても**例外は出ず、確率だけが
静かに変わる**。だから読むときに照合し、ずれていれば配らない。

**成果物は木の構造そのものを持つ**（PR E′）。`lightgbm` はここでは
「正解を出す側」としてだけ使い、**配信の経路には入らない**
（`tests/test_serving_imports.py` が機械で確かめる）。
"""

from typing import Final

import lightgbm as lgb
import numpy as np
import pytest

from bikechance_ml.features.arrays import Float64
from bikechance_ml.features.constants import FEATURE_SET, HORIZONS_MIN
from bikechance_ml.features.schema import SERVING_SCHEMA
from bikechance_ml.models import artifact as lightgbm_artifact
from bikechance_ml.models import forest, matrix

N_COLUMNS: Final[int] = len(matrix.MODEL_COLUMNS)

#: 読み直した森と `Booster.predict` の差の上限（`jobs/fit_lightgbm.py` と同じ考え方）。
TOLERANCE: Final[float] = 1e-9


def _features(rows: int, seed: int) -> Float64:
    """本番と同じ列数・同じカテゴリの位置で、**欠損も混ぜた**行列を作る。"""
    rng = np.random.default_rng(seed)
    values = rng.normal(size=(rows, N_COLUMNS))
    for index in matrix.categorical_indices():
        values[:, index] = rng.integers(0, 3, size=rows)
    values[rng.random((rows, N_COLUMNS)) < 0.05] = np.nan
    return values


def _boosters(rounds: int = 12) -> dict[str, lgb.Booster]:
    """小さな森を 2 つ当てはめる。**正解を出す側**（配信には使わない）。"""
    rng = np.random.default_rng(20260910)
    rows = 4_000
    features = _features(rows, 20260910)
    made: dict[str, lgb.Booster] = {}
    for name in ("bike", "dock"):
        y = (np.nan_to_num(features[:, 3]) + rng.normal(size=rows) > 0).astype(np.float64)
        made[name] = lgb.train(
            {"objective": "binary", "num_leaves": 15, "min_data_in_leaf": 20, "verbose": -1},
            lgb.Dataset(
                features,
                label=y,
                feature_name=list(matrix.MODEL_COLUMNS),
                categorical_feature=list(matrix.categorical_indices()),
                free_raw_data=False,
            ),
            num_boost_round=rounds,
        )
    return made


BOOSTERS: Final[dict[str, lgb.Booster]] = _boosters()


def _artifact() -> lightgbm_artifact.LightGbmArtifact:
    return lightgbm_artifact.build(
        forests={name: forest.flatten(one.dump_model()) for name, one in BOOSTERS.items()},
        model_version="lgbm-v0-test",
        feature_set=FEATURE_SET,
        created_at="2026-09-10T05:00:00+00:00",
        train_days=["2026-09-08", "2026-09-09"],
        horizons_min=HORIZONS_MIN,
        params={"objective": "binary", "num_boost_round": 12},
    )


ARTIFACT: Final[lightgbm_artifact.LightGbmArtifact] = _artifact()


# ── 往復 ──────────────────────────────────────────────────────
def test_round_trip_keeps_everything() -> None:
    """**書いて読んで書けば同じバイト列になる。** 森の配列まで含めて落ちない。"""
    written = lightgbm_artifact.to_bytes(ARTIFACT)
    assert lightgbm_artifact.to_bytes(lightgbm_artifact.from_bytes(written)) == written


def test_round_trip_keeps_the_metadata() -> None:
    restored = lightgbm_artifact.from_bytes(lightgbm_artifact.to_bytes(ARTIFACT))
    assert restored.model_version == ARTIFACT.model_version
    assert restored.feature_set == ARTIFACT.feature_set
    assert restored.train_days == ARTIFACT.train_days
    assert restored.horizons_min == ARTIFACT.horizons_min
    assert restored.params == ARTIFACT.params


def test_the_same_artifact_gives_the_same_bytes() -> None:
    """**再現性。** 同じ成果物から同じバイト列（`mtime=0`、キーは整列）。"""
    assert lightgbm_artifact.to_bytes(ARTIFACT) == lightgbm_artifact.to_bytes(ARTIFACT)


def test_the_path_uses_the_version() -> None:
    assert (
        lightgbm_artifact.artifact_path("lgbm-v0-20260909") == "lightgbm/lgbm-v0-20260909.json.gz"
    )


def test_the_column_order_is_burned_in() -> None:
    assert ARTIFACT.columns == matrix.MODEL_COLUMNS
    assert ARTIFACT.categorical == matrix.CATEGORICAL_COLUMNS


def test_the_format_version_says_how_to_read_it() -> None:
    """**書式の版は 2**（PR E′ で木の構造そのものを持つ形に変えた）。"""
    assert ARTIFACT.format_version == 2


# ── 照合 ──────────────────────────────────────────────────────
def _tampered(**changes: object) -> bytes:
    import gzip
    import json

    document = json.loads(gzip.decompress(lightgbm_artifact.to_bytes(ARTIFACT)).decode())
    document.update(changes)
    return gzip.compress(json.dumps(document).encode())


def test_a_different_column_order_is_refused() -> None:
    """**並びがずれたら配らない。** 木は位置で特徴量を見る。"""
    shuffled = [*matrix.MODEL_COLUMNS[1:], matrix.MODEL_COLUMNS[0]]
    with pytest.raises(lightgbm_artifact.ArtifactMismatchError):
        lightgbm_artifact.from_bytes(_tampered(columns=shuffled))


def test_a_missing_column_is_refused() -> None:
    with pytest.raises(lightgbm_artifact.ArtifactMismatchError):
        lightgbm_artifact.from_bytes(_tampered(columns=list(matrix.MODEL_COLUMNS[:-1])))


def test_different_categorical_columns_are_refused() -> None:
    with pytest.raises(lightgbm_artifact.ArtifactMismatchError):
        lightgbm_artifact.from_bytes(_tampered(categorical=["system_id"]))


def test_a_changed_vocabulary_is_refused() -> None:
    """**同じ番号が別の意味になる。** 暦の規則を足したときに効く。"""
    changed = {name: list(values) for name, values in ARTIFACT.vocabularies.items()}
    changed["dow_type"] = [*changed["dow_type"], "宇宙の日"]
    with pytest.raises(lightgbm_artifact.ArtifactMismatchError):
        lightgbm_artifact.from_bytes(_tampered(vocabularies=changed))


def test_an_unknown_format_version_is_refused() -> None:
    with pytest.raises(ValueError, match="書式"):
        lightgbm_artifact.from_bytes(_tampered(format_version=99))


def test_the_old_format_is_refused() -> None:
    """**PR E の成果物（`model_to_string`）は読めない。** 黙って読み替えない。"""
    with pytest.raises(ValueError, match="書式"):
        lightgbm_artifact.from_bytes(_tampered(format_version=1))


# ── 予測 ──────────────────────────────────────────────────────
def test_predict_matches_lightgbm_after_a_round_trip() -> None:
    """**Storage を往復した森が、当てはめた `Booster` と同じ値を出す。**

    ここが `lightgbm` に頼る最後の場所である。配信ではこの経路（`models/forest.py`）
    だけが動き、`Booster` は居ない。
    """
    values = _features(500, 7)
    built = matrix.Matrix(values=values, columns=matrix.MODEL_COLUMNS)
    restored = lightgbm_artifact.from_bytes(lightgbm_artifact.to_bytes(ARTIFACT))
    for name, booster in BOOSTERS.items():
        ours = lightgbm_artifact.predict(restored.forests[name], built)
        theirs = np.asarray(booster.predict(values), dtype=np.float64)
        assert float(np.abs(ours - theirs).max()) < TOLERANCE


def test_the_predictor_returns_both_targets() -> None:
    predictor = lightgbm_artifact.to_predictor(ARTIFACT)
    assert predictor.kind == "lightgbm"
    assert predictor.feature_set == FEATURE_SET
    assert set(predictor.artifact.forests) == {"bike", "dock"}


def test_a_global_model_has_no_unknown_ports() -> None:
    """**全ポート共通**なので「成果物が知らないポート」が無い（B2 と違う）。"""
    predictor = lightgbm_artifact.to_predictor(ARTIFACT)
    assert predictor.unknown_ports("hellocycling", SERVING_SCHEMA.empty_table()) == 0
