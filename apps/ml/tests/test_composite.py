"""合成器（`models/composite.py`、W6 の PR H、D-33、契約 33・35）。

**この 1 ファイルの主題は 2 つ。**

  * **セルごとに部品とビット一致**（完了条件 1）：森を歩いた行は LightGBM 単体と、残りの行は
    B3 単体と、確率のバイト列まで同じ
  * **組の照合**：部品の SHA-256・門の表と森の組・書式 2 の B3・削ったセル（足せない）が
    1 つでも崩れたら読まない

配信の表は、推論の検査と同じ仕込み（`tests/test_infer.py` の代役）から本物の `build_now` で作る。
"""

import base64
import gzip
import json
from collections.abc import Callable
from pathlib import Path
from typing import Final

import numpy as np
import pyarrow as pa
import pytest

from bikechance_ml.eval.gates import UnknownHorizonError
from bikechance_ml.features import profile
from bikechance_ml.features.grid import jst_date
from bikechance_ml.jobs import infer
from bikechance_ml.models import artifact as lightgbm_artifact
from bikechance_ml.models import composite, matrix
from bikechance_ml.models.gates import GateTableError
from bikechance_ml.models.predictor import BaselinePredictor, Prediction
from tests import composite_fixture as fixture
from tests import test_infer

SYSTEMS: Final[tuple[str, ...]] = ("hellocycling", "docomo-cycle")
FORMAT_1: Final[Path] = Path(__file__).parent / "fixtures" / "baseline_artifact" / "format_v1.json"


def serving_table(system_id: str, *, with_profile: bool = True) -> pa.Table:
    """**本物の `build_now`** で作った配信の表（推論の検査と同じ仕込み）。"""
    port = test_infer.ready_port()
    if with_profile:
        test_infer.put_profile(port, profile.source_day(jst_date(test_infer.AT)))
    return infer.read_features(port, system_id, test_infer.AT, frozenset()).ready.table


def _bits(values: np.ndarray[tuple[int, ...], np.dtype[np.float64]]) -> bytes:
    """確率の**バイト列**（`array_equal` は -0.0 と 0.0 を同じと見るので、ビットで比べる）。"""
    return np.ascontiguousarray(values).tobytes()


# ── 形 ────────────────────────────────────────────────────────
def test_the_composite_round_trips() -> None:
    made = fixture.build(dropped=[("hellocycling", "dock", 45, "6-10")])
    read = composite.from_bytes(composite.to_bytes(made))
    assert read.model_version == made.model_version
    assert read.created_at == made.created_at
    assert read.dropped == made.dropped
    assert dict(read.parts) == dict(made.parts)
    assert read.lightgbm_cells() == made.lightgbm_cells()


def test_the_same_composite_gives_the_same_bytes() -> None:
    """**同じ部品からは同じバイト列**（置き直しても中身が変わらないことを確かめられる）。"""
    assert composite.to_bytes(fixture.build()) == composite.to_bytes(fixture.build())


def test_derived_fields_come_from_the_parts() -> None:
    """`feature_set`・`train_days`・LightGBM の最終学習日は**部品から決まる**（書かない）。"""
    made = fixture.build()
    assert made.feature_set == fixture.FOREST.feature_set
    assert made.train_days == tuple(
        sorted(set(fixture.b3().train_days) | set(fixture.FOREST.train_days))
    )
    assert made.lightgbm_last_day == max(fixture.FOREST.train_days)
    document = json.loads(gzip.decompress(composite.to_bytes(made)).decode())
    assert set(document) == {
        "format_version",
        "kind",
        "model_version",
        "created_at",
        "dropped_cells",
        "parts",
    }


def test_train_days_follow_the_last_b3_day() -> None:
    """**鮮度の見張りは `max(train_days)` を見る**（0048）。和集合なら B3 の最終学習日になる。"""
    made = fixture.build()
    assert max(made.train_days) == max(max(fixture.b3().train_days), made.lightgbm_last_day)


def test_the_name_carries_the_last_b3_day() -> None:
    days = ("2026-10-06", "2026-10-12")
    assert composite.model_version_for(days) == "composite-v1-20261012"
    assert composite.model_version_for(days, "rehearsal") == "composite-v1-rehearsal-20261012"
    assert (
        composite.artifact_path("composite-v1-20261012")
        == "composite/composite-v1-20261012.json.gz"
    )


