"""ゴールデンフィクスチャを作り直す（W3 プラン §5.8、§12 の 93）。

    cd apps/ml && ./.venv/bin/python -m tests.gen_golden

`snapshots.csv`（入力）・`profile.csv`（入力。v4 の `prof_*` の源）・`expected.csv`
（期待値）を書き出す。**規則を変えたら必ず走らせ、差分をレビューする。** 差分が
読めなくなったら、それは規則の変更が大きすぎるか、フィクスチャが複雑すぎるかの
どちらかである。

`reference.json` は手で書いたもので、ここでは触らない。
"""

import csv
import io
from datetime import datetime, timedelta
from typing import Final

import pyarrow as pa

from bikechance_ml.features import build, profile
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


_PROFILE_HEADER: Final[str] = """\
# 特徴量パイプラインのゴールデンフィクスチャ（入力。profile(2026-09-06) の代わり）。
# 生成: cd apps/ml && ./.venv/bin/python -m tests.gen_golden
# **手で編集しない。** 仕込みの意味は tests/gen_golden.py の _profile_cells にある。
"""

#: 目標時刻がとりうる枠（基準 08:00〜14:00 JST に水平 5〜180 分を足した 08:05〜17:00）を
#: 少し広めに覆う。**07:00〜18:00 の 44 枠。**
_PROFILE_SLOTS: Final[range] = range(28, 72)

#: `p1` に**枠の穴**を空ける（10:00〜11:00）。そこに目標時刻が落ちる行は `prof_*` が NULL
_HOLE: Final[range] = range(40, 44)

#: ポートごとの日数（**厚み**）。`d1` だけ枠で変える（`prof_n_days` が行で揺れるように）。
_DAYS: Final[dict[str, int]] = {"p1": 5, "p2": 2, "ghost": 4}


def _profile_cells() -> list[tuple[str, str, str, int]]:
    """`(system_id, station_id, dow_type, slot15)` の一覧。**4 つ仕込む。**

    * `p1` は平日の全枠。ただし**穴**（`_HOLE`）がある
    * `p2` は**偶数の枠だけ**で、日数も 2 日と薄い
    * `p1` に **`sat` のセル**も置く。**月曜の行が拾ってはいけない**（曜日種別も鍵のうち）
    * **台帳に無いポート**（`ghost`）。引かれることが無いので捨てられる
    """
    cells: list[tuple[str, str, str, int]] = []
    for slot in _PROFILE_SLOTS:
        if slot not in _HOLE:
            cells.append(("hellocycling", "p1", "weekday", slot))
        if slot % 2 == 0:
            cells.append(("hellocycling", "p2", "weekday", slot))
        cells.append(("docomo-cycle", "d1", "weekday", slot))
        cells.append(("hellocycling", "p1", "sat", slot))
    cells.append(("hellocycling", "ghost", "weekday", _HOLE.start))
    return sorted(cells)


def _profile_row(system_id: str, station_id: str, dow_type: str, slot: int) -> dict[str, object]:
    """1 セルぶんの和。**格子点ごとの台数を作ってから足す**ので、和どうしが矛盾しない。"""
    days = _DAYS.get(station_id, 1 + slot % 3) + (3 if dow_type == "sat" else 0)
    points = profile.GRID_POINTS_PER_SLOT * days
    bikes = [(slot + point) % 7 for point in range(points)]
    # `d1` は 4 枠に 1 つ、1 点だけ休止していた（休止の点は借りも返しもできない）
    suspended = 1 if station_id == "d1" and slot % 4 == 0 else 0
    return {
        "system_id": system_id,
        "station_id": station_id,
        "dow_type": dow_type,
        "slot15": slot,
        "n_days": days,
        "n": points,
        "n_suspended": suspended,
        "n_bike_ok": sum(1 for one in bikes if one > 0) - suspended,
        "n_dock_ok": points - slot % 2 - suspended,
        "sum_bikes": sum(bikes),
        "sum_bikes_sq": sum(one * one for one in bikes),
        "sum_rentals_60": (slot % 5) * points,
        "sum_returns_60": (slot % 4) * points,
    }


def write_profile() -> int:
    rows = [_profile_row(*cell) for cell in _profile_cells()]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    fixture.PROFILE.write_text(_PROFILE_HEADER + buffer.getvalue(), encoding="utf-8")
    return len(rows)


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
    print(f"profile.csv:   {write_profile()} 行")
    print(f"expected.csv:  {write_expected()} 行")
