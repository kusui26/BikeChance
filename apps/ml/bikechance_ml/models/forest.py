"""LightGBM の木を numpy で歩く（W4 プラン §6.5a、PR E′）。

**`lightgbm` を import しない。** 配信のランタイムに OpenMP（`libgomp.so.1`）が無く、
`import lightgbm` が `OSError` で落ちるため（§12 の 126）。木の構造だけを成果物に
持ち、予測は numpy で行う。

**同じ値が出ることは「宣言」ではなく「検査」で担保する。** 当てはめた直後に
`Booster.predict` と突き合わせ、**1 つでも違えば成果物を書かない**
（`jobs/fit_lightgbm.py` の `refuse_if_different`）。加えてゴールデン
（`tests/test_model_forest.py`）が、欠損・カテゴリ・未知の値まで含めて固定する。

## 木の歩き方（LightGBM の `tree.h` に合わせてある。すべて実測で確かめた）

**数値の節**（`decision_type` が `<=`）

  1. `missing_type` が `NaN` でなければ、**NaN は 0.0 として扱う**
  2. 「欠損」に当たるのは `missing_type = NaN` かつ NaN、または `missing_type = Zero`
     かつ 0（厳密には `-1e-35 < x <= 1e-35`）。そのときは `default_left` に従う
  3. それ以外は `x <= threshold` なら左

**カテゴリの節**（`decision_type` が `==`）

  1. **NaN は右**（`missing_type` によらない。2026-09-10 に実測。`missing_type = None` で
     カテゴリ 0 が左の集合に入っている節に NaN を渡しても、右へ行った）
  2. 負の値も右
  3. それ以外は `int(x)` がビット集合に入っていれば左

**葉は自分に戻る節にしてある。** 「葉に着いたか」を節の中身で判定せずに済む。

## 速さのために測って決めたこと（2026-09-10、300 木 × 127 葉の実物で実測）

素直に「全部の行 × 全部の木」を `max_depth` 回まわすと、**HELLO の 1 サイクル
（148,610 行 × 2 ターゲット）に 95 秒**かかった。手元の Mac の値で、**クラウドの vCPU は
1.5〜2 倍遅い**と見込むと 143〜190 秒——`maxDuration`（120 秒）を超える。
**測ってから 3 つ削り、21.8 秒にした。**

| 実測 | 削ったこと |
|---|---|
| 深さ 12 で **86.7%** が葉に着いているのに 26 回とも全幅で計算 | **着いたものを毎回外す** |
| 分岐 37,800 のうち **カテゴリは 11,950（32%）** | **カテゴリの節にだけ**ビット集合を引く |
| `missing_type = Zero` の節が **0 件** | その節が**無ければ 0 の判定をしない** |

（`missing_type = Zero` は `zero_as_missing` を立てたときしか出ない。**規則は消していない**——
節が 1 つでもあれば、いままでどおり `default_left` へ送る。）

**どれも「する計算を減らす」だけで、規則は 1 つも変えていない。** 一致は当てはめ時の
照合とゴールデンが見張っている。
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

import numpy as np

from bikechance_ml.features.arrays import Bools, Float64, Int32, Int64

#: ビット集合の 1 語のビット数（LightGBM の `cat_threshold` と同じ）。
WORD_BITS: Final[int] = 32

#: `WORD_BITS` で割る代わりの右シフト幅。
WORD_SHIFT: Final[int] = 5

#: LightGBM が「0 とみなす」幅（`kZeroThreshold`）。`missing_type = Zero` のときだけ効く。
ZERO_THRESHOLD: Final[float] = 1e-35

#: `missing_type` の値。LightGBM の dump がこの 3 つを出す。
MISSING_NAN: Final[str] = "NaN"
MISSING_ZERO: Final[str] = "Zero"

#: 1 度に歩かせる行数。**大きくすると遅くなる**（2026-09-10、300 木の実物で実測）。
#:
#: 「行 × 木」の位置と、そこから引く特徴量の値が **CPU のキャッシュに乗るかどうか**で
#: 決まる。148,610 行（HELLO の 1 サイクル）を 1 ターゲットぶん歩かせた最小値：
#:
#:     1,024 → 14.1 秒 / 4,096 → 14.2 秒 / 16,384 → 17.2 秒 / 131,072 → 94.5 秒
#:
#: **4,096 までは平ら**なので、その中で作業領域が小さいほうを採る
#: （4,096 行 × 300 木 × 4 バイト ＝ 約 5 MB）。**測り値は機械のぶれが 2 倍あるので、
#: 4 周まわした最小で比べている。**
BLOCK_ROWS: Final[int] = 4_096


class UnsupportedModelError(ValueError):
    """この歩き方では扱えない木。**黙って別の答えを出さない。**"""


@dataclass(frozen=True)
class Forest:
    """平たくした森。**節は 1 本の配列に並び、木の根は `roots` が指す。**

    葉も節として並んでいる（`left == right == 自分`、`threshold = +inf`）ので、
    歩くときに「葉に着いたか」を見なくてよい。
    """

    #: 節ごと。葉は `feature = 0`
    feature: Int32
    threshold: Float64
    left: Int32
    right: Int32
    default_left: Bools
    is_categorical: Bools
    missing_nan: Bools
    missing_zero: Bools
    #: 葉の値。節では NaN
    value: Float64
    #: カテゴリの節が使うビット集合の位置と長さ
    cat_start: Int64
    cat_words: Int32
    bitset: Int64
    #: 木の根の節番号
    roots: Int32
    #: いちばん深い木の深さ（歩く回数）
    max_depth: int
    #: `binary` の sigmoid の係数。生スコアから確率にするときに使う
    sigmoid: float

    #: **導出。** NaN が左へ行くか（節ごとに決まっていて、データに依らない）
    nan_left: Bools = field(init=False)
    #: **導出。** `missing_type = Zero` の節が 1 つでもあるか
    any_zero_missing: bool = field(init=False)

    def __post_init__(self) -> None:
        """**成果物に書かないものを、読んだあとに組み立てる。**

        どちらも他の欄から一意に決まる。書いてしまうと**正が 2 つ**になり、
        食い違ったときにどちらが本当か分からなくなる（W3 プラン §12 の 106 と同じ形）。
        """
        object.__setattr__(self, "nan_left", _nan_left(self))
        object.__setattr__(self, "any_zero_missing", bool(self.missing_zero.any()))

    def __len__(self) -> int:
        return int(self.roots.size)

    @property
    def n_nodes(self) -> int:
        return int(self.feature.size)


def raw_score(forest: Forest, values: Float64, block: int = BLOCK_ROWS) -> Float64:
    """生スコア（葉の値の合計）。**`Booster.predict(raw_score=True)` と同じ値。**"""
    total = np.zeros(len(values), dtype=np.float64)
    for start in range(0, len(values), block):
        chunk = values[start : start + block]
        total[start : start + block] = _score_block(forest, chunk)
    return total


def probability(forest: Forest, values: Float64, block: int = BLOCK_ROWS) -> Float64:
    """確率。**`binary` の sigmoid を掛ける**（`Booster.predict` と同じ値）。"""
    return 1.0 / (1.0 + np.exp(-forest.sigmoid * raw_score(forest, values, block)))


def _score_block(forest: Forest, chunk: Float64) -> Float64:
    """1 ブロックぶん。**全部の木を同時に歩き、葉に着いたものは毎回外す。**

    `node` は「行 × 木」を平たく並べた 1 本の配列で、`walking` がまだ歩いている位置を
    指す。深さが増えるほど `walking` は短くなるので、**深いところの計算は少数の行の
    ぶんしか払わない**（実測：深さ 12 で 86.7% が葉に着いている）。
    """
    rows, trees = len(chunk), len(forest)
    where_row = np.repeat(np.arange(rows, dtype=np.int32), trees)
    node = np.tile(forest.roots, rows)
    walking = np.flatnonzero(forest.left[node] != node)
    for _ in range(forest.max_depth):
        if not walking.size:
            break
        walking = _step(forest, chunk, where_row, node, walking)
    _refuse_a_walk_that_does_not_end(walking)
    return np.asarray(forest.value[node].reshape(rows, trees).sum(axis=1), dtype=np.float64)


def _step(forest: Forest, chunk: Float64, where_row: Int32, node: Int32, walking: Int64) -> Int64:
    """歩いている位置を 1 段進め、**まだ葉に着いていない位置**を返す。`node` を書き換える。"""
    here = node[walking]
    picked = chunk[where_row[walking], forest.feature[here]]
    stepped = np.where(_goes_left(forest, here, picked), forest.left[here], forest.right[here])
    node[walking] = stepped
    return np.asarray(walking[forest.left[stepped] != stepped], dtype=np.int64)


def _refuse_a_walk_that_does_not_end(walking: Int64) -> None:
    """**`max_depth` 回で葉に着かなかったら止める。**

    成果物の `max_depth` は読んだ値なので、嘘をついていれば途中の節の値を葉として
    足してしまう。**例外は出ず、確率だけが静かに変わる**——止める。
    """
    if walking.size:
        raise UnsupportedModelError(f"max_depth 回で葉に着かない位置が {walking.size} あります")


def _goes_left(forest: Forest, node: Int32, picked: Float64) -> Bools:
    """左へ行くか。**数値で全部決めてから、カテゴリの節だけ上書きする。**

    カテゴリの分岐は全体の 3 割（実測 11,950 / 37,800）なので、**残り 7 割に
    ビット集合を引かせない**。数値の規則をカテゴリの節に当てた結果は必ず捨てられる。
    """
    goes = _numeric_left(forest, node, picked)
    categorical = np.flatnonzero(forest.is_categorical[node])
    if categorical.size:
        goes[categorical] = _categorical_left(forest, node[categorical], picked[categorical])
    return goes


def _numeric_left(forest: Forest, node: Int32, picked: Float64) -> Bools:
    """数値の節。**NaN の行き先は節ごとに決まっている**（`nan_left`）。

    NaN との比較は必ず偽なので、`picked <= threshold` は NaN で右に行く。それを
    `nan_left` で上書きする——**欠損を 0 に置き換えた配列を作らなくて済む**。
    """
    goes = np.where(np.isnan(picked), forest.nan_left[node], picked <= forest.threshold[node])
    if forest.any_zero_missing:
        goes = _zero_is_missing(forest, node, picked, goes)
    return np.asarray(goes, dtype=np.bool_)


def _zero_is_missing(forest: Forest, node: Int32, picked: Float64, goes: Bools) -> Bools:
    """`missing_type = Zero` の節だけ、**0 を欠損として `default_left` へ送る**。

    LightGBM の既定では出ない形（`zero_as_missing` を立てたときだけ）なので、
    **その節が 1 つも無ければ呼ばない**。規則そのものは消していない。
    """
    zero = (picked > -ZERO_THRESHOLD) & (picked <= ZERO_THRESHOLD)
    return np.asarray(
        np.where(forest.missing_zero[node] & zero, forest.default_left[node], goes),
        dtype=np.bool_,
    )


def _categorical_left(forest: Forest, node: Int32, picked: Float64) -> Bools:
    """カテゴリの節。**NaN と負は右**、それ以外はビット集合を引く。"""
    code = np.where(np.isnan(picked) | (picked < 0), -1, picked).astype(np.int64)
    word = code >> WORD_SHIFT
    inside = (code >= 0) & (word < forest.cat_words[node])
    index = np.where(inside, forest.cat_start[node] + np.where(inside, word, 0), 0)
    bit = (forest.bitset[index] >> (code & (WORD_BITS - 1))) & 1
    return np.asarray(inside & (bit == 1), dtype=np.bool_)


def _nan_left(forest: Forest) -> Bools:
    """NaN が左へ行くか。**節ごとに決まっていて、渡す値に依らない。**

    * `missing_type = NaN` → NaN が欠損なので `default_left`
    * `missing_type = Zero` → NaN は 0 として扱われ、その 0 が欠損なので `default_left`
    * `missing_type = None` → NaN は 0 として扱われ、0 は欠損でないので `0 <= threshold`
    * **カテゴリの節** → 右（`_categorical_left` が上書きするので、この値は使われない）
    """
    missing = forest.missing_nan | forest.missing_zero
    plain = np.zeros_like(forest.threshold) <= forest.threshold
    return np.asarray(
        ~forest.is_categorical & np.where(missing, forest.default_left, plain), dtype=np.bool_
    )


# ── LightGBM の dump を平たくする ─────────────────────────────
class _Builder:
    """`dump_model()` の入れ子を 1 本の配列に落とす。**葉も節として並べる。**"""

    def __init__(self) -> None:
        self.feature: list[int] = []
        self.threshold: list[float] = []
        self.left: list[int] = []
        self.right: list[int] = []
        self.default_left: list[bool] = []
        self.is_categorical: list[bool] = []
        self.missing: list[str] = []
        self.value: list[float] = []
        self.cat_start: list[int] = []
        self.cat_words: list[int] = []
        self.bitset: list[int] = []
        self.roots: list[int] = []

    def add_tree(self, structure: Mapping[str, object]) -> None:
        self.roots.append(len(self.feature))
        self._walk(structure)

    def _walk(self, node: Mapping[str, object]) -> int:
        index = len(self.feature)
        if "leaf_value" in node:
            self._add_leaf(float(str(node["leaf_value"])), index)
            return index
        self._add_split(node, index)
        self.left[index] = self._walk(_child(node, "left_child"))
        self.right[index] = self._walk(_child(node, "right_child"))
        return index

    def _add_leaf(self, leaf_value: float, index: int) -> None:
        self.feature.append(0)
        self.threshold.append(math.inf)
        self.left.append(index)
        self.right.append(index)
        self.default_left.append(True)
        self.is_categorical.append(False)
        self.missing.append("None")
        self.value.append(leaf_value)
        self.cat_start.append(len(self.bitset))
        self.cat_words.append(0)

    def _add_split(self, node: Mapping[str, object], index: int) -> None:
        categorical = str(node["decision_type"]) == "=="
        self.feature.append(int(str(node["split_feature"])))
        self.threshold.append(math.inf if categorical else float(str(node["threshold"])))
        self.left.append(index)
        self.right.append(index)
        self.default_left.append(bool(node["default_left"]))
        self.is_categorical.append(categorical)
        self.missing.append(str(node["missing_type"]))
        self.value.append(math.nan)
        self.cat_start.append(len(self.bitset))
        self.cat_words.append(self._add_bitset(node) if categorical else 0)

    def _add_bitset(self, node: Mapping[str, object]) -> int:
        """左へ行くカテゴリをビット集合にする（`"0||3||6"` の形で入っている）。"""
        categories = [int(one) for one in str(node["threshold"]).split("||")]
        if any(one < 0 for one in categories):
            raise UnsupportedModelError(f"負のカテゴリ: {categories}")
        words = max(categories) // WORD_BITS + 1
        block = [0] * words
        for one in categories:
            block[one // WORD_BITS] |= 1 << (one % WORD_BITS)
        self.bitset.extend(block)
        return words


def _child(node: Mapping[str, object], name: str) -> Mapping[str, object]:
    found = node[name]
    if not isinstance(found, dict):
        raise UnsupportedModelError(f"{name} が入れ子になっていない")
    return found


def depth_of(structure: Mapping[str, object]) -> int:
    """木の深さ（葉まで何回進むか）。"""
    if "leaf_value" in structure:
        return 1
    return 1 + max(
        depth_of(_child(structure, "left_child")), depth_of(_child(structure, "right_child"))
    )


def flatten(dump: Mapping[str, object]) -> Forest:
    """`Booster.dump_model()` を `Forest` にする。**扱えない形は例外にする。**"""
    _refuse_unsupported(dump)
    builder = _Builder()
    trees = _trees(dump)
    for tree in trees:
        builder.add_tree(_structure(tree))
    return _to_forest(builder, max(depth_of(_structure(one)) for one in trees), _sigmoid(dump))


def _to_forest(builder: _Builder, max_depth: int, sigmoid: float) -> Forest:
    return Forest(
        feature=np.array(builder.feature, dtype=np.int32),
        threshold=np.array(builder.threshold, dtype=np.float64),
        left=np.array(builder.left, dtype=np.int32),
        right=np.array(builder.right, dtype=np.int32),
        default_left=np.array(builder.default_left, dtype=np.bool_),
        is_categorical=np.array(builder.is_categorical, dtype=np.bool_),
        missing_nan=np.array([one == MISSING_NAN for one in builder.missing], dtype=np.bool_),
        missing_zero=np.array([one == MISSING_ZERO for one in builder.missing], dtype=np.bool_),
        value=np.array(builder.value, dtype=np.float64),
        cat_start=np.array(builder.cat_start, dtype=np.int64),
        cat_words=np.array(builder.cat_words, dtype=np.int32),
        bitset=np.array(builder.bitset or [0], dtype=np.int64),
        roots=np.array(builder.roots, dtype=np.int32),
        max_depth=max_depth,
        sigmoid=sigmoid,
    )


def _refuse_unsupported(dump: Mapping[str, object]) -> None:
    """**扱える形かどうかを先に確かめる。** 黙って別の答えを出さない。"""
    if bool(dump.get("average_output", False)):
        raise UnsupportedModelError("average_output（ランダムフォレスト）は扱えません")
    if int(str(dump.get("num_class", 1))) != 1:
        raise UnsupportedModelError(f"多クラス（num_class={dump.get('num_class')}）は扱えません")
    objective = str(dump.get("objective", ""))
    if not objective.startswith("binary"):
        raise UnsupportedModelError(f"目的関数 {objective!r} は扱えません（binary だけ）")


def _sigmoid(dump: Mapping[str, object]) -> float:
    """`"binary sigmoid:1"` から係数を取る。**書いていなければ 1.0。**"""
    for part in str(dump.get("objective", "")).split():
        if part.startswith("sigmoid:"):
            return float(part.removeprefix("sigmoid:"))
    return 1.0


def _trees(dump: Mapping[str, object]) -> Sequence[Mapping[str, object]]:
    found = dump.get("tree_info")
    if not isinstance(found, list) or not found:
        raise UnsupportedModelError("tree_info が空です")
    return [one for one in found if isinstance(one, dict)]


def _structure(tree: Mapping[str, object]) -> Mapping[str, object]:
    found = tree.get("tree_structure")
    if not isinstance(found, dict):
        raise UnsupportedModelError("tree_structure が無い木があります")
    return found


# ── 成果物との往復 ────────────────────────────────────────────
def to_json(forest: Forest) -> dict[str, object]:
    """成果物に書く形。**平たい配列のまま**（読むときに木へ戻さない）。"""
    return {
        "feature": [int(one) for one in forest.feature],
        "threshold": [_json_number(one) for one in forest.threshold],
        "left": [int(one) for one in forest.left],
        "right": [int(one) for one in forest.right],
        "default_left": [bool(one) for one in forest.default_left],
        "is_categorical": [bool(one) for one in forest.is_categorical],
        "missing_nan": [bool(one) for one in forest.missing_nan],
        "missing_zero": [bool(one) for one in forest.missing_zero],
        "value": [_json_number(one) for one in forest.value],
        "cat_start": [int(one) for one in forest.cat_start],
        "cat_words": [int(one) for one in forest.cat_words],
        "bitset": [int(one) for one in forest.bitset],
        "roots": [int(one) for one in forest.roots],
        "max_depth": forest.max_depth,
        "sigmoid": forest.sigmoid,
    }


def from_json(document: Mapping[str, object]) -> Forest:
    """成果物から読む。**JSON の `null` は NaN / 無限大に戻す。**"""
    return Forest(
        feature=_int32(document, "feature"),
        threshold=_floats(document, "threshold"),
        left=_int32(document, "left"),
        right=_int32(document, "right"),
        default_left=_bools(document, "default_left"),
        is_categorical=_bools(document, "is_categorical"),
        missing_nan=_bools(document, "missing_nan"),
        missing_zero=_bools(document, "missing_zero"),
        value=_floats(document, "value"),
        cat_start=_int64(document, "cat_start"),
        cat_words=_int32(document, "cat_words"),
        bitset=_int64(document, "bitset"),
        roots=_int32(document, "roots"),
        max_depth=int(str(document["max_depth"])),
        sigmoid=float(str(document["sigmoid"])),
    )


#: JSON は NaN も Infinity も書けない。**この 2 つを文字列で表す。**
_NAN: Final[str] = "nan"
_INFINITY: Final[str] = "inf"


def _json_number(value: float) -> float | str:
    if math.isnan(value):
        return _NAN
    if math.isinf(value):
        return _INFINITY
    return float(value)


def _floats(document: Mapping[str, object], name: str) -> Float64:
    values = document[name]
    if not isinstance(values, list):
        raise UnsupportedModelError(f"{name} が配列ではありません")
    return np.array([_number(one) for one in values], dtype=np.float64)


def _number(value: object) -> float:
    if value == _NAN:
        return math.nan
    if value == _INFINITY:
        return math.inf
    return float(str(value))


def _int32(document: Mapping[str, object], name: str) -> Int32:
    return np.array(_integers(document, name), dtype=np.int32)


def _int64(document: Mapping[str, object], name: str) -> Int64:
    return np.array(_integers(document, name), dtype=np.int64)


def _integers(document: Mapping[str, object], name: str) -> list[int]:
    values = document[name]
    if not isinstance(values, list):
        raise UnsupportedModelError(f"{name} が配列ではありません")
    return [int(str(one)) for one in values]


def _bools(document: Mapping[str, object], name: str) -> Bools:
    values = document[name]
    if not isinstance(values, list):
        raise UnsupportedModelError(f"{name} が配列ではありません")
    return np.array([bool(one) for one in values], dtype=np.bool_)
