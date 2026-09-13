"""ベースラインの成果物（`baselines/artifact.py`、W3 プラン §5.10）。

**この 1 ファイルの主題は「学習したものと配るものが同じであること」。**
書き出して読み直したモデルが、元のモデルと同じ確率を返さなければ、
配信は学習と別のことをしている。
"""

import gzip
import json

import numpy as np
import pytest

from bikechance_ml.baselines import climatology, conditional
from bikechance_ml.baselines.artifact import FORMAT_VERSION, Artifact, from_bytes, to_bytes
from bikechance_ml.baselines.climatology import FromSamples
from bikechance_ml.eval.dataset import TARGETS, to_samples
from bikechance_ml.jobs.fit_baseline import build_artifact, model_version_for
from tests import eval_fixture as fixture

BIKE, DOCK = TARGETS
DAY0, DAY1, DAY2 = fixture.DAYS


def scenario() -> list[dict[str, object]]:
    """3 日 × 2 システム × 2 水平 × 台数違い。**同じセルが 2 日にまたがる。**"""
    return [
        fixture.row(day, system, station, horizon, bikes, 9 - bikes, 1 if bikes else 0, 1)
        for day in fixture.DAYS
        for system in ("hellocycling", "docomo-cycle")
        for horizon in (5, 60)
        for station, bikes in (("a", 0), ("b", 1), ("c", 7))
    ]


def built() -> Artifact:
    samples = to_samples(fixture.to_table(scenario()))
    return build_artifact(samples, fixture.DAYS, FromSamples())


ARTIFACT = built()


def test_version_names_the_last_training_day() -> None:
    """**いつまでのデータで作ったかが名前で分かる。**"""
    assert model_version_for(fixture.DAYS) == "baseline-b3-v0-20260909"


def test_round_trip_keeps_the_shape() -> None:
    restored = from_bytes(to_bytes(ARTIFACT))
    assert restored.model_version == ARTIFACT.model_version
    assert restored.systems == ARTIFACT.systems
    assert restored.ports == ARTIFACT.ports
    assert restored.horizons_min == ARTIFACT.horizons_min
    assert restored.train_days == ARTIFACT.train_days


def test_round_trip_keeps_the_probabilities() -> None:
    """**書き出して読み直しても同じ確率を返す。** ここがずれたら配信は別物になる。"""
    samples = to_samples(fixture.to_table(scenario()))
    restored = from_bytes(to_bytes(ARTIFACT))
    for target in TARGETS:
        before, _ = conditional.predict(ARTIFACT.targets[target.name].b1, samples, target)
        after, _ = conditional.predict(restored.targets[target.name].b1, samples, target)
        assert after.tolist() == pytest.approx(before.tolist(), abs=1e-6)


def test_round_trip_keeps_the_climatology_cells() -> None:
    samples = to_samples(fixture.to_table(scenario()))
    restored = from_bytes(to_bytes(ARTIFACT))
    fallback = np.full(len(samples), 0.5)
    for target in TARGETS:
        before = climatology.predict(ARTIFACT.targets[target.name].b2, samples, fallback)
        after = climatology.predict(restored.targets[target.name].b2, samples, fallback)
        assert after.fell_back == before.fell_back
        assert after.probability.tolist() == pytest.approx(before.probability.tolist(), abs=1e-6)


def test_round_trip_keeps_the_blend_coefficients() -> None:
    restored = from_bytes(to_bytes(ARTIFACT))
    for target in TARGETS:
        before = ARTIFACT.targets[target.name].b3
        after = restored.targets[target.name].b3
        assert after.intercept == pytest.approx(before.intercept, abs=1e-6)
        assert after.weights.tolist() == pytest.approx(before.weights.tolist(), abs=1e-6)
        assert after.scale.tolist() == pytest.approx(before.scale.tolist(), abs=1e-6)


def test_serialisation_is_deterministic() -> None:
    """**同じ成果物からは同じバイト列が出る**（gzip の時刻も固定する）。"""
    assert to_bytes(ARTIFACT) == to_bytes(ARTIFACT)


def test_only_usable_climatology_cells_are_stored() -> None:
    """**597 万セルを全部書き出さない。** 使えるセルだけ番号と値で持つ。"""
    document = json.loads(gzip.decompress(to_bytes(ARTIFACT)).decode())
    keys = document["targets"]["bike"]["b2_keys"]
    assert len(keys) == ARTIFACT.targets["bike"].b2.cells
    assert len(keys) == len(document["targets"]["bike"]["b2_rate"])


def test_unknown_format_version_is_refused() -> None:
    """**読み方を変えたら版を上げる。** 古い成果物を新しい読み方で読まない。"""
    document = json.loads(gzip.decompress(to_bytes(ARTIFACT)).decode())
    document["format_version"] = FORMAT_VERSION + 1
    body = gzip.compress(json.dumps(document).encode(), mtime=0)
    with pytest.raises(ValueError, match="書式"):
        from_bytes(body)


def test_climatology_needs_two_days() -> None:
    """フィクスチャは 3 日あるので、セルが立つ（W3 プラン §12 の 101）。"""
    assert ARTIFACT.targets["bike"].b2.min_days == 2
    assert ARTIFACT.targets["bike"].b2.cells > 0


def test_describe_mentions_the_version_and_cells() -> None:
    text = ARTIFACT.describe()
    assert ARTIFACT.model_version in text
    assert "気候値" in text
