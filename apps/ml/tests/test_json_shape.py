"""JSON の形の検査（`bikechance_ml/json_shape.py`）。

PostgREST の応答を信用しすぎないための関門なので、**通してはいけないもの**を厚く書く。
"""

import pytest

from bikechance_ml.json_shape import (
    ShapeError,
    as_dict,
    as_int,
    as_int_list,
    as_list,
    as_str,
    field,
)


def test_as_list_accepts_a_list() -> None:
    assert as_list([1, "a"], "where") == [1, "a"]


def test_as_list_rejects_a_dict() -> None:
    with pytest.raises(ShapeError):
        as_list({"a": 1}, "systems")


def test_as_dict_accepts_string_keys() -> None:
    assert as_dict({"a": 1}, "where") == {"a": 1}


def test_as_dict_rejects_a_list() -> None:
    with pytest.raises(ShapeError):
        as_dict([1], "where")


def test_as_str_rejects_a_number() -> None:
    with pytest.raises(ShapeError):
        as_str(1, "where")


def test_as_int_rejects_bool() -> None:
    """bool は int の派生。素通しすると真偽値が 0/1 に化けて気づけない。"""
    with pytest.raises(ShapeError):
        as_int(True, "where")


def test_as_int_rejects_float() -> None:
    with pytest.raises(ShapeError):
        as_int(1.5, "where")


def test_as_int_list_accepts_negative_values() -> None:
    """`-1` は欠損を表す正当な値（W1 プラン §11.1）。"""
    assert as_int_list([0, -1, 3], "bikes") == [0, -1, 3]


def test_as_int_list_rejects_null_element() -> None:
    with pytest.raises(ShapeError):
        as_int_list([1, None], "bikes")


def test_field_reports_the_missing_name() -> None:
    with pytest.raises(ShapeError, match="idx"):
        field({"station_id": "a"}, "idx", "stations")