# ── 照合 ──────────────────────────────────────────────────────
def _rewritten(change: Callable[[dict[str, object]], None]) -> bytes:
    """書いた合成器の JSON を書き換えて、もう 1 度 gzip する。"""
    document = json.loads(gzip.decompress(composite.to_bytes(fixture.build())).decode())
    change(document)
    return gzip.compress(json.dumps(document).encode(), mtime=0)


def _part(document: dict[str, object], name: str) -> dict[str, str]:
    parts = document["parts"]
    assert isinstance(parts, dict)
    found = parts[name]
    assert isinstance(found, dict)
    return found


def test_a_part_with_a_wrong_digest_is_refused() -> None:
    """**部品が書き換わっていたら読まない**（中身を変えて、SHA-256 はそのまま）。"""

    def swap(document: dict[str, object]) -> None:
        _part(document, "gates")["base64"] = base64.b64encode(
            fixture.gates_body(lightgbm_cells=fixture.LIGHTGBM_CELLS[:1])
        ).decode()

    with pytest.raises(composite.CompositeFormatError, match="SHA-256"):
        composite.from_bytes(_rewritten(swap))


def test_a_part_that_is_not_base64_is_refused() -> None:
    def spoil(document: dict[str, object]) -> None:
        _part(document, "b3")["base64"] = "これは base64 ではない"

    with pytest.raises(composite.CompositeFormatError, match="base64"):
        composite.from_bytes(_rewritten(spoil))


def test_gates_decided_for_another_forest_are_refused() -> None:
    """**門の表は組になる森でしか使わない**（契約 35）。別の森の SHA-256 を持つ表は止める。"""
    other = fixture.gates_body(forest_body=b"another forest")
    with pytest.raises(GateTableError, match="別の森"):
        composite.assemble(
            fixture.parts(gates=other), model_version=fixture.VERSION, created_at=fixture.CREATED_AT
        )


def test_gates_named_for_another_version_are_refused() -> None:
    """森のバイト列が合っていても、**表の版の名前が違えば止める**（取り違えの印）。"""
    other = fixture.gates_body(model_version="lgbm-v1-20991231")
    with pytest.raises(composite.CompositeFormatError, match="門の表は"):
        composite.assemble(
            fixture.parts(gates=other), model_version=fixture.VERSION, created_at=fixture.CREATED_AT
        )


def test_a_b3_of_format_1_is_refused() -> None:
    """**B3 は書式 2 だけ**（W6-08）。版 1 の成果物は読めても、合成器には入れない。"""
    old = gzip.compress(FORMAT_1.read_bytes(), mtime=0)
    with pytest.raises(composite.CompositeFormatError, match="書式 2"):
        composite.assemble(
            fixture.parts(b3=old), model_version=fixture.VERSION, created_at=fixture.CREATED_AT
        )


def test_a_cell_that_goes_to_b3_cannot_be_dropped() -> None:
    """**削れるのは LightGBM に行くセルだけ**（W6-12：shadow の後は削るだけ。足さない）。"""
    b3_cell = fixture.B3_CELLS[0][0]
    with pytest.raises(composite.CompositeFormatError, match="足すことになる"):
        fixture.build(dropped=[b3_cell])
    with pytest.raises(composite.CompositeFormatError, match="足すことになる"):
        fixture.build(dropped=[("hellocycling", "bike", 120, "11+")])


def test_dropped_cells_are_unique_and_sorted() -> None:
    cells = sorted(fixture.LIGHTGBM_CELLS[:2])
    with pytest.raises(composite.CompositeFormatError, match="重複なく昇順"):
        fixture.build(dropped=[cells[1], cells[0]])
    with pytest.raises(composite.CompositeFormatError, match="重複なく昇順"):
        fixture.build(dropped=[cells[0], cells[0]])


def test_the_parts_are_exactly_three() -> None:
    three = fixture.parts()
    missing = {name: body for name, body in three.items() if name != composite.GATES_PART}
    with pytest.raises(composite.CompositeFormatError, match="部品が揃っていません"):
        composite.assemble(missing, model_version=fixture.VERSION, created_at=fixture.CREATED_AT)
    with pytest.raises(composite.CompositeFormatError, match="部品が揃っていません"):
        composite.assemble(
            {**three, "extra": b"x"}, model_version=fixture.VERSION, created_at=fixture.CREATED_AT
        )


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [("kind", "lightgbm", "合成器ではありません"), ("format_version", 99, "読めません")],
)
def test_another_kind_or_format_is_refused(name: str, value: object, message: str) -> None:
    def change(document: dict[str, object]) -> None:
        document[name] = value

    with pytest.raises(composite.CompositeFormatError, match=message):
        composite.from_bytes(_rewritten(change))


