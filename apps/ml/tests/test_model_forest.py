"""LightGBM の木を numpy で歩く（`models/forest.py`、PR E′）。

**この 1 ファイルの主題は「LightGBM とビットまで同じ値が出ること」。**

配信のランタイムに OpenMP が無く `import lightgbm` が落ちるので（W4 プラン §12 の 126）、
予測は自前で行う。**自前で行う以上、一致は宣言ではなく検査で担保する。**

`lightgbm` は**開発の依存**なので、この検査は手元と CI では走り、本番の関数には
入らない。当てはめの側（`jobs/fit_lightgbm.py`）も成果物を書く前に同じ突き合わせを
するので、**壊れた成果物は Storage に置かれない**。
"""

import json
import math
from collections.abc import Mapping, Sequence
from typing import Final

import lightgbm as lgb
import numpy as np
import pytest

from bikechance_ml.features.arrays import Float64
from bikechance_ml.models import forest as tree

#: 検査で使う行数。少なすぎると珍しい枝を踏まない。
ROWS: Final[int] = 8_000

#: 一致の許容。**0 にしない**のは、こちらが木ごとに足し込むのに対し numpy の
#: `sum(axis=1)` が対和で足すため（実測の差は 1e-14 程度で、確率 ×1000 の分解能の
#: 10 桁下）。**規則の違いはこの桁では吸収されない**ので、検査の力は落ちない。
TOLERANCE: Final[float] = 1e-9


def _train(
    values: Float64,
    labels: Float64,
    categorical: Sequence[int] = (),
    rounds: int = 40,
    extra: Mapping[str, object] | None = None,
) -> lgb.Booster:
    params: dict[str, object] = {
        "objective": "binary",
        "num_leaves": 31,
        "min_data_in_leaf": 20,
        "learning_rate": 0.1,
        "verbose": -1,
        "seed": 20260910,
    }
    params.update(extra or {})
    return lgb.train(
        params,
        lgb.Dataset(
            values,
            label=labels,
            categorical_feature=list(categorical),
            free_raw_data=False,
        ),
        num_boost_round=rounds,
    )


def _matches(booster: lgb.Booster, values: Float64) -> tuple[float, float]:
    """生スコアと確率の、いちばん大きい食い違い。"""
    built = tree.flatten(booster.dump_model())
    theirs_raw = np.asarray(booster.predict(values, raw_score=True), dtype=np.float64)
    theirs = np.asarray(booster.predict(values), dtype=np.float64)
    raw = float(np.abs(tree.raw_score(built, values) - theirs_raw).max())
    probability = float(np.abs(tree.probability(built, values) - theirs).max())
    return raw, probability


def _assert_matches(booster: lgb.Booster, values: Float64, where: str) -> None:
    raw, probability = _matches(booster, values)
    assert raw < TOLERANCE, f"{where}: 生スコアが {raw:.3e} 違う"
    assert probability < TOLERANCE, f"{where}: 確率が {probability:.3e} 違う"


# ── 素直な木 ──────────────────────────────────────────────────
def test_numeric_only() -> None:
    """欠損もカテゴリも無い、いちばん素直な形。"""
    rng = np.random.default_rng(1)
    values = rng.normal(size=(ROWS, 6))
    labels = (values[:, 0] + 0.5 * values[:, 2] + rng.normal(size=ROWS) > 0).astype(np.float64)
    _assert_matches(_train(values, labels), values, "数値だけ")


# ── 欠損の 3 通り ────────────────────────────────────────────
def test_missing_type_nan() -> None:
    """**学習に NaN がある列**（`missing_type = NaN`）。`default_left` に従う。"""
    rng = np.random.default_rng(2)
    values = rng.normal(size=(ROWS, 6))
    values[rng.random((ROWS, 6)) < 0.1] = np.nan
    labels = (np.nan_to_num(values[:, 0]) + rng.normal(size=ROWS) > 0).astype(np.float64)
    booster = _train(values, labels)
    assert _has_missing_type(booster, tree.MISSING_NAN), "NaN の節が出ていない（仕込みを疑う）"
    _assert_matches(booster, values, "欠損あり")


