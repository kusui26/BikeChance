"""モデルに渡す行列（`models/matrix.py`）。

**主題は「学習と推論で同じ行列になること」。** 列が 1 つずれても、カテゴリの番号が
1 つずれても、**例外は出ず確率だけが静かに変わる**。だから機械で固定する。

`tests/test_features_parity.py` は 61 列の**値**が一致することを見ている。ここで見るのは
その値を**行列に並べる規則**——順序・型・符号化・欠損の扱いである。
"""

import inspect
import math
from typing import Final

import numpy as np
import pyarrow as pa
import pytest

from bikechance_ml.features import build
from bikechance_ml.features.calendar import DAY_TYPES, DOW_TYPES
from bikechance_ml.features.schema import SCHEMA, feature_columns
from bikechance_ml.models import matrix
from tests import features_fixture as fixture

BUILT: Final[pa.Table] = build.build_day(fixture.build_inputs()).table


# ── 列の並び ──────────────────────────────────────────────────
def test_columns_are_the_feature_set_plus_system() -> None:
    """**69 列。** `feature_columns()` の 68（v4）に `system_id` を足したもの。"""
    assert matrix.MODEL_COLUMNS[0] == "system_id"
    assert matrix.MODEL_COLUMNS[1:] == feature_columns()
    assert len(matrix.MODEL_COLUMNS) == 69


def test_station_id_is_not_a_feature() -> None:
    """**21,000 カテゴリを入れない**（開発プラン §7.2）。"""
    assert "station_id" not in matrix.MODEL_COLUMNS


def test_labels_and_weights_are_not_features() -> None:
    """ラベルは未来、重みは学習の都合。**どちらもモデルに渡さない。**"""
    for name in ("y_bike", "y_dock", "weight", "stratum", "t", "feature_set"):
        assert name not in matrix.MODEL_COLUMNS


# ── カテゴリ ──────────────────────────────────────────────────
def test_categorical_columns_have_no_order() -> None:
    """**順序を持たない列だけ**をカテゴリにする。"""
    names = [matrix.MODEL_COLUMNS[index] for index in matrix.categorical_indices()]
    assert set(names) == set(matrix.CATEGORICAL_COLUMNS)


def test_pref_code_is_not_categorical() -> None:
    """開発プラン §7.2：**近傍が無いポートで `pref` は全体平均より当たらない**（W3-07）。"""
    assert "pref_code" not in matrix.CATEGORICAL_COLUMNS
    assert "pref_code" in matrix.MODEL_COLUMNS


def test_vocabularies_come_from_the_code() -> None:
    """**学習データから作らない。** 出てこなかった値で番号がずれない。"""
    assert matrix.VOCABULARIES["day_type"] == tuple(DAY_TYPES)
    assert matrix.VOCABULARIES["dow_type"] == tuple(DOW_TYPES)
    assert matrix.VOCABULARIES["target_dow_type"] == tuple(DOW_TYPES)


def test_every_string_column_has_a_vocabulary() -> None:
    """**文字列を素の番号にしない。** 語彙が無い文字列列があれば取りこぼしている。"""
    strings = [
        name
        for name in matrix.MODEL_COLUMNS
        if name != "system_id" and SCHEMA.field(name).type == pa.string()
    ]
    assert set(strings) <= set(matrix.VOCABULARIES)


def test_an_unknown_category_stops() -> None:
    """**黙って別の番号に落とさない**（暦の規則を足したときに気づく）。"""
    table = BUILT.set_column(
        BUILT.schema.get_field_index("dow_type"),
        "dow_type",
        pa.array(["宇宙の日"] * BUILT.num_rows, type=pa.string()),
    )
    with pytest.raises(matrix.UnknownCategoryError):
        matrix.build(table)


# ── 単調制約 ──────────────────────────────────────────────────
def test_monotone_points_at_the_matching_count() -> None:
    """**当てる対象と同じ側の台数**に非減少（開発プラン §7.2）。"""
    for target, column in (("bike", "bikes"), ("dock", "docks")):
        constraint = matrix.monotone_constraints(target)
        positions = [index for index, value in enumerate(constraint) if value == 1]
        assert [matrix.MODEL_COLUMNS[index] for index in positions] == [column]
        assert len(constraint) == len(matrix.MODEL_COLUMNS)


