"""特徴量の表を、モデルに渡す行列にする（開発プラン §7.2、W4 プラン §6.5）。

**純粋。** 学習も推論も**この 1 実装**を通る。列の順序・型・カテゴリの符号化が
1 つでもずれると、木は別の特徴量を見て分岐する——しかも**例外は出ず、確率だけが
静かに変わる**。だから、ここを 1 か所にして落ちる検査で固定する（W4-04 と同じ考え）。

決めていること。

  * **列は `MODEL_COLUMNS` の順**。`features/schema.py` の `feature_columns()`（61 列）に
    `system_id` を足した 62 列である。`station_id` は入れない（21,000 カテゴリになる。
    ポート固有性は近傍と履歴プロファイルで表す。開発プラン §7.2）
  * **カテゴリは順序を持たない列**。文字列 4 つと、順序に意味の無い整数の ID 2 つ。
    `pref_code` は入れない（実測で近傍の残差に足すものが無く、`muni_code` のほうが
    当たる。W3 プラン §10.2a、開発プラン §7.2）
  * **文字列の語彙はコードの定数から決める**（学習データから作らない）。`day_type` も
    `dow_type` も `features/calendar.py` が持っている閉じた集合なので、
    「学習に出てこなかった値」で番号がずれることが**構造的に起きない**
  * **欠損は NaN のまま渡す**。LightGBM は欠損を扱える（0 で埋めない）
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np
import pyarrow as pa

from bikechance_ml.features.arrays import Float64
from bikechance_ml.features.calendar import DAY_TYPES, DOW_TYPES
from bikechance_ml.features.schema import SCHEMA, feature_columns

#: `system_id` を特徴量として渡す。**`feature_columns()` は鍵として外している**が、
#: 系統ごとの癖（`gap` の有無・容量の意味）はモデルに知らせたい（開発プラン §7.2）。
SYSTEM_COLUMN: Final[str] = "system_id"

#: モデルに渡す列と、その順序。**この並びが成果物に焼き付く。**
MODEL_COLUMNS: Final[tuple[str, ...]] = (SYSTEM_COLUMN, *feature_columns())

#: 文字列の語彙。**コードの定数**（`features/calendar.py`）と `SYSTEM_IDS` から作る。
VOCABULARIES: Final[Mapping[str, tuple[str, ...]]] = {
    SYSTEM_COLUMN: ("docomo-cycle", "hellocycling"),
    "day_type": tuple(DAY_TYPES),
    # **これは LightGBM のカテゴリ番号で、`DOW_TYPE_ORDER` とは別物である。**
    # 語彙は成果物に焼き込まれ、読むときに照合する（`models/artifact.py`）ので、
    # 並びが `DOW_TYPE_ORDER` と違っていても構わない——**揃えようとしない**。
    # 揃えると「1 つの並びを直せば全部直る」ように見えて、実際には
    # 過去の成果物が別の意味になる（W5 プラン §12 の 132）
    "dow_type": tuple(DOW_TYPES),
    "target_dow_type": tuple(DOW_TYPES),
}

#: カテゴリとして渡す列。**順序を持たない列だけ。**
#:
#: `pref_code` は入れない（開発プラン §7.2）。`region_id` は入れる：ドコモの地域 ID で、
#: `muni_code` と同じく順序に意味が無い（W3-07a で整数のカテゴリとして使うと決めた）。
CATEGORICAL_COLUMNS: Final[tuple[str, ...]] = (
    SYSTEM_COLUMN,
    "muni_code",
    "region_id",
    "day_type",
    "dow_type",
    "target_dow_type",
)

#: 単調制約（開発プラン §7.2）。**当てる対象と同じ側の台数に対して非減少**にする。
#: 「自転車が多いほど借りられる確率は下がらない」を構造として持たせ、異常な形を防ぐ。
MONOTONE_COLUMNS: Final[Mapping[str, str]] = {"bike": "bikes", "dock": "docks"}


class UnknownCategoryError(ValueError):
    """語彙に無い値が来た。**黙って別の番号に落とさない。**

    `day_type` も `dow_type` も閉じた集合で、増えるとしたらコードを直したときである。
    そのとき古い成果物は**別の意味の番号**を見ることになるので、ここで止める。
    """


@dataclass(frozen=True)
class Matrix:
    """モデルに渡す行列。**列の順序は `MODEL_COLUMNS` と同じ。**"""

    values: Float64
    columns: tuple[str, ...]

    def __len__(self) -> int:
        return int(self.values.shape[0])


def categorical_indices(columns: Sequence[str] = MODEL_COLUMNS) -> tuple[int, ...]:
    """カテゴリ列の位置。**LightGBM には位置で渡す**（名前ではなく）。"""
    return tuple(index for index, name in enumerate(columns) if name in CATEGORICAL_COLUMNS)


def monotone_constraints(target: str, columns: Sequence[str] = MODEL_COLUMNS) -> tuple[int, ...]:
    """列ごとの単調制約（`+1` / `0`）。**当てる対象で位置が変わる。**"""
    wanted = MONOTONE_COLUMNS[target]
    return tuple(1 if name == wanted else 0 for name in columns)


def build(table: pa.Table) -> Matrix:
    """特徴量の表を行列にする。**学習の表でも推論の表でも同じ結果になる。**

    どちらも `MODEL_COLUMNS` を選ぶだけなので、学習の表に余分にある列（ラベル・重み）は
    自然に落ちる。**足りない列があれば pyarrow が例外にする**（黙って NaN で埋めない）。
    """
    columns = [_column(table, name) for name in MODEL_COLUMNS]
    return Matrix(values=np.column_stack(columns), columns=MODEL_COLUMNS)


def _column(table: pa.Table, name: str) -> Float64:
    """1 列を float64 にする。**NULL は NaN**（0 で埋めない）。"""
    if name in VOCABULARIES:
        return _encoded(table, name)
    values = table.column(name).combine_chunks()
    if SCHEMA.field(name).type == pa.bool_():
        # **真偽は 0/1、NULL は NaN。** `to_numpy` は NULL を含むと object になるので、
        # 先に float へ落としてから numpy に渡す
        floats = values.cast(pa.float64()).to_numpy(zero_copy_only=False)
        return np.asarray(floats, dtype=np.float64)
    return np.asarray(values.to_numpy(zero_copy_only=False), dtype=np.float64)


def _encoded(table: pa.Table, name: str) -> Float64:
    """文字列を語彙の番号にする。**語彙に無い値は例外**（黙って落とさない）。"""
    vocabulary = VOCABULARIES[name]
    order = {value: index for index, value in enumerate(vocabulary)}
    values = table.column(name).to_pylist()
    unknown = {one for one in values if one is not None and one not in order}
    if unknown:
        raise UnknownCategoryError(f"{name} に語彙外の値: {sorted(unknown)}")
    return np.array(
        [np.nan if one is None else float(order[one]) for one in values], dtype=np.float64
    )
