"""どの版を配るか（`models/registry.py`）。

**主題は 3 つ。**
  * 「いま配る版」の正は **DB**（`model_versions`）で、環境変数ではない（W4-08）
  * **`kind` で読み方が分かれる**のはここ 1 か所だけ
  * **特徴量の版の照合は、特徴量の表を読むモデルにだけ掛ける**（この非対称が肝）
"""

import subprocess
import sys
from dataclasses import dataclass, field, replace

import pytest

from bikechance_ml.baselines.artifact import artifact_path as baseline_path
from bikechance_ml.baselines.artifact import to_bytes as baseline_to_bytes
from bikechance_ml.features.constants import FEATURE_SET
from bikechance_ml.models import artifact as lightgbm_artifact
from bikechance_ml.models import registry
from tests.test_infer import ARTIFACT as BASELINE
from tests.test_model_artifact import ARTIFACT as LGBM_ARTIFACT


@dataclass
class FakePort:
    """登録簿と Storage の代役。**取りに行った回数を数える。**"""

    rows: dict[str, registry.Registered] = field(default_factory=dict)
    bodies: dict[str, bytes] = field(default_factory=dict)
    fetches: int = 0

    def active_model(self) -> registry.Registered | None:
        return next((one for one in self.rows.values() if one.status == "active"), None)

    def find_model(self, model_version: str) -> registry.Registered | None:
        return self.rows.get(model_version)

    def download(self, bucket: str, path: str) -> bytes | None:
        assert bucket == registry.MODEL_BUCKET
        self.fetches += 1
        return self.bodies.get(path)


def _registered(
    model_version: str, kind: str, feature_set: str, status: str = "active"
) -> registry.Registered:
    path = (
        baseline_path(model_version)
        if kind == registry.BASELINE_KIND
        else lightgbm_artifact.artifact_path(model_version)
    )
    return registry.Registered(
        model_version=model_version,
        kind=kind,
        feature_set=feature_set,
        artifact_path=path,
        status=status,
    )


def _baseline_port(feature_set: str = FEATURE_SET, status: str = "active") -> FakePort:
    """**成果物と登録簿の `feature_set` をそろえる**（食い違いは別の検査で見る）。"""
    artifact = replace(BASELINE, feature_set=feature_set)
    row = _registered(artifact.model_version, registry.BASELINE_KIND, feature_set, status)
    return FakePort(
        rows={row.model_version: row}, bodies={row.artifact_path: baseline_to_bytes(artifact)}
    )


def _lightgbm_port(feature_set: str = FEATURE_SET, status: str = "candidate") -> FakePort:
    artifact = replace(LGBM_ARTIFACT, feature_set=feature_set)
    row = _registered(artifact.model_version, registry.LIGHTGBM_KIND, feature_set, status)
    return FakePort(
        rows={row.model_version: row},
        bodies={row.artifact_path: lightgbm_artifact.to_bytes(artifact)},
    )


@pytest.fixture(autouse=True)
def _clean() -> None:
    registry.forget()


# ── 「いま配る版」を引く ──────────────────────────────────────
def test_the_active_row_is_the_source_of_truth() -> None:
    port = _baseline_port()
    assert registry.active(port).model_version == BASELINE.model_version


def test_no_active_row_stops() -> None:
    """**環境変数に落とさない**（正を 2 つ作らない）。"""
    with pytest.raises(registry.NoActiveModelError):
        registry.active(FakePort())


def test_a_version_can_be_named() -> None:
    port = _lightgbm_port()
    assert registry.named(port, LGBM_ARTIFACT.model_version).kind == registry.LIGHTGBM_KIND


def test_an_unknown_version_stops() -> None:
    with pytest.raises(registry.UnknownModelError):
        registry.named(FakePort(), "知らない版")


# ── 読み方の分岐 ──────────────────────────────────────────────
def test_a_baseline_row_loads_the_baseline() -> None:
    port = _baseline_port()
    predictor = registry.load(port, registry.active(port))
    assert predictor.kind == "baseline"
    assert predictor.model_version == BASELINE.model_version


def test_a_lightgbm_row_loads_lightgbm() -> None:
    port = _lightgbm_port()
    predictor = registry.load(port, registry.named(port, LGBM_ARTIFACT.model_version))
    assert predictor.kind == "lightgbm"


