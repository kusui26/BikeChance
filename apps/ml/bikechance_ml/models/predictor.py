"""配信するモデルの共通の口（W4 プラン §6.5）。

**推論はモデルの種類を知らない。** `jobs/infer.py` は `Predictor` を 1 つ受け取り、
`build_now` が作った表を渡して確率を受け取る。ベースラインか LightGBM かで分岐するのは
`models/registry.py` の 1 か所だけである。

`lightgbm` はここでは import しない。ベースラインを配っているあいだ（W4 の既定）は
**LightGBM のコードを 1 行も読み込まない**（`models/artifact.py` が持つ）。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import numpy as np
import pyarrow as pa

from bikechance_ml.baselines import blend, climatology, conditional
from bikechance_ml.baselines.artifact import Artifact
from bikechance_ml.eval.dataset import TARGETS, Samples, Target
from bikechance_ml.features.arrays import Bools, Float64, Int8, Int16
from bikechance_ml.features.calendar import DOW_TYPE_ORDER
from bikechance_ml.features.constants import HORIZONS_MIN
from bikechance_ml.features.grid import JST

#: 成果物に無いポート。**気候値が引けず B1 だけになる**（W3 プラン §12 の 110）。
NO_PORT: int = -1


@dataclass(frozen=True)
class Prediction:
    """1 回ぶんの予測。**行の並びは渡した表のまま。**

    `informed` は「そのモデルが本来の情報で出せたか」。B3 では**気候値が引けたか**で、
    LightGBM では常に真（欠損は木が扱う）。`confidence` の材料になる。
    """

    probability: Mapping[str, Float64]
    informed: Mapping[str, Bools]


class Predictor(Protocol):
    """配信するモデル 1 つ。**種類ごとの違いはこの裏に閉じる。**"""

    @property
    def model_version(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def feature_set(self) -> str:
        """**当てはめたときの特徴量の版。** `inference_log` に記録する。"""

    def predict(self, system_id: str, at: datetime, table: pa.Table) -> Prediction: ...

    def unknown_ports(self, system_id: str, table: pa.Table) -> int:
        """成果物が知らないポートの数。**ポート単位で数える**（行は水平のぶんある）。"""


@dataclass(frozen=True)
class BaselinePredictor:
    """B3（ベースラインの混合）。**W3 の段 8 から配っているもの。**

    読むのは 6 列（システム・ポート・日・水平・日内分・目標の曜日種別）と台数だけで、
    **`FEATURE_SET` が v0 から v3 へ進んでも 1 つも変わっていない**。だから版が違っても
    値は変わらず、照合で止めない（`models/registry.py` の注記）。
    """

    artifact: Artifact

    @property
    def model_version(self) -> str:
        return self.artifact.model_version

    @property
    def kind(self) -> str:
        return "baseline"

    @property
    def feature_set(self) -> str:
        return self.artifact.feature_set

    def predict(self, system_id: str, at: datetime, table: pa.Table) -> Prediction:
        samples = to_samples(self.artifact, system_id, at, table)
        outcome = {target.name: self._one(samples, target) for target in TARGETS}
        return Prediction(
            probability={name: values for name, (values, _) in outcome.items()},
            informed={name: used for name, (_, used) in outcome.items()},
        )

    def _one(self, samples: Samples, target: Target) -> tuple[Float64, Bools]:
        """B3 の確率と、気候値が使えたかどうか。**学習と同じ関数を呼ぶ。**

        **「引けたか」は `climatology` に聞く。** 以前は `b2.probability != b1` と
        確率どうしを見比べていたが、**プロファイルから作った気候値の率はちょうど 1.0 に
        なることが多く**（実測：使えるセルの 53.1%）、B1 の 120 セルにも 1.0 が在る
        （実測：貸出 4・返却 6）。**両方が 1.0 の行を「引けなかった」と数えて
        `confidence` が下がる**（W5 プラン §12 の 144）。
        """
        model = self.artifact.targets[target.name]
        b1, _ = conditional.predict(model.b1, samples, target)
        b2 = climatology.predict(model.b2, samples, b1)
        return blend.predict(model.b3, blend.design(b1, b2.probability, samples.h_min)), b2.used

    def unknown_ports(self, system_id: str, table: pa.Table) -> int:
        ports = set(self.artifact.ports)
        return sum(1 for one in station_ids_of(table) if f"{system_id}/{one}" not in ports)


def station_ids_of(table: pa.Table) -> list[str]:
    """ポートの並び。**行は `(ポート, 水平)` の順**なので、水平の数だけ飛ばす。"""
    values: list[str] = table.column("station_id").to_pylist()
    return values[:: len(HORIZONS_MIN)]


def to_samples(artifact: Artifact, system_id: str, at: datetime, table: pa.Table) -> Samples:
    """`build_now` の出力を、ベースラインが読む形にする。**列を引き写すだけ。**

    B0〜B3 が使うのは 6 つ（システム・ポート・日・水平・日内分・目標の曜日種別）と
    台数だけである。**61 列を作ってから 6 つを引く**のは一見無駄だが、LightGBM を
    入れるときに経路を変えずに済む（W4 プラン §6.3）。

    **ラベルは持たない**（`predict` は読まない）。長さ 0 の配列を入れておく。
    """
    ports = {name: index for index, name in enumerate(artifact.ports)}
    station_ids = table.column("station_id").to_pylist()
    rows = table.num_rows
    return Samples(
        systems=artifact.systems,
        ports=artifact.ports,
        system=np.full(rows, artifact.systems.index(system_id), dtype=np.int8),
        port=np.fromiter(
            (ports.get(f"{system_id}/{one}", NO_PORT) for one in station_ids),
            dtype=np.int32,
            count=rows,
        ),
        day=np.full(rows, at.astimezone(JST).date().toordinal(), dtype=np.int32),
        h_min=_int16(table, "h_min"),
        minute_of_day=_int16(table, "minute_of_day"),
        dow_type=_dow_type_index(table),
        weight=np.ones(rows, dtype=np.float32),
        labels={target.label: np.zeros(0, dtype=np.int8) for target in TARGETS},
        counts={"bikes": _int16(table, "bikes"), "docks": _int16(table, "docks")},
    )


def _int16(table: pa.Table, name: str) -> Int16:
    column = table.column(name).combine_chunks()
    return np.asarray(column.to_numpy(zero_copy_only=False), dtype=np.int16)


def _dow_type_index(table: pa.Table) -> Int8:
    """目標時刻の曜日種別を、**成果物と同じ並び**の番号にする。

    並びの正は `features/calendar.py` の `DOW_TYPE_ORDER` 1 つ。**学習側
    （`eval/dataset.py`）も同じ定数を読む**——以前は両方が別々に `sorted()` を
    呼んでいて、片方を「直す」と成果物が別のセルを指す状態だった
    （W5 プラン §12 の 132）。
    """
    values = table.column("target_dow_type").to_pylist()
    return np.fromiter(
        (DOW_TYPE_ORDER.index(one) for one in values), dtype=np.int8, count=len(values)
    )