# ── 値の作り方 ────────────────────────────────────────────────
def test_shape_matches_the_table() -> None:
    built = matrix.build(BUILT)
    assert built.values.shape == (BUILT.num_rows, len(matrix.MODEL_COLUMNS))
    # **float32。** 28 日ぶんの行列を 8.1 → 4.05 GB にするために変えた（§12 の 168）
    assert built.values.dtype == np.float32


def test_the_dtype_does_not_change_the_values() -> None:
    """**型を落としても値は変わらない。**

    仮数 24 ビットで足りることは実データでも確かめてある（2026-09-19、117 万行で
    **62 列のうち 60 列が float64 と完全一致**。違うのは `lat` と `lon` だけで、
    往復の誤差は 7.6e-06 度 ＝ 約 0.6 m）。ここではフィクスチャの全列で、
    **float64 で作ってから落とした値**と一致することを見る。
    """
    built = matrix.build(BUILT)
    for index, name in enumerate(built.columns):
        exact = np.asarray(matrix._column(BUILT, name), dtype=np.float64)
        assert np.array_equal(built.values[:, index], exact.astype(matrix.DTYPE), equal_nan=True), (
            f"{name} が型を落とす前後で違います"
        )


def test_keep_selects_rows_without_copying_the_table() -> None:
    """**`keep` は `table.filter(...)` と同じ結果を出す。**

    先に `filter` を通すと**表まるごとの写し**ができる。28 日ぶんではそれだけで
    4.3 GB になるので、**行を選ぶのは列ごと**にした（§12 の 168）。
    ここでは「同じ答えになること」を、古いやり方と突き合わせて留める。
    """
    keep = np.zeros(BUILT.num_rows, dtype=np.bool_)
    keep[::2] = True
    picked = matrix.build(BUILT, keep)
    filtered = matrix.build(BUILT.filter(pa.array(keep)))
    assert picked.values.shape == (int(keep.sum()), len(matrix.MODEL_COLUMNS))
    assert np.array_equal(picked.values, filtered.values, equal_nan=True)


def test_keep_none_takes_every_row() -> None:
    assert len(matrix.build(BUILT, None)) == BUILT.num_rows
    assert len(matrix.build(BUILT)) == BUILT.num_rows


def test_an_empty_selection_gives_an_empty_matrix() -> None:
    """**1 行も選ばなくても落ちない**（形は保つ）。"""
    none = matrix.build(BUILT, np.zeros(BUILT.num_rows, dtype=np.bool_))
    assert none.values.shape == (0, len(matrix.MODEL_COLUMNS))


def test_building_does_not_hold_twice_the_matrix() -> None:
    """**置き場所を先に確保して 1 列ずつ埋める**（`np.column_stack` を使わない）。

    素直に書くと 62 列ぶんの配列を全部作ってから同じ大きさの結果を確保するので、
    **山が行列の 2 倍**になる。28 日ぶんでは 8 GB が 16 GB になり、それだけで
    ランナーに載らない（§12 の 168）。**書き方そのものを固定する。**

    **見るのは `empty` と `fill` である**（2026-09-20 に分けた。§12 の 168 の 2 段目）。
    `build` はその 2 つを 1 回ずつ呼ぶだけになり、**確保も書き込みもしていない。**
    """
    # **本体だけを見る。** docstring は「素直に書くと column_stack になる」と説明して
    # いるので、そこまで含めると必ず引っかかる
    reserve = inspect.getsource(matrix.empty).replace(matrix.empty.__doc__ or "", "")
    write = inspect.getsource(matrix.fill).replace(matrix.fill.__doc__ or "", "")
    assert "np.empty(" in reserve, "先に確保していません"
    assert "column_stack" not in write, "column_stack は行列の 2 倍を持ちます"
    assert "np.empty(" not in write, "埋めるところで確保し直しています"