def test_an_unknown_kind_stops() -> None:
    row = _registered("x", "xgboost", FEATURE_SET)
    port = FakePort(rows={"x": row}, bodies={row.artifact_path: b"{}"})
    with pytest.raises(registry.UnknownModelError):
        registry.load(port, row)


def test_a_missing_artifact_stops() -> None:
    """**代わりの値をでっち上げない。**"""
    port = _baseline_port()
    port.bodies.clear()
    with pytest.raises(registry.MissingArtifactError):
        registry.load(port, registry.active(port))


# ── 特徴量の版の照合（**非対称**）──────────────────────────────
def test_lightgbm_refuses_a_different_feature_set() -> None:
    """**61 列すべてを読むので、版が違えば意味の違う列を見る。**"""
    port = _lightgbm_port(feature_set="v1")
    with pytest.raises(registry.FeatureSetMismatchError):
        registry.load(port, registry.named(port, LGBM_ARTIFACT.model_version))


def test_lightgbm_accepts_the_matching_feature_set() -> None:
    port = _lightgbm_port(feature_set=FEATURE_SET)
    assert registry.load(port, registry.named(port, LGBM_ARTIFACT.model_version)) is not None


def test_the_baseline_is_not_bound_to_the_feature_set() -> None:
    """**いま配っている版は `v0` で当てはめたもの**（配信側は v3）。

    B0〜B3 が読むのは 6 列（システム・ポート・日・水平・日内分・目標の曜日種別）で、
    **v0 から v3 まで 1 つも変わっていない**。だから止めない。この非対称は
    「特徴量の表を読むモデルだけが版に縛られる」という区別である。
    """
    port = _baseline_port(feature_set="v0")
    predictor = registry.load(port, registry.active(port))
    assert predictor.feature_set == "v0"
    assert predictor.feature_set != FEATURE_SET


def test_a_row_that_disagrees_with_the_artifact_stops() -> None:
    """**登録簿の写しが成果物と食い違ったら止める**（種類によらず）。"""
    port = _baseline_port(feature_set="v0")
    row = registry.active(port)
    lying = registry.Registered(
        model_version=row.model_version,
        kind=row.kind,
        feature_set="v9",
        artifact_path=row.artifact_path,
        status=row.status,
    )
    with pytest.raises(registry.FeatureSetMismatchError, match="登録簿"):
        registry.load(port, lying)


# ── 取り直さない ──────────────────────────────────────────────
def test_the_same_version_is_fetched_once() -> None:
    """**3.2 MB を 5 分毎に取り直さない**（開発プラン §8.2）。"""
    port = _baseline_port()
    row = registry.active(port)
    registry.load(port, row)
    registry.load(port, row)
    assert port.fetches == 1


def test_a_new_version_replaces_the_cache() -> None:
    port = _baseline_port()
    registry.load(port, registry.active(port))
    renamed = replace(BASELINE, model_version="baseline-b3-v0-99999999")
    other = _registered(renamed.model_version, registry.BASELINE_KIND, FEATURE_SET)
    port.rows[other.model_version] = other
    port.bodies[other.artifact_path] = baseline_to_bytes(renamed)
    registry.load(port, other)
    assert port.fetches == 2


# ── 依存が条件付きであること ──────────────────────────────────
def test_serving_does_not_import_lightgbm() -> None:
    """**ベースラインを配っているあいだは lightgbm も scipy も読み込まない。**

    `models/registry.py` が `kind` の枝の中で import している、という約束を機械で守る。
    トップレベルに動かすと、`/ml/infer`・`/ml/compact`・`/ml/reference`・`/ml/weather` の
    **すべてが 0.19 秒と 110 MB ぶんの読み込みを毎回**払うことになる。

    **別のプロセスで確かめる。** この検査ファイル自身が `models/artifact` を import して
    いるので、同じプロセスでは `sys.modules` に残っている。
    """
    code = (
        "import sys, bikechance_ml.api as api; api.build_app();"
        " print('lightgbm' in sys.modules,"
        " any(one.startswith('scipy') for one in sys.modules))"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "False False", done.stdout
