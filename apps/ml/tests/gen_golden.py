"""ゴールデンフィクスチャを作り直す（W3 プラン §5.8、§12 の 93）。

    cd apps/ml && ./.venv/bin/python -m tests.gen_golden

`snapshots.csv`（入力）と `expected.csv`（期待値）を書き出す。**規則を変えたら
必ず走らせ、差分をレビューする。** 差分が読めなくなったら、それは規則の変更が
大きすぎるか、フィクスチャが複雑すぎるかのどちらかである。

`reference.json` は手で書いたもので、ここでは触らない。
"""

import csv
import io
from datetime import datetime, timedelta
from typing import Final

import pyarrow as pa

from bikechance_ml.features import build
from bikechance_ml.features.schema import SCHEMA
from tests import features_fixture as fixture

#: 仕込みの一覧。`(system_id, station_id, 周期秒, 台数の作り方)`。
_HELLO_CADENCE_S: Final[int] = 300
_DOCOMO_CADENCE_S: Final[int] = 81

_HEADER: Final[str] = """\
# 特徴量パイプラインのゴールデンフィクスチャ（入力）。
# 生成: cd apps/ml && ./.venv/bin/python -m tests.gen_golden
# **手で編集しない。** 仕込みの意味は tests/features_fixture.py の docstring にある。
"""


def _bikes(station_id: str, step: int) -> int:
    """台数の作り方。**ポートごとに違う波**にして、ラグと流量に差が出るようにする。"""
    if station_id == "p1":
        return (step * 2) % 9
    if station_id == "p2":
        return 1 if step % 7 else 0
    if station_id == "d1":
        return (step // 3) % 5
    return 3


def _rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for system_id, station_id, cadence in (
        ("hellocycling", "p1", _HELLO_CADENCE_S),
        ("hellocycling", "p2", _HELLO_CADENCE_S),
        ("docomo-cycle", "d1", _DOCOMO_CADENCE_S),
        ("docomo-cycle", "5753", _DOCOMO_CADENCE_S),
    ):
        rows.extend(_station_rows(system_id, station_id, cadence))
    rows.sort(
        key=lambda row: (str(row["system_id"]), str(row["station_id"]), str(row["observed_at"]))
    )
    return rows


def _station_rows(system_id: str, station_id: str, cadence_s: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    at, step = fixture.FROM, 0
    while at < fixture.TO:
        rows.append(
            {
                "system_id": system_id,
                "station_id": station_id,
                "observed_at": at.isoformat(),
                "fetched_at": (at + fixture.FETCH_DELAY).isoformat(),
                **_values(station_id, step),
                "reported_age_s": 30 if system_id == "hellocycling" else 0,
            }
        )
        at += timedelta(seconds=cadence_s)
        step += 1
    return rows


def _values(station_id: str, step: int) -> dict[str, object]:
    """台数・返却枠・`flags`。**p2 に停止と欠測を仕込む。**"""
    bikes = _bikes(station_id, step)
    if station_id == "p2" and 20 <= step < 26:
        return {"bikes": -1, "docks": -1, "flags": -1}
    if station_id == "p2" and 40 <= step < 46:
        return {"bikes": bikes, "docks": 9 - bikes, "flags": 1}
    if station_id == "5753":
        return {"bikes": 3, "docks": 9997, "flags": 7}
    return {"bikes": bikes, "docks": 9 - bikes, "flags": 7}


def write_snapshots() -> int:
    rows = _rows()
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    fixture.SNAPSHOTS.write_text(_HEADER + buffer.getvalue(), encoding="utf-8")
    return len(rows)


def write_expected() -> int:
    built = build.build_day(fixture.build_inputs())
    fixture.EXPECTED.write_text(to_csv(built.table), encoding="utf-8")
    return int(built.table.num_rows)


def to_csv(table: pa.Table) -> str:
    """表を CSV にする。**null は空欄**、浮動小数は 6 桁で丸めて差分を安定させる。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(SCHEMA.names)
    for row in table.to_pylist():
        writer.writerow([_cell(row[name]) for name in SCHEMA.names])
    return buffer.getvalue()


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6f}"
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


if __name__ == "__main__":
    print(f"snapshots.csv: {write_snapshots()} 行")
    print(f"expected.csv:  {write_expected()} 行")