def test_null_becomes_nan_not_zero() -> None:
    """**0 で埋めない。** LightGBM は欠損を木の中で扱う。"""
    built = matrix.build(BUILT)
    index = matrix.MODEL_COLUMNS.index("region_id")
    # フィクスチャの HELLO は `region_id` を持たない
    assert bool(np.isnan(built.values[:, index]).any())
    assert not bool((built.values[:, index] == 0).all())


def test_booleans_become_zero_and_one() -> None:
    built = matrix.build(BUILT)
    index = matrix.MODEL_COLUMNS.index("is_holiday")
    values = built.values[:, index]
    assert set(values[~np.isnan(values)].tolist()) <= {0.0, 1.0}


def test_strings_use_the_vocabulary_order() -> None:
    built = matrix.build(BUILT)
    index = matrix.MODEL_COLUMNS.index("dow_type")
    expected = [float(DOW_TYPES.index(one)) for one in BUILT.column("dow_type").to_pylist()]
    assert built.values[:, index].tolist() == expected


def test_a_column_that_is_missing_stops() -> None:
    """**黙って NaN で埋めない。** 表に列が無ければ pyarrow が例外にする。"""
    trimmed = BUILT.drop_columns(["bikes"])
    with pytest.raises(KeyError):
        matrix.build(trimmed)


def test_the_matrix_is_deterministic() -> None:
    """2 回作って**同じ値**（乱数も辞書順も入っていない）。"""
    first, second = matrix.build(BUILT), matrix.build(BUILT)
    assert np.array_equal(first.values, second.values, equal_nan=True)


def test_nan_is_not_confused_with_a_category() -> None:
    """語彙の中に `None` が混ざらない。**欠損は NaN のまま。**"""
    values = matrix.build(BUILT).values[:, matrix.MODEL_COLUMNS.index("muni_code")]
    assert all(math.isnan(one) or one >= 0 for one in values.tolist())


# ── 先に確保して日ごとに埋める（W5 プラン §12 の 168 の 2 段目）──────
def test_filling_in_two_goes_matches_building_at_once() -> None:
    """**区間に分けて埋めても、まとめて作ったのと同じ値になる。**

    ここが崩れると、**日ごとに読む**書き換えが確率を変える。答えが変わらないことを
    突き合わせで留める。
    """
    half = BUILT.num_rows // 2
    built = matrix.empty(BUILT.num_rows)
    at = matrix.fill(built, 0, BUILT.slice(0, half))
    at = matrix.fill(built, at, BUILT.slice(half))

    assert at == BUILT.num_rows
    assert np.array_equal(built.values, matrix.build(BUILT).values, equal_nan=True)


def test_filling_past_the_end_stops() -> None:
    """**確保した外へは書かない。** 黙って切り詰めると、どの行が落ちたか分からない。"""
    built = matrix.empty(BUILT.num_rows)
    matrix.fill(built, 0, BUILT)
    with pytest.raises(matrix.MatrixOverflowError):
        matrix.fill(built, BUILT.num_rows, BUILT.slice(0, 1))


def test_an_unfilled_matrix_is_refused() -> None:
    """**埋め残しは例外にする。** `np.empty` の中身は未定義で、例外は出ない。"""
    built = matrix.empty(BUILT.num_rows + 1)
    at = matrix.fill(built, 0, BUILT)
    with pytest.raises(matrix.UnfilledMatrixError):
        matrix.refuse_unfilled(built, at)


def test_a_full_matrix_passes_the_check() -> None:
    built = matrix.build(BUILT)
    matrix.refuse_unfilled(built, len(built))


def test_filling_with_a_mask_takes_only_those_rows() -> None:
    """`keep` を渡したときも、**埋めるのは選んだ行数ぶんだけ**。"""
    keep = np.zeros(BUILT.num_rows, dtype=np.bool_)
    keep[::2] = True
    built = matrix.empty(int(keep.sum()))
    at = matrix.fill(built, 0, BUILT, keep)

    assert at == int(keep.sum())
    assert np.array_equal(built.values, matrix.build(BUILT, keep).values, equal_nan=True)
