"""配信の経路が `lightgbm` を読み込まないこと（W4 プラン §6.5a、§12 の 126）。

**これが PR E′ の全部である。** Vercel の Python ランタイム（Amazon Linux）には
OpenMP（`libgomp.so.1`）が無く、`lightgbm` のホイールはそれを同梱していない。
`import lightgbm` は **`OSError` で落ちる**。落ちるのは読み込み（`dlopen`）の時点なので、
**ビルドもバンドルサイズの検査も通ってしまう**。2026-09-10 に本番で起きた。

だから「入れないつもり」ではなく、**入っていないことを機械で確かめる**。ここでは
`lightgbm` と `scipy` を import できない状態を作り、その中で

  1. FastAPI のアプリ（`main.py`）を組み立て、
  2. 成果物を読み、
  3. 木を歩いて確率を出す

まで通ることを見る。どこかに `import lightgbm` が紛れ込めば、ここで落ちる。
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Final

import numpy as np

from bikechance_ml.features.arrays import Float64
from bikechance_ml.models import artifact as lightgbm_artifact
from bikechance_ml.models import matrix
from tests.test_model_artifact import ARTIFACT, N_COLUMNS

#: 配信のランタイムに無いもの。**部分一致ではなく先頭のパッケージ名で塞ぐ。**
BLOCKED: Final[tuple[str, ...]] = ("lightgbm", "scipy")

#: 子プロセスで動かす本体。**親と同じ乱数の種**で行列を作り、同じ確率が出るかを見る。
_SCRIPT: Final[str] = '''
import json, sys

BLOCKED = {blocked}


class Refuse:
    """配信のランタイムを真似る。**あるはずのないものを import したら落とす。**"""

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError(f"{{name}} は配信のランタイムには無い（テストが塞いだ）")
        return None


sys.meta_path.insert(0, Refuse())

import numpy as np

import main  # noqa: F401  ── FastAPI のアプリを組み立てる（配信の入口）
from bikechance_ml.models import artifact, matrix

restored = artifact.from_bytes(open(sys.argv[1], "rb").read())
values = np.load(sys.argv[2])
built = matrix.Matrix(values=values, columns=matrix.MODEL_COLUMNS)
probability = artifact.predict(restored.forests["bike"], built)
print(json.dumps({{"loaded": sorted(one for one in sys.modules if one in BLOCKED),
                   "probability": probability.tolist()}}))
'''


def _run(body: bytes, values: Float64, tmp_path: Path) -> dict[str, object]:
    """`lightgbm` と `scipy` を塞いだ子プロセスで、成果物を読んで予測させる。"""
    artifact_file = tmp_path / "artifact.json.gz"
    artifact_file.write_bytes(body)
    values_file = tmp_path / "values.npy"
    np.save(values_file, values)
    script = _SCRIPT.format(blocked=repr(set(BLOCKED)))
    done = subprocess.run(
        [sys.executable, "-c", script, str(artifact_file), str(values_file)],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, f"配信の経路が落ちました:\n{done.stderr}"
    parsed = json.loads(done.stdout)
    assert isinstance(parsed, dict)
    return parsed


def test_serving_works_without_lightgbm(tmp_path: Path) -> None:
    """**`lightgbm` も `scipy` も無い場所で、読み込みから予測まで通る。**"""
    rng = np.random.default_rng(20260911)
    values = rng.normal(size=(64, N_COLUMNS))
    for index in matrix.categorical_indices():
        values[:, index] = rng.integers(0, 3, size=64)
    values[rng.random((64, N_COLUMNS)) < 0.1] = np.nan

    result = _run(lightgbm_artifact.to_bytes(ARTIFACT), values, tmp_path)

    assert result["loaded"] == [], "配信の経路が lightgbm / scipy を読み込みました"
    built = matrix.Matrix(values=values, columns=matrix.MODEL_COLUMNS)
    here = lightgbm_artifact.predict(ARTIFACT.forests["bike"], built)
    assert np.array_equal(np.asarray(result["probability"], dtype=np.float64), here)
