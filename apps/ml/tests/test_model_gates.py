"""門の表（`models/gates.py`、W6 の PR F が書き PR H が読む。契約 35）。

**主題は「読む側が、別の切り方で決めた表を使わないこと」。** 門の表はセルの名前
（水平・台数バケツ）で LightGBM に回すところを決める。バケツの境目が 1 つずれると、
**同じ名前のセルが別の行を指し、例外を出さずに別のところで LightGBM を配る。**
"""

import gzip
import hashlib
import json
from collections.abc import Callable
from typing import Final

import pytest

from bikechance_ml.eval import gates as judging
from bikechance_ml.eval.dataset import BUCKET_EDGES, BUCKET_LABELS
from bikechance_ml.features.constants import HORIZONS_MIN
from bikechance_ml.models import gates

VERSION: Final[str] = "lgbm-v1-20261005"
RULE: Final[dict[str, object]] = {"min_improvement": 0.1, "judgeable_brier": 0.001}

#: 組になる森の成果物（**中身は何でもよい**。表が見るのはバイト列の SHA-256 だけ）。
FOREST: Final[bytes] = b"forest artifact bytes"
FOREST_SHA256: Final[str] = hashlib.sha256(FOREST).hexdigest()


def _cell(
    *,
    target: str = "bike",
    h_min: int = 30,
    bucket: str = "0",
    reason: str = "passed",
    system: str = "hellocycling",
) -> gates.GateCell:
    route = gates.TO_LIGHTGBM if reason == gates.PASSED else gates.TO_B3
    return gates.GateCell(
        system=system,
        target=target,
        h_min=h_min,
        bucket=bucket,
        route=route,
        reason=reason,
        n=120,
        b0=0.25,
        b3=0.09,
        lgbm=0.0784,
        improvement=0.128888889,
    )


CELLS: Final[tuple[gates.GateCell, ...]] = (
    _cell(),
    _cell(target="dock", bucket="3-5", reason="below"),
    _cell(h_min=5, bucket="11+", reason="excluded"),
    _cell(system="docomo-cycle", h_min=180, bucket="1"),
)
TABLE: Final[gates.GateTable] = gates.build(
    VERSION, FOREST_SHA256, CELLS, ["2026-10-07", "2026-10-08"], RULE
)


def _tampered(change: Callable[[dict[str, object]], None]) -> bytes:
    """書き出した表を開いて 1 か所変え、閉じ直す（**読む側の照合を試す**）。"""
    document = json.loads(gzip.decompress(gates.to_bytes(TABLE)).decode())
    change(document)
    return gzip.compress(json.dumps(document).encode())


def _first_cell(document: dict[str, object]) -> dict[str, object]:
    cells = document["cells"]
    assert isinstance(cells, list)
    first = cells[0]
    assert isinstance(first, dict)
    return first


# ── 往復 ──────────────────────────────────────────────────────
def test_the_table_round_trips() -> None:
    assert gates.from_bytes(gates.to_bytes(TABLE)) == TABLE


def test_the_same_table_gives_the_same_bytes() -> None:
    """**同じ当てはめからは同じバイト列**（gzip の時刻を 0 にし、鍵とセルを並べる）。"""
    shuffled = gates.build(
        VERSION, FOREST_SHA256, tuple(reversed(CELLS)), ["2026-10-07", "2026-10-08"], RULE
    )
    assert gates.to_bytes(shuffled) == gates.to_bytes(TABLE)


def test_only_the_passed_cells_go_to_lightgbm() -> None:
    """**LightGBM を配るのは `passed` のセルだけ。** 表に無いセルと、それ以外は B3。"""
    assert TABLE.lightgbm_cells() == {
        ("hellocycling", "bike", 30, "0"),
        ("docomo-cycle", "bike", 180, "1"),
    }