def test_a_system_unknown_to_b3_is_refused() -> None:
    """門の表に B3 の知らない系統があれば止める（その系統の行は B3 が出せない）。"""
    cells = (*fixture.LIGHTGBM_CELLS, ("unknown-system", "bike", 30, "0"))
    with pytest.raises(composite.CompositeFormatError, match="知らない系統"):
        composite.assemble(
            fixture.parts(gates=fixture.gates_body(lightgbm_cells=cells)),
            model_version=fixture.VERSION,
            created_at=fixture.CREATED_AT,
        )


# ── セルの引き方 ──────────────────────────────────────────────
def _small_table(h_min: list[int], bikes: list[int], docks: list[int]) -> pa.Table:
    return pa.table(
        {
            "h_min": pa.array(h_min, type=pa.int16()),
            "bikes": pa.array(bikes, type=pa.int16()),
            "docks": pa.array(docks, type=pa.int16()),
        }
    )


@pytest.mark.parametrize(
    ("bikes", "bucket"),
    [
        (-2, "0"),
        (0, "0"),
        (1, "1"),
        (2, "2"),
        (3, "3-5"),
        (5, "3-5"),
        (6, "6-10"),
        (10, "6-10"),
        (11, "11+"),
        (40, "11+"),
    ],
)
def test_the_bucket_edges_are_the_gate_cuts(bikes: int, bucket: str) -> None:
    """**バケツの境は門を決めたときと同じ**（`eval.dataset.bucket_index`。上限は含む）。"""
    cells = frozenset({("hellocycling", "bike", 15, bucket)})
    walks = composite.routes(cells, "hellocycling", _small_table([15, 30], [bikes, bikes], [0, 0]))
    assert walks["bike"].tolist() == [True, False]
    assert walks["dock"].tolist() == [False, False]


def test_the_counts_are_those_of_the_target() -> None:
    """**バケツは当てる側の台数**（`bike` は `bikes`、`dock` は `docks`）。"""
    cells = frozenset({("hellocycling", "dock", 15, "0")})
    walks = composite.routes(cells, "hellocycling", _small_table([15, 15], [0, 9], [9, 0]))
    assert walks["dock"].tolist() == [False, True]


def test_cells_of_another_system_are_not_walked() -> None:
    cells = frozenset({("docomo-cycle", "bike", 15, "0")})
    walks = composite.routes(cells, "hellocycling", _small_table([15], [0], [0]))
    assert not walks["bike"].any()


def test_an_unknown_horizon_is_refused() -> None:
    """**知らない水平は止める**（黙ってどこかのセルに落とさない）。"""
    with pytest.raises(UnknownHorizonError):
        composite.routes(frozenset(), "hellocycling", _small_table([17], [0], [0]))


# ── 予測：部品とビット一致（完了条件 1）─────────────────────────
def _components(system_id: str, table: pa.Table) -> tuple[Prediction, Prediction]:
    b3 = BaselinePredictor(fixture.b3()).predict(system_id, test_infer.AT, table)
    forest = lightgbm_artifact.to_predictor(fixture.FOREST).predict(system_id, test_infer.AT, table)
    return b3, forest


#: 系統 → ターゲット → 森を歩く行の数（仕込みの 3 ポートとセルの選び方から決まる）
EXPECTED_WALKS: Final[dict[str, dict[str, int]]] = {
    "hellocycling": {"bike": 2, "dock": 3},
    "docomo-cycle": {"bike": 1, "dock": 0},
}


@pytest.mark.parametrize("system_id", SYSTEMS)
def test_each_cell_matches_its_part_bit_for_bit(system_id: str) -> None:
    """**森を歩いた行は LightGBM 単体と、残りの行は B3 単体と、確率のバイト列まで同じ。**"""
    table = serving_table(system_id)
    made = composite.to_predictor(fixture.build())
    mixed = made.predict(system_id, test_infer.AT, table)
    b3, forest = _components(system_id, table)
    walks = made.walks(system_id, table)
    for name, rows in walks.items():
        assert int(rows.sum()) == EXPECTED_WALKS[system_id][name], (
            "仕込みのセルが行に当たっていない"
        )
        assert _bits(mixed.probability[name][rows]) == _bits(forest.probability[name][rows])
        assert _bits(mixed.probability[name][~rows]) == _bits(b3.probability[name][~rows])


