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
import pyarrow as pa

from bikechance_ml.features.arrays import Features
from bikechance_ml.models import artifact as lightgbm_artifact
from bikechance_ml.models import composite, matrix
from tests import composite_fixture, test_composite, test_infer
from tests.test_model_artifact import ARTIFACT, N_COLUMNS

#: 配信のランタイムに無いもの。**部分一致ではなく先頭のパッケージ名で塞ぐ。**
BLOCKED: Final[tuple[str, ...]] = ("lightgbm", "scipy")

#: 子プロセスの前置き。**配信のランタイムを真似て塞ぎ**、FastAPI のアプリを組み立てる。
_PRELUDE: Final[str] = '''
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
'''

#: 子プロセスで動かす本体。**親と同じ乱数の種**で行列を作り、同じ確率が出るかを見る。
_SCRIPT: Final[str] = (
    _PRELUDE
    + """
from bikechance_ml.models import artifact, matrix

restored = artifact.from_bytes(open(sys.argv[1], "rb").read())
values = np.load(sys.argv[2])
built = matrix.Matrix(values=values, columns=matrix.MODEL_COLUMNS)
probability = artifact.predict(restored.forests["bike"], built)
print(json.dumps({{"loaded": sorted(one for one in sys.modules if one in BLOCKED),
                   "probability": probability.tolist()}}))
"""
)


def _run(body: bytes, values: Features, tmp_path: Path) -> dict[str, object]:
    """`lightgbm` と `scipy` を塞いだ子プロセスで、成果物を読んで予測させる。"""
    artifact_file = tmp_path / "artifact.json.gz"
    artifact_file.write_bytes(body)
    values_file = tmp_path / "values.npy"
    np.save(values_file, values)
    return _run_script(_SCRIPT, [str(artifact_file), str(values_file)])


#: 合成器（W6 の PR H）。**3 つの部品を開き、本物の配信の表で予測する**まで塞いだまま通す。
_COMPOSITE_SCRIPT: Final[str] = (
    _PRELUDE
    + """
from datetime import datetime

import pyarrow as pa

from bikechance_ml.models import composite

made = composite.to_predictor(composite.from_bytes(open(sys.argv[1], "rb").read()))
with pa.OSFile(sys.argv[2], "rb") as source:
    table = pa.ipc.open_file(source).read_all()
outcome = made.predict(sys.argv[4], datetime.fromisoformat(sys.argv[3]), table)
probability = {{name: values.tolist() for name, values in outcome.probability.items()}}
print(json.dumps({{"loaded": sorted(one for one in sys.modules if one in BLOCKED),
                   "probability": probability}}))
"""
)


def _run_script(script: str, arguments: list[str]) -> dict[str, object]:
    """塞いだ子プロセスで動かし、出力の JSON を返す。**落ちたら標準エラーを見せる。**"""
    done = subprocess.run(
        [sys.executable, "-c", script.format(blocked=repr(set(BLOCKED))), *arguments],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, f"配信の経路が落ちました:\n{done.stderr}"
    parsed = json.loads(done.stdout)
    assert isinstance(parsed, dict)
    return parsed


def test_a_composite_serves_without_lightgbm(tmp_path: Path) -> None:
    """**合成器も、`lightgbm` も `scipy` も無い場所で、読み込みから予測まで通る**（W6 の PR H）。

    門の表と B3 の部品を読む道（`models/gates`・`eval/gates`・`baselines`）が、配信の
    ランタイムに無いものを読み込まないことを見る。確率は親とビット単位で同じ。
    """
    system_id = "hellocycling"
    table = test_composite.serving_table(system_id)
    made = composite_fixture.build()
    artifact_file = tmp_path / "composite.json.gz"
    artifact_file.write_bytes(composite.to_bytes(made))
    table_file = tmp_path / "table.arrow"
    with pa.OSFile(str(table_file), "wb") as sink, pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    at = test_infer.AT.isoformat()
    result = _run_script(_COMPOSITE_SCRIPT, [str(artifact_file), str(table_file), at, system_id])
    assert result["loaded"] == [], "配信の経路が lightgbm / scipy を読み込みました"
    here = composite.to_predictor(made).predict(system_id, test_infer.AT, table)
    probability = result["probability"]
    assert isinstance(probability, dict)
    for name, values in here.probability.items():
        assert np.array_equal(np.asarray(probability[name], dtype=np.float64), values)


def test_serving_works_without_lightgbm(tmp_path: Path) -> None:
    """**`lightgbm` も `scipy` も無い場所で、読み込みから予測まで通る。**"""
    rng = np.random.default_rng(20260911)
    values = rng.normal(size=(64, N_COLUMNS))
    for index in matrix.categorical_indices():
        values[:, index] = rng.integers(0, 3, size=64)
    values[rng.random((64, N_COLUMNS)) < 0.1] = np.nan
    # **本番と同じ型で渡す**（`matrix.build` は float32 を作る。§12 の 168）
    features = np.asarray(values, dtype=matrix.DTYPE)

    result = _run(lightgbm_artifact.to_bytes(ARTIFACT), features, tmp_path)

    assert result["loaded"] == [], "配信の経路が lightgbm / scipy を読み込みました"
    built = matrix.Matrix(values=features, columns=matrix.MODEL_COLUMNS)
    here = lightgbm_artifact.predict(ARTIFACT.forests["bike"], built)
    assert np.array_equal(np.asarray(result["probability"], dtype=np.float64), here)
