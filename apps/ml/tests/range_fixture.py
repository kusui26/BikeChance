"""Range 要求を返す偽の Storage と、全セルを持つ合成のプロファイル（W6 の PR D）。

**本物の応答と同じ規則で返す**（2026-09-27 に本番で確かめた）：

  * `bytes=-N` は末尾の N バイト。**ファイルより長ければ全体**
  * `bytes=a-b` は **b を含む**。ファイルの終わりを越えたぶんは切る
  * 応答は**全体の大きさ**と**版（ETag）**を持つ

`test_range_file.py`（読み口）と `test_infer.py`（推論の入口）が同じものを使う。
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

import pyarrow as pa

from bikechance_ml.features import profile
from bikechance_ml.features.calendar import DOW_TYPE_ORDER
from bikechance_ml.io.range_file import Piece

#: 版の目印（本番の ETag と同じく二重引用符で囲む）。
ETAG: Final[str] = '"v1"'


def serve(body: bytes, byte_range: str, etag: str | None = ETAG) -> Piece:
    """`Range` の値どおりに切り出す。**`bytes=-N` と `bytes=a-b` の 2 つだけ**を受ける。"""
    spec = byte_range.removeprefix("bytes=")
    if spec.startswith("-"):
        start = max(0, len(body) - int(spec[1:]))
        stop = len(body)
    else:
        first, last = spec.split("-")
        start, stop = int(first), min(len(body), int(last) + 1)
    return Piece(start=start, body=body[start:stop], size=len(body), etag=etag)


@dataclass
class MemoryStorage:
    """パス → バイト列を持ち、Range 要求に答える。**頼まれた範囲をすべて覚える。**"""

    files: Mapping[str, bytes]
    requests: list[str] = field(default_factory=list)

    def download_range(self, bucket: str, path: str, byte_range: str) -> Piece | None:
        self.requests.append(byte_range)
        body = self.files.get(path)
        return None if body is None else serve(body, byte_range)


def full_profile(ports: tuple[tuple[str, str], ...]) -> pa.Table:
    """**全セル**（ポート × 3 曜日種別 × 96 枠）を持つプロファイル。**セルごとに値が違う。**

    同じ値のセルが並ぶと、違うセルを引いても特徴量が変わらず、引き間違いを見逃す。
    日数・格子点・台数の和・流量の和を、ポートと曜日種別と枠から決まる別々の値にする。
    """
    cells = [
        (port, dow, slot)
        for port in range(len(ports))
        for dow in range(len(DOW_TYPE_ORDER))
        for slot in range(profile.SLOTS_PER_DAY)
    ]
    rows = [_cell_row(ports[port], dow, slot, port) for port, dow, slot in cells]
    columns = {
        name: pa.array([row[name] for row in rows], type=profile.PROFILE_SCHEMA.field(name).type)
        for name in profile.PROFILE_SCHEMA.names
    }
    table = pa.table(columns, schema=profile.PROFILE_SCHEMA)
    return table.sort_by([(one, "ascending") for one in profile.PROFILE_ORDER])


def _cell_row(port: tuple[str, str], dow: int, slot: int, index: int) -> dict[str, object]:
    days = 1 + (slot + dow + index) % 7
    points = 3 * days
    bikes_ok = (slot + index) % (points + 1)
    return {
        "system_id": port[0],
        "station_id": port[1],
        "dow_type": DOW_TYPE_ORDER[dow],
        "slot15": slot,
        "n_days": days,
        "n": points,
        "n_suspended": 0,
        "n_bike_ok": bikes_ok,
        "n_dock_ok": points - bikes_ok,
        "sum_bikes": points * (slot % 11 + dow + index),
        "sum_bikes_sq": points * (slot % 11 + dow + index) ** 2 + slot,
        "sum_rentals_60": slot * (dow + 1) + index,
        "sum_returns_60": (95 - slot) * (dow + 1) + index,
    }
