"""ポートの静的な特徴量（開発プラン §6.3 の「ポート静的」）。

入力は**参照データ**（`stations` / `station_attributes` / `muni_codes` の結果）で、
Parquet ではない。`io/supabase.py` が読み、ここは並べ替えと導出だけを行う。

**ドコモの `capacity` は動的値**（`bikes + docks`）で、`station_attributes` に入って
いるのは取り込み時点の凍結値にすぎない（開発プラン §3.6）。だから推定値を使う。
ただし**「その日の最大」を使うと未来を覗く**ので、`fetched_at <= t` の観測だけを見た
**累積最大**にする。日が進むにつれて推定が良くなる形で、リークが無い。

HELLO は宣言値（`vehicle_capacity`）をそのまま使う。こちらは固定ラック数なので
動かない。
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np

from bikechance_ml.features.arrays import Bools, Float32, Float64, Int8, Int32, Int64
from bikechance_ml.features.asof import Observations
from bikechance_ml.features.constants import (
    MISSING,
    SYSTEMS_WITH_DYNAMIC_CAPACITY,
    SYSTEMS_WITH_GAP,
    SYSTEMS_WITH_REPORTED_AGE,
)
from bikechance_ml.features.reference import (
    StationAttributeRow,
    StationGeoRow,
    SystemReference,
)

_MS_PER_DAY: Final[int] = 86_400_000

#: 数値の欠損を表す番兵。**Parquet には null として書く**ので外には出ない。
NO_CODE: Final[int] = -1


@dataclass(frozen=True)
class StationFacts:
    """台帳と属性を、ポートの並び順にそろえたもの。長さはすべて等しい。

    `-1` は「無い」。`pref_code` / `muni_code` はドコモでは常に無く、`region_id` は
    HELLO では常に無い（W3 プラン §9.5）。`is_charging_station` は HELLO 専用。
    """

    system_ids: tuple[str, ...]
    station_ids: tuple[str, ...]
    first_seen_ms: Int64
    lat: Float64
    lon: Float64
    declared_capacity: Int32
    pref_code: Int32
    muni_code: Int32
    region_id: Int32
    #: 3 値。1 = そう、0 = 違う、-1 = そもそも情報が無い（ドコモ）
    is_charging_station: Int8
    #: システムごとの癖（`constants.py`）。ポート単位の真偽にしておくと、
    #: 2 システムを 1 つの台帳で扱っても分岐が要らない
    has_gap: Bools
    has_reported_age: Bools
    has_dynamic_capacity: Bools

    def __len__(self) -> int:
        return len(self.station_ids)

    def station_keys(self) -> tuple[tuple[str, str], ...]:
        """`(system_id, station_id)` の並び。観測と近傍の位置はこれで決まる。"""
        return tuple(zip(self.system_ids, self.station_ids, strict=True))


def to_facts(systems: Sequence[SystemReference]) -> StationFacts:
    """参照データを、**システムの順 → 台帳の順**に並べた配列にする。

    属性を持たないポートがある（`status` にしか現れない）。その場合は座標も容量も
    「無い」として並べる。**行を落とさない**（落とすと近傍の位置がずれる）。
    """
    pairs = [pair for system in systems for pair in _ordered(system)]
    return StationFacts(
        system_ids=tuple(system_id for system_id, _, _ in pairs),
        station_ids=tuple(geo.station_id for _, geo, _ in pairs),
        first_seen_ms=np.array(
            [int(geo.first_seen_at.timestamp() * 1000) for _, geo, _ in pairs], dtype=np.int64
        ),
        lat=_floats(attribute.lat if attribute else None for _, _, attribute in pairs),
        lon=_floats(attribute.lon if attribute else None for _, _, attribute in pairs),
        declared_capacity=_codes(
            attribute.capacity if attribute else None for _, _, attribute in pairs
        ),
        pref_code=_codes(geo.pref_code for _, geo, _ in pairs),
        muni_code=_codes(geo.muni_code for _, geo, _ in pairs),
        region_id=_codes(attribute.region_id if attribute else None for _, _, attribute in pairs),
        is_charging_station=np.array(
            [
                _tri_state(attribute.is_charging_station if attribute else None)
                for _, _, attribute in pairs
            ],
            dtype=np.int8,
        ),
        has_gap=_in(pairs, SYSTEMS_WITH_GAP),
        has_reported_age=_in(pairs, SYSTEMS_WITH_REPORTED_AGE),
        has_dynamic_capacity=_in(pairs, SYSTEMS_WITH_DYNAMIC_CAPACITY),
    )


#: 1 ポートぶんの参照データ：システム・台帳の行・属性の行（無いこともある）。
type _Pair = tuple[str, StationGeoRow, StationAttributeRow | None]


def _ordered(system: SystemReference) -> list[_Pair]:
    """1 システムを台帳の順（`station_id` の昇順）に並べ、属性を突き合わせる。"""
    by_id = {one.station_id: one for one in system.attributes}
    return [
        (system.system_id, row, by_id.get(row.station_id))
        for row in sorted(system.geo, key=lambda one: one.station_id)
    ]


def _floats(values: Iterable[float | None]) -> Float64:
    """無い値は NaN。**0 で埋めない**（赤道上のポートになってしまう）。"""
    return np.array([np.nan if one is None else one for one in values], dtype=np.float64)


def _codes(values: Iterable[int | None]) -> Int32:
    """無い値は `NO_CODE`。書き出すときに NULL に直す。"""
    return np.array([NO_CODE if one is None else one for one in values], dtype=np.int32)


def _tri_state(value: bool | None) -> int:
    return NO_CODE if value is None else int(value)


def _in(pairs: Sequence[_Pair], systems: frozenset[str]) -> Bools:
    return np.array([system_id in systems for system_id, _, _ in pairs], dtype=np.bool_)


def station_age_days(first_seen_ms: Int64, grid_ms: Sequence[int]) -> Float32:
    """台帳に載ってからの日数。`t` ごとに変わるので `(ポート, 基準時刻)` になる。

    観測開始より前から在るポートは負にならない（`first_seen_at` は台帳への登録時刻で、
    収集開始が起点）。**「古いポート」ではなく「いつから見ているか」を表す。**
    """
    grid = np.asarray(grid_ms, dtype=np.int64)
    days = (grid[None, :] - first_seen_ms[:, None]) / _MS_PER_DAY
    return np.asarray(days, dtype=np.float32)


def running_capacity(observations: Observations) -> Int32:
    """観測 1 行ごとの `max(bikes + docks)`（そのポートの、その時点までの累積最大）。

    **未来を覗かない**ように累積で取る。観測されていない行（`-1`）は数えず、
    その時点までに 1 度も観測が無ければ `-1` を残す。
    """
    result = np.full(observations.n_rows, NO_CODE, dtype=np.int32)
    for station in range(observations.n_stations):
        rows = observations.rows_of(station)
        if rows.stop == rows.start:
            continue
        bikes = observations.bikes[rows].astype(np.int32)
        docks = observations.docks[rows].astype(np.int32)
        total = np.where(bikes == MISSING, NO_CODE, bikes + docks)
        result[rows] = np.maximum.accumulate(total).astype(np.int32)
    return result


def declared_capacity_grid(facts: StationFacts, n_grid: int) -> Int32:
    """宣言値を `(ポート, 基準時刻)` に広げる。HELLO 用。"""
    return np.asarray(
        np.broadcast_to(facts.declared_capacity[:, None], (len(facts), n_grid)), dtype=np.int32
    )


def fill_ratio(bikes: Float64 | Int32, capacity: Int32) -> Float32:
    """`bikes / capacity`。容量が分からない・0 のときは NaN（**0 で埋めない**）。"""
    usable = capacity > 0
    ratio = np.where(usable, bikes / np.where(usable, capacity, 1), np.nan)
    return np.asarray(ratio, dtype=np.float32)


def gap(capacity: Int32, bikes: Int32, docks: Int32) -> Float32:
    """`capacity − bikes − docks`。**HELLO だけで意味を持つ**（§3.5 の予約の代理）。

    **負にもなる**。非負を仮定しない（開発プラン §6.3）。容量が無ければ NaN。
    """
    usable = capacity > 0
    return np.asarray(np.where(usable, capacity - bikes - docks, np.nan), dtype=np.float32)
