"""numpy の配列の型別名。

**要素型を必ず書く。** `np.ndarray` を素で書くと、numpy 側の既定の型引数が `Any` を
含むため `mypy --strict`（`disallow_any_explicit`。CLAUDE.md §3 の「any 禁止」）を
通らない。それ以上に、`int16` の合計を `int16` のまま取って桁あふれさせるような事故を
**型で捕まえられる**のが大きい。

別名にしておくのは、`npt.NDArray[np.int16]` が並ぶと式より注釈のほうが長くなるため。
"""

from typing import TypeVar

import numpy as np
import numpy.typing as npt

type Bools = npt.NDArray[np.bool_]
type Int8 = npt.NDArray[np.int8]
type Int16 = npt.NDArray[np.int16]
type Int32 = npt.NDArray[np.int32]
type Int64 = npt.NDArray[np.int64]
type UInt64 = npt.NDArray[np.uint64]
type Float32 = npt.NDArray[np.float32]
type Float64 = npt.NDArray[np.float64]

type Strings = npt.NDArray[np.str_]

#: 配列の区間。素の `slice` は型引数が `Any` なので、ここで具体化しておく。
type Span = slice[int, int, int]

#: 要素型を保ったまま配列を受け渡す関数のための型変数。**値で制限する**
#: （`np.generic` を上限にすると、その型引数の `Any` が漏れてくる）。
ScalarT = TypeVar(
    "ScalarT", np.bool_, np.int8, np.int16, np.int32, np.int64, np.float32, np.float64
)