def test_missing_type_none_with_nan_at_predict() -> None:
    """**学習に NaN が無い列**（`missing_type = None`）に、予測で NaN を渡す。

    LightGBM は NaN を **0.0 として**扱う（欠損としてではなく）。ここを取り違えると、
    「学習では出なかったが本番では出る」列で静かに違う枝へ行く。
    """
    rng = np.random.default_rng(3)
    values = rng.normal(size=(ROWS, 4))
    labels = (values[:, 1] + rng.normal(size=ROWS) > 0).astype(np.float64)
    booster = _train(values, labels)
    assert _has_missing_type(booster, "None"), "None の節が出ていない（仕込みを疑う）"
    probe = values.copy()
    probe[rng.random(ROWS) < 0.2, 1] = np.nan
    _assert_matches(booster, probe, "学習に無かった欠損")


def test_missing_type_zero() -> None:
    """`zero_as_missing=true` のときだけ出る `missing_type = Zero`。**0 が欠損になる。**"""
    rng = np.random.default_rng(4)
    values = rng.integers(0, 3, size=(ROWS, 4)).astype(np.float64)
    labels = (values[:, 0] + rng.normal(size=ROWS) > 1).astype(np.float64)
    booster = _train(values, labels, extra={"zero_as_missing": True})
    assert _has_missing_type(booster, tree.MISSING_ZERO), "Zero の節が出ていない（仕込みを疑う）"
    _assert_matches(booster, values, "0 が欠損")


# ── カテゴリ ──────────────────────────────────────────────────
def test_categorical_splits() -> None:
    """カテゴリの節（`==`）。**ビット集合で左右を決める。**"""
    rng = np.random.default_rng(5)
    values = rng.normal(size=(ROWS, 5))
    values[:, 3] = rng.integers(0, 24, size=ROWS)
    labels = (np.isin(values[:, 3], [0, 3, 6, 9]) ^ (values[:, 0] > 0)).astype(np.float64)
    booster = _train(values, labels, categorical=[3])
    assert _has_categorical(booster), "カテゴリの節が出ていない（仕込みを疑う）"
    _assert_matches(booster, values, "カテゴリ")


def test_a_category_beyond_the_bitset() -> None:
    """**学習に無かったカテゴリ**（ビット集合の外）。右へ行く。"""
    rng = np.random.default_rng(6)
    values = rng.normal(size=(ROWS, 5))
    values[:, 3] = rng.integers(0, 8, size=ROWS)
    labels = (np.isin(values[:, 3], [1, 4]) ^ (values[:, 0] > 0)).astype(np.float64)
    booster = _train(values, labels, categorical=[3])
    probe = values.copy()
    probe[rng.random(ROWS) < 0.3, 3] = 9_999.0
    _assert_matches(booster, probe, "未知のカテゴリ")


def test_a_negative_category() -> None:
    """**負の値は右**（LightGBM の `int_fval < 0`）。"""
    rng = np.random.default_rng(7)
    values = rng.normal(size=(ROWS, 5))
    values[:, 3] = rng.integers(0, 8, size=ROWS)
    labels = (np.isin(values[:, 3], [2, 5]) ^ (values[:, 0] > 0)).astype(np.float64)
    booster = _train(values, labels, categorical=[3])
    probe = values.copy()
    probe[rng.random(ROWS) < 0.3, 3] = -1.0
    _assert_matches(booster, probe, "負のカテゴリ")


def test_nan_on_a_categorical_goes_right() -> None:
    """**NaN は右**（`missing_type` によらない）。カテゴリ 0 として扱われるのではない。

    2026-09-10 に実測で確かめた枝。`missing_type = None` で**カテゴリ 0 が左の集合に
    入っている**節に NaN を渡し、右へ行くことを確認してある（§6.5a）。
    """
    rng = np.random.default_rng(8)
    values = rng.integers(0, 4, size=(ROWS, 1)).astype(np.float64)
    labels = np.isin(values[:, 0], [0, 2]).astype(np.float64)
    booster = _train(values, labels, categorical=[0], rounds=1, extra={"num_leaves": 4})
    node = booster.dump_model()["tree_info"][0]["tree_structure"]
    assert 0 in {int(one) for one in str(node["threshold"]).split("||")}, "0 が左に無い（仕込み）"
    assert node["missing_type"] == "None"
    probe = np.array([[math.nan], [0.0], [1.0]])
    _assert_matches(booster, probe, "カテゴリの NaN")
    # **カテゴリ 0 と同じにならない**ことも直に見る
    built = tree.flatten(booster.dump_model())
    scores = tree.raw_score(built, probe)
    assert not math.isclose(scores[0], scores[1]), "NaN がカテゴリ 0 と同じ枝へ行っている"


