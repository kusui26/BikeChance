"""どの版を配るかを引き、成果物を読む（W4 プラン §6.5、開発プラン §8.4）。

**「いま何を配っているか」の正は DB**（`model_versions`）である。W3 の段 8 では
環境変数（`BASELINE_MODEL_VERSION`）で指定していたが、配るものが 2 つ以上になった
時点で登録簿に移した（W4-08）。

**種類で分岐するのはここだけ。** `jobs/infer.py` は `Predictor` を 1 つ受け取るだけで、
ベースラインか LightGBM かを知らない。

**`lightgbm` の import はこの中の枝でだけ行う。** ベースラインを配っているあいだ
（W4 の既定）は LightGBM も scipy も読み込まれない（0.19 秒と 110 MB ぶんの
読み込みが要らない）。CLAUDE.md §3 の「条件付き依存のみ動的 import」に当たる。
"""

from dataclasses import dataclass
from typing import Final, Protocol

from bikechance_ml.baselines.artifact import from_bytes as baseline_from_bytes
from bikechance_ml.features.constants import FEATURE_SET
from bikechance_ml.models.predictor import BaselinePredictor, Predictor

#: 成果物の置き場所（0027 のバケット）。**`gbfs-parquet` には相乗りさせない**：
#: あちらは Parquet の MIME しか許さず、寿命も作り直し方も違う（W3 プラン §12 の 106）。
MODEL_BUCKET: Final[str] = "models"

#: `model_versions.kind` の値。
BASELINE_KIND: Final[str] = "baseline"
LIGHTGBM_KIND: Final[str] = "lightgbm"


@dataclass(frozen=True)
class Registered:
    """`model_versions` の 1 行のうち、配るのに要る分だけ。"""

    model_version: str
    kind: str
    feature_set: str
    artifact_path: str
    status: str


class NoActiveModelError(RuntimeError):
    """配る版が登録されていない。**環境変数に落とさない**（正を 2 つ作らない）。"""


class UnknownModelError(RuntimeError):
    """指定された版が登録簿に無い。"""


class MissingArtifactError(RuntimeError):
    """成果物が Storage に無い。**代わりの値をでっち上げない。**"""


class FeatureSetMismatchError(RuntimeError):
    """成果物の特徴量の版が、いま作っている版と違う。**配らない。**"""


class ReadsModels(Protocol):
    """登録簿と成果物を読む口だけ。"""

    def active_model(self) -> Registered | None: ...
    def find_model(self, model_version: str) -> Registered | None: ...
    def download(self, bucket: str, path: str) -> bytes | None: ...


#: 読み込んだ版。**同じ版なら取り直さない**（成果物は 3.2 MB あり、5 分毎に取り直すと
#: 1 日 900 MB の転送になる。開発プラン §8.2）。冷えれば消えるだけで正しさに影響しない。
_CACHE: dict[str, Predictor] = {}


def forget() -> None:
    """キャッシュを捨てる。**テストが版をまたぐときに使う。**"""
    _CACHE.clear()


def active(source: ReadsModels) -> Registered:
    """いま配る版。**登録が無ければ止める。**"""
    found = source.active_model()
    if found is None:
        raise NoActiveModelError("active な版が model_versions に登録されていません")
    return found


def named(source: ReadsModels, model_version: str) -> Registered:
    """版を名指しで引く（候補を手で試すとき）。"""
    found = source.find_model(model_version)
    if found is None:
        raise UnknownModelError(f"登録されていない版です: {model_version}")
    return found


def load(source: ReadsModels, registered: Registered) -> Predictor:
    """成果物を読んで配信用の口にする。**版が同じなら取り直さない。**"""
    cached = _CACHE.get(registered.model_version)
    if cached is not None:
        return cached
    body = source.download(MODEL_BUCKET, registered.artifact_path)
    if body is None:
        raise MissingArtifactError(f"成果物がありません: {registered.model_version}")
    predictor = _build(registered, body)
    # 版が変わったら古いものは要らない。1 つだけ持つ
    _CACHE.clear()
    _CACHE[registered.model_version] = predictor
    return predictor


def _build(registered: Registered, body: bytes) -> Predictor:
    """`kind` で読み方を分ける。**分岐はここだけ。**"""
    predictor = _read(registered, body)
    _refuse_a_lying_row(registered, predictor.feature_set)
    if registered.kind == LIGHTGBM_KIND:
        _refuse_another_feature_set(predictor.feature_set, registered.model_version)
    return predictor


def _read(registered: Registered, body: bytes) -> Predictor:
    if registered.kind == BASELINE_KIND:
        return BaselinePredictor(artifact=baseline_from_bytes(body))
    if registered.kind == LIGHTGBM_KIND:
        # **条件付きの import。** ベースラインを配っているあいだは lightgbm も
        # scipy も読み込まない（`models/artifact.py` の冒頭）
        from bikechance_ml.models import artifact as lightgbm

        return lightgbm.to_predictor(lightgbm.from_bytes(body))
    raise UnknownModelError(f"知らない kind です: {registered.kind}")


def _refuse_a_lying_row(registered: Registered, model_feature_set: str) -> None:
    """**登録簿と成果物が食い違ったら止める**（種類によらず）。

    `model_versions.feature_set` は問い合わせ用の写しで、**正は成果物**である。
    食い違うのは登録の誤りで、放っておくと「登録簿を見て安心したのに、配っている
    ものは別」という状態になる。
    """
    if registered.feature_set != model_feature_set:
        raise FeatureSetMismatchError(
            f"{registered.model_version}: 登録簿は {registered.feature_set}、"
            f"成果物は {model_feature_set} と言っています"
        )


def _refuse_another_feature_set(model_feature_set: str, model_version: str) -> None:
    """**LightGBM は特徴量の版が一致しなければ配らない**（CLAUDE.md §2 の原則 4）。

    61 列すべてを読むので、版が違えば「同じ名前で意味の違う列」を見る（v1 は容量まわり、
    v2 は `minutes_since_last_change`、v3 は天気）。**例外は出ず、確率だけが静かに変わる。**

    ベースラインには掛けない。あちらが読むのは 6 列（システム・ポート・日・水平・
    日内分・目標の曜日種別）で、**v0 から v3 まで 1 つも変わっていない**。
    「特徴量の表を読むモデルだけが版に縛られる」という区別である。
    """
    if model_feature_set != FEATURE_SET:
        raise FeatureSetMismatchError(
            f"{model_version} は feature_set {model_feature_set} で当てはめたもので、"
            f"いま作っているのは {FEATURE_SET} です"
        )