@pytest.mark.parametrize("system_id", SYSTEMS)
def test_the_confidence_follows_w6_11(system_id: str) -> None:
    """**確度**：森の行は目標セルの履歴の厚さ（W6-11）、B3 の行は B3 の「気候値が引けたか」。"""
    table = serving_table(system_id)
    made = composite.to_predictor(fixture.build())
    mixed = made.predict(system_id, test_infer.AT, table)
    b3, _ = _components(system_id, table)
    thick = lightgbm_artifact.profile_informed(table)
    assert thick.any() and not thick.all(), "仕込みの厚さが下限の両側に散っていない"
    for name, rows in made.walks(system_id, table).items():
        assert mixed.informed[name][rows].tolist() == thick[rows].tolist()
        assert mixed.informed[name][~rows].tolist() == b3.informed[name][~rows].tolist()


def test_the_two_confidence_rules_disagree_on_some_forest_row() -> None:
    """**検査の検査**：森の行の中に、B3 の確度と履歴の厚さが食い違う行がある。

    無いと、森の行に B3 の確度を出しても上の検査が落ちない（W6-11 を外しても通る）。
    """
    disagree = 0
    for system_id in SYSTEMS:
        table = serving_table(system_id)
        b3, _ = _components(system_id, table)
        thick = lightgbm_artifact.profile_informed(table)
        walks = composite.to_predictor(fixture.build()).walks(system_id, table)
        disagree += sum(
            int((thick[rows] != b3.informed[name][rows]).sum()) for name, rows in walks.items()
        )
    assert disagree > 0


def test_the_b3_only_form_is_exactly_b3() -> None:
    """**プロファイルを読めなかった周期の形**（契約 33）：全行が B3 と同じで、森を歩かない。"""
    table = serving_table("hellocycling", with_profile=False)
    only = composite.to_predictor(fixture.build()).b3_only()
    mixed = only.predict("hellocycling", test_infer.AT, table)
    b3, _ = _components("hellocycling", table)
    for name in ("bike", "dock"):
        assert _bits(mixed.probability[name]) == _bits(b3.probability[name])
        assert mixed.informed[name].tolist() == b3.informed[name].tolist()
        assert not only.walks("hellocycling", table)[name].any()
    assert (only.route, only.model_version) == (composite.ROUTE_B3_ONLY, fixture.VERSION)


def test_dropped_cells_go_back_to_b3() -> None:
    """**削ったセルは B3 を配る**（W6-12）。ほかの森のセルはそのまま。"""
    dropped = ("hellocycling", "dock", 45, "6-10")
    table = serving_table("hellocycling")
    made = composite.to_predictor(fixture.build(dropped=[dropped]))
    walks = made.walks("hellocycling", table)
    assert int(walks["dock"].sum()) == EXPECTED_WALKS["hellocycling"]["dock"] - 2
    b3, _ = _components("hellocycling", table)
    mixed = made.predict("hellocycling", test_infer.AT, table)
    assert _bits(mixed.probability["dock"][~walks["dock"]]) == _bits(
        b3.probability["dock"][~walks["dock"]]
    )


def test_unknown_ports_are_counted_by_b3() -> None:
    """**知らないポートは B3 の数え方**（B3 のセルでは気候値が引けず B1 だけになる）。"""
    table = serving_table("hellocycling")
    made = composite.to_predictor(fixture.build())
    assert made.unknown_ports("hellocycling", table) == BaselinePredictor(
        fixture.b3()
    ).unknown_ports("hellocycling", table)


def test_no_forest_row_means_no_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    """**森を歩く行が無い周期は、行列も作らない**（ドコモの dock のような系統・ターゲット）。"""
    built: list[int] = []
    original = matrix.build

    def counting(
        table: pa.Table, keep: np.ndarray[tuple[int, ...], np.dtype[np.bool_]] | None = None
    ) -> object:
        built.append(table.num_rows)
        return original(table, keep)

    monkeypatch.setattr(composite, "build_matrix", counting)
    table = serving_table("docomo-cycle")
    empty = composite.assemble(
        fixture.parts(gates=fixture.gates_body(lightgbm_cells=[("hellocycling", "bike", 30, "0")])),
        model_version=fixture.VERSION,
        created_at=fixture.CREATED_AT,
    )
    composite.to_predictor(empty).predict("docomo-cycle", test_infer.AT, table)
    assert built == []
