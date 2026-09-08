"""JSON の形を検査して型を付ける（CLAUDE.md §3「`as` キャスト禁止 → 型ガード」）。

PostgREST の応答はまず `object` として受け取り、ここを通してから使う。TypeScript 側の
型ガードと同じ役割で、**想定と違う形を静かに通さない**。

エラーには**場所と期待だけ**を書き、値は入れない。応答の断片がそのままログに出る経路を
作らないため（CLAUDE.md §5）。
"""

from collections.abc import Mapping


class ShapeError(ValueError):
    """JSON の形が想定と違う。"""


def as_list(value: object, where: str) -> list[object]:
    if not isinstance(value, list):
        raise ShapeError(f"{where}: 配列を期待した")
    return value


def as_dict(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ShapeError(f"{where}: オブジェクトを期待した")
    fields: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ShapeError(f"{where}: キーが文字列でない")
        fields[key] = item
    return fields


def as_str(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise ShapeError(f"{where}: 文字列を期待した")
    return value


def as_int(value: object, where: str) -> int:
    # bool は int の派生。真偽値が数として通ると、後段で 0/1 に化けて気づけない
    if isinstance(value, bool) or not isinstance(value, int):
        raise ShapeError(f"{where}: 整数を期待した")
    return value


def as_float(value: object, where: str) -> float:
    # bool は int の派生、int は float として通したい。**bool だけを弾く**
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ShapeError(f"{where}: 数を期待した")
    return float(value)


def as_bool(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise ShapeError(f"{where}: 真偽値を期待した")
    return value


def as_int_list(value: object, where: str) -> list[int]:
    """整数の配列。`smallint[]` は要素に null を含まない（欠損は -1）。"""
    items = as_list(value, where)
    numbers: list[int] = []
    for item in items:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ShapeError(f"{where}: 要素が整数でない")
        numbers.append(item)
    return numbers


def field(row: Mapping[str, object], name: str, where: str) -> object:
    if name not in row:
        raise ShapeError(f"{where}: {name} がない")
    return row[name]
