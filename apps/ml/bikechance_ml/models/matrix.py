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
import pyarrow.compute as pc

from bikechance_ml.features.arrays import Bools, Float32, Float64
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

#: モデルに渡す行列の型。
#:
#: **`float32` にしてある。** 仮数 24 ビットで足りることを実データで確かめた
#: （2026-09-19、117 万行）：**62 列のうち 60 列は float64 と完全に一致**し、
#: 違うのは `lat` と `lon` だけで、往復の誤差は **7.6e-06 度（約 0.6 m）**・相対 5e-08。
#: LightGBM は特徴量を高々 255 の区間に量子化するので、この差が分岐を変えることは
#: 事実上ない（**変わらないことは当てはめて確かめる**——`--report` の指標を突き合わせる）。
#:
#: **学習と配信で同じ型を使う。** 別々にすると、当てはめた閾値と配信で比べる値の型が
#: 違い、境界のすぐ近くで分岐が入れ替わる。`refuse_if_different` はそれを見張るが、
#: **見張る前に型を揃えておくほうが確実である。**
#:
#: 28 日ぶんの行列は **8.1 GB → 4.05 GB** になる（W5 プラン §12 の 168）。
DTYPE: Final[type[np.float32]] = np.float32

#: 語彙外の値を報告するときに、何件まで名前を出すか。**全部は出さない**
#: （1,640 万行のうち全部が語彙外なら、例外の文言そのものが巨大になる）。
UNKNOWN_SAMPLE: Final[int] = 10

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

    #: **float32**（`DTYPE`）。行列そのものは 28 日で 4.05 GB になる
    values: Float32
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


def build(table: pa.Table, keep: Bools | None = None) -> Matrix:
    """特徴量の表を行列にする。**学習の表でも推論の表でも同じ結果になる。**

    どちらも `MODEL_COLUMNS` を選ぶだけなので、学習の表に余分にある列（ラベル・重み）は
    自然に落ちる。**足りない列があれば pyarrow が例外にする**（黙って NaN で埋めない）。

    `keep` を渡すと**その行だけ**を取る。`table.filter(...)` を先に通すのと同じ結果に
    なるが、**表まるごとの写しを作らない**——28 日ぶんでは、その写しだけで 4.3 GB に
    なる（W5 プラン §12 の 168）。**選ぶのは列ごと**なので、同時に生きるのは
    「行列と 1 列と、その列の選んだぶん」に収まる。

    **先に置き場所を確保して 1 列ずつ埋める。** 素直に書くと
    `np.column_stack([_column(...) for ...])` になるが、それは **62 列ぶんの配列を
    すべて作ってから、同じ大きさの結果をもう 1 つ確保する**——**山が行列の 2 倍**になる。
    28 日ぶん（1,640 万行）では 8 GB が 16 GB になり、**それだけでランナーに載らない**。
    """
    rows = table.num_rows if keep is None else int(keep.sum())
    values = np.empty((rows, len(MODEL_COLUMNS)), dtype=DTYPE)
    for index, name in enumerate(MODEL_COLUMNS):
        # **1 列ずつ書き写して、その列はすぐ捨てる**（次の周回で参照が切れる）
        column = _column(table, name)
        values[:, index] = column if keep is None else column[keep]
    return Matrix(values=values, columns=MODEL_COLUMNS)


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
    """文字列を語彙の番号にする。**語彙に無い値は例外**（黙って落とさない）。

    **Python のリストを通さない。** `to_pylist()` は 1 列ぶんの Python オブジェクトを
    全部作るので、1,640 万行では**その 1 列だけで 2 GB を超える**（W5 プラン §12 の 168）。
    語彙は高々 4 語なので、**語ごとに当たる場所を塗る**ほうが速くて小さい。

    **語彙外は最後にまとめて数える。** 「非 null なのにどの語にも当たらなかった」行が
    あれば、そこに語彙外の値が居る。**何が居たかは、その行だけを取り出して見る**
    （全行を Python に持ち上げない）。
    """
    vocabulary = VOCABULARIES[name]
    values = table.column(name).combine_chunks()
    codes = np.full(table.num_rows, np.nan, dtype=np.float64)
    for index, word in enumerate(vocabulary):
        hit = np.asarray(pc.fill_null(pc.equal(values, word), False).to_numpy(zero_copy_only=False))
        codes[hit] = float(index)
    missing = np.asarray(pc.is_null(values).to_numpy(zero_copy_only=False))
    stray = np.flatnonzero(np.isnan(codes) & ~missing)
    if stray.size:
        found = sorted({values[int(one)].as_py() for one in stray[:UNKNOWN_SAMPLE]})
        raise UnknownCategoryError(f"{name} に語彙外の値: {found}")
    return codes