def test_everything_at_once() -> None:
    """**本番に近い形**（62 列・欠損・カテゴリ・未知の値を全部混ぜる）。"""
    rng = np.random.default_rng(9)
    columns = 62
    values = rng.normal(size=(ROWS, columns))
    for index in (0, 14, 18, 19):
        values[:, index] = rng.integers(0, 20, size=ROWS)
    values[rng.random((ROWS, columns)) < 0.05] = np.nan
    labels = (np.nan_to_num(values[:, 24]) + rng.normal(size=ROWS) > 0).astype(np.float64)
    booster = _train(values, labels, categorical=[0, 14, 18, 19], rounds=60)

    probe = values.copy()
    probe[rng.random(ROWS) < 0.05, 0] = 9_999.0
    probe[rng.random(ROWS) < 0.05, 14] = -3.0
    probe[rng.random(ROWS) < 0.05, 30] = np.nan
    _assert_matches(booster, probe, "全部入り")


# ── 往復と形 ──────────────────────────────────────────────────
def test_json_round_trip() -> None:
    """**成果物に書いて読み直しても同じ値。** NaN と ∞ は JSON に書けないので文字列で持つ。"""
    rng = np.random.default_rng(10)
    values = rng.normal(size=(ROWS, 5))
    values[:, 3] = rng.integers(0, 8, size=ROWS)
    values[rng.random((ROWS, 5)) < 0.05] = np.nan
    labels = (values[:, 0] + rng.normal(size=ROWS) > 0).astype(np.float64)
    booster = _train(values, labels, categorical=[3])

    built = tree.flatten(booster.dump_model())
    restored = tree.from_json(json.loads(json.dumps(tree.to_json(built))))
    assert np.array_equal(tree.raw_score(built, values), tree.raw_score(restored, values))
    assert restored.max_depth == built.max_depth
    assert restored.sigmoid == built.sigmoid


def test_leaves_point_at_themselves() -> None:
    """**葉は自分に戻る節。** これが「深さぶん回すだけ」で歩ける根拠。"""
    rng = np.random.default_rng(11)
    values = rng.normal(size=(2_000, 3))
    booster = _train(values, (values[:, 0] > 0).astype(np.float64), rounds=5)
    built = tree.flatten(booster.dump_model())
    leaves = ~np.isnan(built.value)
    assert bool(np.all(built.left[leaves] == np.nonzero(leaves)[0]))
    assert bool(np.all(built.right[leaves] == np.nonzero(leaves)[0]))


def test_block_size_does_not_change_the_answer() -> None:
    """**行を刻んでも同じ値。** ブロックは memory の都合であって規則ではない。"""
    rng = np.random.default_rng(12)
    values = rng.normal(size=(3_000, 4))
    booster = _train(values, (values[:, 1] > 0).astype(np.float64), rounds=10)
    built = tree.flatten(booster.dump_model())
    assert np.array_equal(
        tree.raw_score(built, values, block=100), tree.raw_score(built, values, block=10_000)
    )


# ── 扱えない形は止める ────────────────────────────────────────
def test_multiclass_is_refused() -> None:
    """**黙って 1 クラス目だけ返さない。**"""
    with pytest.raises(tree.UnsupportedModelError, match="多クラス"):
        tree.flatten({"objective": "multiclass num_class:3", "num_class": 3, "tree_info": [{}]})


def test_another_objective_is_refused() -> None:
    with pytest.raises(tree.UnsupportedModelError, match="目的関数"):
        tree.flatten({"objective": "regression", "num_class": 1, "tree_info": [{}]})


def test_random_forest_is_refused() -> None:
    """`average_output` は葉の平均を取る。**合計とは違う。**"""
    with pytest.raises(tree.UnsupportedModelError, match="average_output"):
        tree.flatten(
            {"objective": "binary", "num_class": 1, "average_output": True, "tree_info": [{}]}
        )


def test_an_empty_forest_is_refused() -> None:
    with pytest.raises(tree.UnsupportedModelError, match="tree_info"):
        tree.flatten({"objective": "binary", "num_class": 1, "tree_info": []})


# ── 仕込みの確認 ──────────────────────────────────────────────
def _nodes(booster: lgb.Booster) -> list[Mapping[str, object]]:
    found: list[Mapping[str, object]] = []

    def walk(node: object) -> None:
        if not isinstance(node, dict) or "leaf_value" in node:
            return
        found.append(node)
        walk(node["left_child"])
        walk(node["right_child"])

    for one in booster.dump_model()["tree_info"]:
        walk(one["tree_structure"])
    return found


def _has_missing_type(booster: lgb.Booster, wanted: str) -> bool:
    return any(one["missing_type"] == wanted for one in _nodes(booster))