def test_the_current_cuts_are_burned_in() -> None:
    assert TABLE.horizons_min == tuple(HORIZONS_MIN)
    assert TABLE.bucket_labels == tuple(BUCKET_LABELS)
    assert TABLE.bucket_edges == tuple(BUCKET_EDGES)


def test_the_table_sits_next_to_the_forest() -> None:
    """**LightGBM の成果物の隣**（`lightgbm/<版>.json.gz`）。`models` バケットは gzip だけ。"""
    assert gates.gates_path(VERSION) == f"lightgbm/{VERSION}.gates.json.gz"
    assert gates.CONTENT_TYPE == "application/gzip"


# ── 門を当てた結果から組む ─────────────────────────────────────
def _scored(*, b0: float, b3: float, model: float, bucket: str | None = "0") -> judging.Scored:
    return judging.Scored(
        system="hellocycling",
        target="bike",
        h_min=30,
        bucket=bucket,
        n=50,
        weight=50.0,
        b0=b0,
        b3=b3,
        model=model,
    )


def _judged(*cells: judging.Scored) -> judging.Judged:
    return judging.Judged(model="LGBM", cells=cells, horizons=(), served={}, holdout={})


@pytest.mark.parametrize(
    ("scored", "route", "reason"),
    [
        (_scored(b0=0.2, b3=0.10, model=0.08), gates.TO_LIGHTGBM, judging.PASSED),
        (_scored(b0=0.2, b3=0.10, model=0.095), gates.TO_B3, judging.BELOW),
        (_scored(b0=0.0005, b3=0.10, model=0.01), gates.TO_B3, judging.EXCLUDED),
    ],
)
def test_the_route_follows_the_judgement(scored: judging.Scored, route: str, reason: str) -> None:
    """**10% 以上なら LightGBM、届かないか判定の対象外なら B3**（契約 35）。"""
    table = gates.from_judged(_judged(scored), VERSION, FOREST_SHA256, ["2026-10-07"])
    (cell,) = table.cells
    assert (cell.route, cell.reason) == (route, reason)
    assert (cell.b0, cell.b3, cell.lgbm, cell.n) == (scored.b0, scored.b3, scored.model, 50)


def test_the_rule_is_burned_in_from_the_judging_side() -> None:
    """**規則の数字は `eval/gates.py` の 1 か所から**（表の側で書き直さない）。"""
    table = gates.from_judged(_judged(), VERSION, FOREST_SHA256, ["2026-10-07"])
    assert table.rule == {
        "unit": judging.CELL_GATE,
        "reference": judging.REFERENCE,
        "min_improvement": judging.MIN_IMPROVEMENT,
        "judgeable_brier": judging.JUDGEABLE_BRIER,
    }
    assert gates.from_bytes(gates.to_bytes(table)) == table


def test_a_horizon_unit_is_not_a_cell() -> None:
    """**水平単位の切り口は表に入れない**（バケツが無いと、読む側が行き先を決められない）。"""
    with pytest.raises(gates.GateTableError, match="セル単位でない"):
        gates.cell_of(_scored(b0=0.2, b3=0.1, model=0.08, bucket=None))


# ── 森との組 ──────────────────────────────────────────────────
def test_the_table_accepts_its_own_forest() -> None:
    gates.refuse_other_forest(gates.from_bytes(gates.to_bytes(TABLE)), FOREST)


def test_a_forest_put_again_under_the_same_name_is_refused() -> None:
    """**同じ名前で置き直した森と、前の表を組にしない。**

    candidate の名前は置き直せる（W6-19）。森だけ置き直して表の置き直しが落ちると、
    **名前は同じまま、新しい森と古い表が組になる**——セルの行き先が別の森の成績で決まる。
    """
    with pytest.raises(gates.GateTableError, match="別の森"):
        gates.refuse_other_forest(TABLE, FOREST + b" fitted again")


