"""参照データの行（W3 プラン §9.5）。

**numpy を import しない。** `io/supabase.py` がこの型を返し、numpy の配列に直すのは
`static.to_facts` / `neighbors.to_links` の仕事にする。こうしておけば、本番の
`/ml/infer` が `io/` を読み込んでも numpy を引きずらない（numpy は dev 依存。W3-20）。

値の意味：`None` は「無い」。`pref_code` / `muni_code` はドコモでは常に無く、
`region_id` は HELLO では常に無い。
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class StationGeoRow:
    """`stations` の 1 行。"""

    station_id: str
    first_seen_at: datetime
    pref_code: int | None
    muni_code: int | None


@dataclass(frozen=True)
class StationAttributeRow:
    """`station_attributes` の現行行（`valid_to is null`）。

    属性を持たないポートがある（`status` にしか現れない。実測でドコモに数件）ので、
    **台帳にあって属性が無い**場合を呼ぶ側で扱えるようにしておく。
    """

    station_id: str
    lat: float | None
    lon: float | None
    capacity: int | None
    is_charging_station: bool | None
    region_id: int | None


@dataclass(frozen=True)
class NeighborRow:
    """`station_neighbors` の 1 行（片方向）。

    **近傍はシステムを跨ぐ。** HELLO の起点からドコモのポートを指す行があるので、
    相手のシステムまで持たないと状態を引けない（W3 プラン §4.3 の 22）。
    """

    station_id: str
    nb_system_id: str
    nb_station_id: str
    distance_m: int
    same_system: bool


@dataclass(frozen=True)
class SystemReference:
    """1 システムぶんの参照データ。**台帳の順がポートの位置を決める。**"""

    system_id: str
    geo: tuple[StationGeoRow, ...]
    attributes: tuple[StationAttributeRow, ...]
    neighbors: tuple[NeighborRow, ...]