def _has_categorical(booster: lgb.Booster) -> bool:
    return any(one["decision_type"] == "==" for one in _nodes(booster))


# ── 規則そのものを直に固定する ────────────────────────────────
#
# 上の突き合わせは「LightGBM が作った木」を通すので、**LightGBM が作らない状況は
# 試せない**。閾値は必ず観測値の中点になるので、`x == threshold` の行が 1 つも出ない
# ——実際、`<=` を `<` に変えても上の 16 件は全部通ってしまう。
#
# だから境界だけは**手で組んだ木**で固定する。
def _one_split(
    threshold: float,
    *,
    categorical: bool = False,
    categories: Sequence[int] = (),
    missing: str = "None",
    default_left: bool = False,
) -> tree.Forest:
    """`x` で 1 回だけ分岐する木。左の葉が `-1`、右の葉が `+1`。"""
    words = (max(categories) // tree.WORD_BITS + 1) if categories else 0
    bitset = [0] * words
    for one in categories:
        bitset[one // tree.WORD_BITS] |= 1 << (one % tree.WORD_BITS)
    return tree.Forest(
        feature=np.array([0, 0, 0], dtype=np.int32),
        threshold=np.array([threshold, math.inf, math.inf], dtype=np.float64),
        left=np.array([1, 1, 2], dtype=np.int32),
        right=np.array([2, 1, 2], dtype=np.int32),
        default_left=np.array([default_left, True, True], dtype=np.bool_),
        is_categorical=np.array([categorical, False, False], dtype=np.bool_),
        missing_nan=np.array([missing == tree.MISSING_NAN, False, False], dtype=np.bool_),
        missing_zero=np.array([missing == tree.MISSING_ZERO, False, False], dtype=np.bool_),
        value=np.array([math.nan, -1.0, 1.0], dtype=np.float64),
        cat_start=np.array([0, 0, 0], dtype=np.int64),
        cat_words=np.array([words, 0, 0], dtype=np.int32),
        bitset=np.array(bitset or [0], dtype=np.int64),
        roots=np.array([0], dtype=np.int32),
        max_depth=2,
        sigmoid=1.0,
    )


def _went(built: tree.Forest, values: Sequence[float]) -> list[str]:
    scores = tree.raw_score(built, np.array(values, dtype=np.float64).reshape(-1, 1))
    return ["左" if one < 0 else "右" for one in scores]


def test_the_threshold_is_inclusive() -> None:
    """**`x <= threshold` なら左**（`<` ではない）。閾値ちょうどの行で決まる。"""
    built = _one_split(2.0)
    assert _went(built, [1.9, 2.0, 2.1]) == ["左", "左", "右"]


def test_a_category_is_truncated_toward_zero() -> None:
    """**`int(x)` は 0 方向に切り捨て**（`2.9` はカテゴリ 2）。"""
    built = _one_split(math.inf, categorical=True, categories=[2])
    assert _went(built, [2.0, 2.9, 3.0]) == ["左", "左", "右"]


def test_zero_is_missing_only_for_missing_type_zero() -> None:
    """**`missing_type = Zero` のときだけ 0 が欠損**になり、`default_left` に従う。"""
    zero_missing = _one_split(-1.0, missing=tree.MISSING_ZERO, default_left=True)
    assert _went(zero_missing, [0.0]) == ["左"], "0 が欠損として扱われていない"
    # 同じ閾値でも `missing_type = None` なら、0 は普通に `0 <= -1` で右
    plain = _one_split(-1.0, missing="None", default_left=True)
    assert _went(plain, [0.0]) == ["右"], "0 を欠損として扱ってしまっている"


def test_the_zero_window_matches_lightgbm() -> None:
    """**0 とみなす幅は `-1e-35 < x <= 1e-35`**（下端は含まない）。"""
    built = _one_split(-1.0, missing=tree.MISSING_ZERO, default_left=True)
    assert _went(built, [1e-35, -1e-35, 1e-34]) == ["左", "右", "右"]


def test_nan_follows_default_left_only_for_missing_type_nan() -> None:
    """`missing_type = NaN` のときだけ、NaN が `default_left` に従う。"""
    as_missing = _one_split(-1.0, missing=tree.MISSING_NAN, default_left=True)
    assert _went(as_missing, [math.nan]) == ["左"]
    # `None` なら NaN は 0.0 になり、`0 <= -1` は偽なので右
    as_zero = _one_split(-1.0, missing="None", default_left=True)
    assert _went(as_zero, [math.nan]) == ["右"]