@pytest.mark.parametrize("value", ["", "ABC", FOREST_SHA256.upper(), FOREST_SHA256[:63]])
def test_a_malformed_forest_digest_is_refused(value: str) -> None:
    """**印の形が違えば読まない**（大文字・短い・空）。突き合わせが素通りする形を作らない。"""
    with pytest.raises(gates.GateTableError, match="SHA-256"):
        gates.from_bytes(_tampered(lambda document: document.__setitem__("artifact_sha256", value)))


# ── 読む側の照合 ───────────────────────────────────────────────
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("horizons_min", [5, 10, 15, 20, 30, 45, 60, 90, 120, 240]),
        ("bucket_labels", ["0", "1", "2", "3-4", "5-10", "11+"]),
        ("bucket_edges", [0, 1, 2, 4, 10]),
    ],
)
def test_a_table_cut_another_way_is_refused(name: str, value: list[object]) -> None:
    """**水平やバケツの切り方が違う表は使わない**（同じ名前のセルが別の行を指す）。"""
    with pytest.raises(gates.GateTableError):
        gates.from_bytes(_tampered(lambda document: document.__setitem__(name, value)))


def test_something_that_is_not_a_gate_table_is_refused() -> None:
    """**成果物（森）を門の表として読まない**（種類の印を見る）。"""
    with pytest.raises(gates.GateTableError, match="門の表ではありません"):
        gates.from_bytes(_tampered(lambda document: document.__setitem__("kind", "lightgbm")))


def test_an_unknown_format_is_refused() -> None:
    with pytest.raises(gates.GateTableError, match="書式"):
        gates.from_bytes(_tampered(lambda document: document.__setitem__("format_version", 2)))


def test_a_route_that_contradicts_its_reason_is_refused() -> None:
    """**`below` なのに LightGBM に回す表は使わない**（行き先は理由から決まる）。"""

    def contradict(document: dict[str, object]) -> None:
        _first_cell(document)["reason"] = "below"

    with pytest.raises(gates.GateTableError, match="行き先が理由と合いません"):
        gates.from_bytes(_tampered(contradict))


@pytest.mark.parametrize(
    ("name", "value"),
    [("target", "scooter"), ("h_min", 7), ("bucket", "12+"), ("route", "b2"), ("reason", "lucky")],
)
def test_an_unknown_value_in_a_cell_is_refused(name: str, value: object) -> None:
    def change(document: dict[str, object]) -> None:
        _first_cell(document)[name] = value

    with pytest.raises(gates.GateTableError):
        gates.from_bytes(_tampered(change))


def test_a_cell_listed_twice_is_refused() -> None:
    """**同じセルが 2 度あれば、どちらが正しいか分からない**ので使わない。"""

    def duplicate(document: dict[str, object]) -> None:
        cells = document["cells"]
        assert isinstance(cells, list)
        cells.append(dict(cells[0]))

    with pytest.raises(gates.GateTableError, match="2 度"):
        gates.from_bytes(_tampered(duplicate))


@pytest.mark.parametrize(("name", "value"), [("n", True), ("n", 1.5), ("b3", "0.09")])
def test_a_number_of_the_wrong_kind_is_refused(name: str, value: object) -> None:
    """**真偽値を数として読まない**（`True` は Python では整数の子）。"""

    def change(document: dict[str, object]) -> None:
        _first_cell(document)[name] = value

    with pytest.raises(gates.GateTableError):
        gates.from_bytes(_tampered(change))


def test_building_a_contradicting_table_is_refused_too() -> None:
    """**書く側でも止める**——読めない表を置かない。"""
    broken = gates.GateCell(
        system="hellocycling",
        target="bike",
        h_min=30,
        bucket="0",
        route=gates.TO_LIGHTGBM,
        reason="excluded",
        n=1,
        b0=0.0,
        b3=0.0,
        lgbm=0.0,
        improvement=None,
    )
    with pytest.raises(gates.GateTableError):
        gates.build(VERSION, FOREST_SHA256, (broken,), ["2026-10-07"], RULE)
