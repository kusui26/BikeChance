"""合成器の検査の仕込み（W6 の PR H）。**部品は本物の書き手で作る。**

  * B3：推論の検査と同じ当てはめ（`tests.test_infer.ARTIFACT`。書式 2）
  * 森：`tests.test_model_artifact.ARTIFACT`（小さな本物の LightGBM を平たくしたもの）
  * 門の表：PR F の書き手（`models.gates.build`）で、**その森の SHA-256** を焼き付ける

セルは仕込みのポート（`tests/infer_fixture.STATIONS`）の台数が散るように選んである：
`bike` は a（0 台）・b（1 台）・c（7 台）、`dock` は a（9）・b（8）・c（2）。
"""

from collections.abc import Mapping, Sequence
from typing import Final

from bikechance_ml.baselines import artifact as baseline_artifact
from bikechance_ml.eval import gates as judging
from bikechance_ml.models import artifact as lightgbm_artifact
from bikechance_ml.models import composite
from bikechance_ml.models import gates as gate_tables
from bikechance_ml.models.gates import CellKey
from tests import test_infer, test_model_artifact

FOREST: Final[lightgbm_artifact.LightGbmArtifact] = test_model_artifact.ARTIFACT
FOREST_BODY: Final[bytes] = lightgbm_artifact.to_bytes(FOREST)

#: LightGBM に回すセル。**両系統・両ターゲット・いろいろなバケツ**を混ぜる
LIGHTGBM_CELLS: Final[tuple[CellKey, ...]] = (
    ("docomo-cycle", "bike", 45, "1"),
    ("hellocycling", "bike", 30, "0"),
    ("hellocycling", "bike", 60, "6-10"),
    ("hellocycling", "dock", 15, "2"),
    ("hellocycling", "dock", 45, "6-10"),
)

#: B3 に残すセル（届かない・判定の対象外）。**表に載っていても B3**
B3_CELLS: Final[tuple[tuple[CellKey, str], ...]] = (
    (("hellocycling", "bike", 15, "0"), judging.BELOW),
    (("hellocycling", "dock", 30, "6-10"), judging.EXCLUDED),
)

EVALUATE_DAYS: Final[tuple[str, ...]] = ("2026-09-10", "2026-09-11")
CREATED_AT: Final[str] = "2026-10-04T01:00:00+00:00"
VERSION: Final[str] = "composite-v1-test-20260908"


def b3() -> baseline_artifact.Artifact:
    """B3 の部品。**呼んだときに取りに行く**——推論の検査もこの仕込みを読むので、読み込みの
    途中で互いの中身に触らない（`tests.test_infer` との循環を断つ）。"""
    return test_infer.ARTIFACT


def b3_body() -> bytes:
    return baseline_artifact.to_bytes(b3())


def gate_cell(key: CellKey, reason: str) -> gate_tables.GateCell:
    """1 つのセル。**行き先は理由から決まる**（`passed` だけが LightGBM）。"""
    system, target, h_min, bucket = key
    passed = reason == gate_tables.PASSED
    return gate_tables.GateCell(
        system=system,
        target=target,
        h_min=h_min,
        bucket=bucket,
        route=gate_tables.TO_LIGHTGBM if passed else gate_tables.TO_B3,
        reason=reason,
        n=120,
        b0=0.2,
        b3=0.1,
        lgbm=0.08 if passed else 0.11,
        improvement=0.2 if passed else -0.1,
    )


def gates_body(
    forest_body: bytes = FOREST_BODY,
    model_version: str = FOREST.model_version,
    lightgbm_cells: Sequence[CellKey] = LIGHTGBM_CELLS,
) -> bytes:
    """門の表のバイト列（PR F の書き手。**渡した森の SHA-256** を焼き付ける）。"""
    cells = [gate_cell(one, gate_tables.PASSED) for one in lightgbm_cells]
    cells += [gate_cell(key, reason) for key, reason in B3_CELLS]
    table = gate_tables.build(
        model_version=model_version,
        artifact_sha256=composite.sha256_of(forest_body),
        cells=cells,
        evaluate_days=EVALUATE_DAYS,
        rule=gate_tables.rule(),
    )
    return gate_tables.to_bytes(table)


def parts(**replaced: bytes) -> dict[str, bytes]:
    """3 つの部品。**名前を渡したものだけ差し替える**（壊した部品の検査に使う）。"""
    made: Mapping[str, bytes] = {
        composite.B3_PART: b3_body(),
        composite.LIGHTGBM_PART: FOREST_BODY,
        composite.GATES_PART: gates_body(),
    }
    return {**made, **replaced}


def build(dropped: Sequence[CellKey] = (), version: str = VERSION) -> composite.CompositeArtifact:
    """組んだ合成器（削ったセルを渡せる）。"""
    return composite.assemble(
        parts(), model_version=version, created_at=CREATED_AT, dropped=dropped
    )
