"""どの版を配るか（`models/registry.py`）。

**主題は 4 つ。**
  * 「いま配る版」の正は **DB**（`model_versions`）で、環境変数ではない（W4-08）
  * **`kind` で読み方が分かれる**のはここ 1 か所だけ
  * **特徴量の版の照合は、特徴量の表を読むモデルにだけ掛ける**（この非対称が肝）
  * **配信中（active・shadow）の版と同じ名前では、成果物を置かない**（W6-19、契約 38）

配信の経路が `lightgbm` を読み込まないことは `tests/test_serving_imports.py` が見る。
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Final

import pyarrow as pa
import pytest

from bikechance_ml.baselines.artifact import artifact_path as baseline_path
from bikechance_ml.baselines.artifact import to_bytes as baseline_to_bytes
from bikechance_ml.features.constants import FEATURE_SET
from bikechance_ml.features.schema import PROFILE_COLUMNS
from bikechance_ml.models import artifact as lightgbm_artifact
from bikechance_ml.models import registry
from bikechance_ml.models.predictor import BaselinePredictor, Prediction
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


def test_a_new_version_is_fetched_and_the_old_one_stays() -> None:
    """**新しい版は取りに行き、前の版は残る**（捨てるのは `retain` と上限。W6-02）。"""
    port = _baseline_port()
    first = registry.active(port)
    registry.load(port, first)
    other = _add_version(port, "baseline-b3-v0-99999999", "active")
    registry.load(port, other)
    registry.load(port, first)
    assert port.fetches == 2


# ── 版ごとに持つ（W6-02、契約 31）──────────────────────────────
def _add_version(port: FakePort, model_version: str, status: str) -> registry.Registered:
    """同じ中身を別の版の名前で置き、登録する。**取りに行った回数だけを見る**検査に使う。"""
    renamed = replace(BASELINE, model_version=model_version)
    row = _registered(model_version, registry.BASELINE_KIND, FEATURE_SET, status)
    port.rows[model_version] = row
    port.bodies[row.artifact_path] = baseline_to_bytes(renamed)
    return row


def test_the_active_and_the_shadow_are_kept_side_by_side() -> None:
    """**active と shadow を毎周期読んでも、取り直さない**（所見 189）。

    1 つだけ持つと互いを追い出し、毎周期 2 つとも落とし直す（版 1 の成果物なら月 300 GB）。
    """
    port = _baseline_port()
    active = registry.active(port)
    shadow = _add_version(port, "baseline-b3-v0-shadow", "shadow")
    for _ in range(3):
        registry.retain({active.model_version, shadow.model_version})
        registry.load(port, active)
        registry.load(port, shadow)
    assert port.fetches == 2


def test_retain_drops_only_the_versions_not_in_use() -> None:
    """**その周期に使う版だけを残す**——下ろした shadow は捨て、active は残す。"""
    port = _baseline_port()
    active = registry.active(port)
    shadow = _add_version(port, "baseline-b3-v0-shadow", "shadow")
    registry.load(port, active)
    registry.load(port, shadow)
    registry.retain({active.model_version})
    registry.load(port, active)
    assert port.fetches == 2, "残した版は取り直さない"
    registry.load(port, shadow)
    assert port.fetches == 3, "捨てた版は取り直す"


def test_the_cache_drops_the_least_recently_used_beyond_the_limit() -> None:
    """**上限を超えたら、いちばん長く使っていない版から捨てる**（`CACHE_LIMIT`）。"""
    port = _baseline_port()
    rows = [
        _add_version(port, f"baseline-b3-v0-9999000{index}", "candidate")
        for index in range(registry.CACHE_LIMIT + 1)
    ]
    for row in rows[: registry.CACHE_LIMIT]:
        registry.load(port, row)
    registry.load(port, rows[0])  # 使い直す（いちばん新しくなる）
    registry.load(port, rows[registry.CACHE_LIMIT])  # 上限を超える
    fetched = port.fetches
    registry.load(port, rows[0])
    assert port.fetches == fetched, "使い直した版は残る"
    registry.load(port, rows[1])
    assert port.fetches == fetched + 1, "いちばん長く使っていない版が捨てられている"


def test_many_cycles_at_once_keep_the_cache_whole() -> None:
    """**2 系統の周期が同じプロセスで並行に走っても壊れない**（`/ml/infer` は同期の口）。"""
    port = _baseline_port()
    active = registry.active(port)
    shadow = _add_version(port, "baseline-b3-v0-shadow", "shadow")

    def cycle(_: int) -> None:
        registry.retain({active.model_version, shadow.model_version})
        registry.load(port, active)
        registry.load(port, shadow)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(cycle, range(400)))
    fetched = port.fetches
    cycle(0)
    assert port.fetches == fetched, "並行に回した後も、2 つとも持っている"


#: ほかのスレッドがロックで待たされていることを確かめる時間。
LOCK_WAIT_S: Final[float] = 0.2


def test_the_cache_is_touched_only_under_its_lock() -> None:
    """**キャッシュの出し入れは、ロックを握ってから**（上の検査の、確率に頼らない形）。

    並行の壊れ方は確率的で、並べて回すだけでは必ずは再現しない。ここでは**ロックを握って
    いる間、ほかのスレッドの `retain` と `load` が待たされる**ことを確かめる。
    """
    port = _baseline_port()
    active = registry.active(port)
    registry.load(port, active)
    workers = [
        threading.Thread(target=registry.retain, args=({active.model_version},)),
        threading.Thread(target=registry.load, args=(port, active)),
    ]
    with registry._CACHE_LOCK:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=LOCK_WAIT_S)
        assert all(worker.is_alive() for worker in workers), "ロックを握っている間に触った"
    for worker in workers:
        worker.join()
    assert port.fetches == 1


# ── 上書きの防止（W6-19、契約 38）──────────────────────────────
@dataclass
class ShelfPort:
    """登録簿を名前で引いて、成果物を置くだけの代役。**置いたものを覚える。**"""

    rows: dict[str, registry.Registered] = field(default_factory=dict)
    uploads: list[tuple[str, str, bytes, str]] = field(default_factory=list)

    def find_model(self, model_version: str) -> registry.Registered | None:
        return self.rows.get(model_version)

    def upload(self, bucket: str, path: str, body: bytes, content_type: str) -> None:
        self.uploads.append((bucket, path, body, content_type))


NAME = "baseline-b3-v0-20260928"
PATH = baseline_path(NAME)


def _shelf(status: str | None) -> ShelfPort:
    """その名前が `status` で登録された登録簿（`None` なら登録が無い）。"""
    if status is None:
        return ShelfPort()
    return ShelfPort(rows={NAME: _registered(NAME, registry.BASELINE_KIND, FEATURE_SET, status)})


@pytest.mark.parametrize("status", ["active", "shadow"])
def test_a_serving_name_is_not_overwritten(status: str) -> None:
    """**配信中の版と同じ名前では置かない。** 置けば、昇格を経ずに配る値が変わる（所見 157）。"""
    port = _shelf(status)
    with pytest.raises(registry.ServingVersionError, match=status):
        registry.upload_artifact(port, NAME, PATH, b"body", "application/gzip")
    assert port.uploads == []


@pytest.mark.parametrize("status", ["candidate", "retired", None])
def test_a_name_that_is_not_serving_can_be_put(status: str | None) -> None:
    """**candidate・retired・登録の無い名前は置ける**（`register_model_version` と同じ範囲）。"""
    port = _shelf(status)
    registry.upload_artifact(port, NAME, PATH, b"body", "application/gzip")
    assert port.uploads == [(registry.MODEL_BUCKET, PATH, b"body", "application/gzip")]


def test_refusing_looks_up_only_the_given_name() -> None:
    """**止めるのは同じ名前だけ。** 別の版が active でも、新しい名前は置ける。"""
    port = ShelfPort(
        rows={"other": _registered("other", registry.BASELINE_KIND, FEATURE_SET, "active")}
    )
    registry.refuse_serving(port, NAME)
    with pytest.raises(registry.ServingVersionError):
        registry.refuse_serving(port, "other")


def test_the_serving_statuses_are_the_ones_the_database_protects() -> None:
    """**DB（0038）の `register_model_version` が登録し直させないのと同じ 2 つ。**

    ここを広げる（例えば candidate を足す）と、当てはめ直しの日課が止まる。狭める
    （shadow を外す）と、shadow の成果物を黙って差し替えられる。どちらも決め直しである。
    """
    assert frozenset({"active", "shadow"}) == registry.SERVING_STATUSES


# ── `prof_*` を読むモデルか（W6 の契約 33）───────────────────────
def test_the_baseline_does_not_read_the_profile() -> None:
    """**B3 は `prof_*` を読まない**（読むのは 6 列と台数）。プロファイルが無い周期も配れる。"""
    assert registry.reads_profile(BaselinePredictor(artifact=BASELINE)) is False


def test_a_v4_forest_reads_the_profile() -> None:
    """**v4 の森は `prof_*` を読む**（成果物の列に入っている）。読めなかった周期には配らない。"""
    assert any(name in PROFILE_COLUMNS for name in LGBM_ARTIFACT.columns)
    assert registry.reads_profile(lightgbm_artifact.to_predictor(LGBM_ARTIFACT)) is True


def test_a_forest_without_profile_columns_does_not() -> None:
    """**決めるのは成果物の列**（種類の名前ではない）。`prof_*` の無い森は読まない。"""
    columns = tuple(name for name in LGBM_ARTIFACT.columns if name not in PROFILE_COLUMNS)
    forest = replace(LGBM_ARTIFACT, columns=columns)
    assert registry.reads_profile(lightgbm_artifact.to_predictor(forest)) is False


@dataclass(frozen=True)
class _Unknown:
    """**知らない種類**の予測器（合成器より前に作られた道の外）。"""

    model_version: str = "someday-v1"
    kind: str = "composite"
    feature_set: str = FEATURE_SET

    def predict(self, system_id: str, at: datetime, table: pa.Table) -> Prediction:
        raise AssertionError("呼ばれないはず")

    def unknown_ports(self, system_id: str, table: pa.Table) -> int:
        return 0


def test_an_unknown_kind_is_taken_to_read_the_profile() -> None:
    """**分からないときは「読む」**——読めなかった周期に配らない側に倒す。"""
    assert registry.reads_profile(_Unknown()) is True
