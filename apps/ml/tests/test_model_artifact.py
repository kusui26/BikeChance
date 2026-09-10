"""LightGBM の成果物（`models/artifact.py`）。

**主題は「読んだ成果物が、当てはめたときと同じ意味を持つこと」。** 木は位置で
特徴量を見るので、列の並びやカテゴリの語彙がずれても**例外は出ず、確率だけが
静かに変わる**。だから読むときに照合し、ずれていれば配らない。
"""

from typing import Final

import lightgbm as lgb
import numpy as np
import pytest

from bikechance_ml.features.constants import FEATURE_SET, HORIZONS_MIN
from bikechance_ml.features.schema import SERVING_SCHEMA
from bikechance_ml.models import artifact as lightgbm_artifact
from bikechance_ml.models import matrix

N_COLUMNS: Final[int] = len(matrix.MODEL_COLUMNS)


def _boosters(rounds: int = 12) -> dict[str, lgb.Booster]:
    """小さな森を 2 つ当てはめる。**本番と同じ列数・同じカテゴリの位置**で作る。"""
    rng = np.random.default_rng(20260910)
    rows = 4_000
    features = rng.normal(size=(rows, N_COLUMNS))
    for index in matrix.categorical_indices():
        features[:, index] = rng.integers(0, 3, size=rows)
    features[rng.random((rows, N_COLUMNS)) < 0.05] = np.nan
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


def _artifact() -> lightgbm_artifact.LightGbmArtifact:
    return lightgbm_artifact.build(
        boosters=_boosters(),
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
    restored = lightgbm_artifact.from_bytes(lightgbm_artifact.to_bytes(ARTIFACT))
    assert restored == ARTIFACT


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


# ── 予測 ──────────────────────────────────────────────────────
def test_predict_matches_lightgbm() -> None:
    """**読み直した森が、当てはめた森と同じ値を出す。**"""
    rng = np.random.default_rng(7)
    values = rng.normal(size=(500, N_COLUMNS))
    for index in matrix.categorical_indices():
        values[:, index] = rng.integers(0, 3, size=500)
    values[rng.random((500, N_COLUMNS)) < 0.05] = np.nan
    built = matrix.Matrix(values=values, columns=matrix.MODEL_COLUMNS)

    restored = lightgbm_artifact.from_bytes(lightgbm_artifact.to_bytes(ARTIFACT))
    boosters = lightgbm_artifact.load_boosters(restored)
    for booster in boosters.values():
        expected = booster.predict(values)
        assert np.allclose(lightgbm_artifact.predict(booster, built), expected)


def test_the_predictor_returns_both_targets() -> None:
    predictor = lightgbm_artifact.to_predictor(ARTIFACT)
    assert predictor.kind == "lightgbm"
    assert predictor.feature_set == FEATURE_SET
    assert set(predictor.boosters) == {"bike", "dock"}


def test_a_global_model_has_no_unknown_ports() -> None:
    """**全ポート共通**なので「成果物が知らないポート」が無い（B2 と違う）。"""
    predictor = lightgbm_artifact.to_predictor(ARTIFACT)
    assert predictor.unknown_ports("hellocycling", SERVING_SCHEMA.empty_table()) == 0
