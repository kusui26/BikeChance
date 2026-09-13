"""学習サンプルを、ベースラインと評価が使う形に開く（W3 プラン §9.3）。

`features/date=…/part.parquet` は 69 列あるが、ベースラインが使うのはごく一部である
（B0 は台数だけ、B1 は台数のバケツと水平だけ）。**必要な列だけを numpy の配列にして
持ち回る。** ここを通すことで、ベースラインの実装は pyarrow を知らなくてよくなる。

**文字列は読み込みのときに番号へ直す。** `system_id` も `station_id` も `dow_type` も、
2 百万行ぶんの文字列として持ち回ると、比較のたびに走査が要る。ここで一度だけ
`np.unique` を通し、以降は整数の索引で扱う。

**ポートは `(system_id, station_id)` の組で数える。** `station_id` はシステムを跨いで
衝突する（実測 2,608 件）。番号を `station_id` だけで振ると、HELLO とドコモの別々の
ポートが同じ気候値のセルに入る。

**ターゲットは 2 つあり、条件に使う台数も 2 つある。** `y_bike` は `bikes`、`y_dock` は
`docks` で条件づける。W3 プラン §9.6 は B1 を `bikes_bucket(t)` と書いているが、
これは貸出側の式で、返却側は `docks` で条件づけるのが対応する形になる（§12 の 100）。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

import numpy as np
import pyarrow as pa

from bikechance_ml.features.arrays import Bools, Float32, Int8, Int16, Int32, Strings
from bikechance_ml.features.calendar import DOW_TYPE_ORDER
from bikechance_ml.features.grid import JST_OFFSET_MS

_MS_PER_DAY: Final[int] = 86_400_000


@dataclass(frozen=True)
class Target:
    """当てる対象と、B1 が条件に使う台数の列。"""

    name: str
    label: str
    counts: str


#: 貸出（借りられるか）と返却（返せるか）。
TARGETS: Final[tuple[Target, ...]] = (
    Target("bike", "y_bike", "bikes"),
    Target("dock", "y_dock", "docks"),
)

#: サンプルを開くのに要る列。**これ以外は読まない**（1 日 34.8 MB のうち一部で済む）。
NEEDED_COLUMNS: Final[tuple[str, ...]] = (
    "system_id",
    "station_id",
    "t",
    "h_min",
    "y_bike",
    "y_dock",
    "weight",
    "bikes",
    "docks",
    "minute_of_day",
    "target_dow_type",
)

#: 台数のバケツ（開発プラン §7.1、W3 プラン §9.6）。**上限は含む。**
#: 総合の Brier は「3 台以上ある」自明な行に支配されるので、必ずこれで割って見る（W3-16）。
BUCKET_EDGES: Final[tuple[int, ...]] = (0, 1, 2, 5, 10)
BUCKET_LABELS: Final[tuple[str, ...]] = ("0", "1", "2", "3-5", "6-10", "11+")


def bucket_index(counts: Int16) -> Int8:
    """台数をバケツの番号（0〜5）にする。"""
    return np.asarray(np.searchsorted(BUCKET_EDGES, counts, side="left"), dtype=np.int8)


@dataclass(frozen=True)
class Samples:
    """1 つ以上の日ぶんのサンプル。列は行の順にそろっている。

    `systems` と `n_ports` は**元の全体**のもので、行を絞っても変わらない。
    参照表の大きさが学習期間と検証期間で違うと、番号が指す先がずれる。
    """

    systems: tuple[str, ...]
    n_ports: int
    system: Int8
    port: Int32
    day: Int32
    h_min: Int16
    minute_of_day: Int16
    dow_type: Int8
    weight: Float32
    labels: dict[str, Int8]
    counts: dict[str, Int16]

    def __len__(self) -> int:
        return len(self.h_min)

    def y(self, target: Target) -> Int8:
        return self.labels[target.label]

    def count_of(self, target: Target) -> Int16:
        return self.counts[target.counts]

    def take(self, keep: Bools) -> "Samples":
        """真偽の並びで行を絞る。**列の対応を崩さない**ための唯一の入り口。"""
        return Samples(
            systems=self.systems,
            n_ports=self.n_ports,
            system=self.system[keep],
            port=self.port[keep],
            day=self.day[keep],
            h_min=self.h_min[keep],
            minute_of_day=self.minute_of_day[keep],
            dow_type=self.dow_type[keep],
            weight=self.weight[keep],
            labels={name: values[keep] for name, values in self.labels.items()},
            counts={name: values[keep] for name, values in self.counts.items()},
        )


def to_samples(table: pa.Table) -> Samples:
    """Parquet の表を開く。`t` は **JST の暦日**（`day`）に直す。"""
    system_id = _strings(table, "system_id")
    systems, system = np.unique(system_id, return_inverse=True)
    ports, port = np.unique(
        _port_keys(system_id, _strings(table, "station_id")), return_inverse=True
    )
    return Samples(
        systems=tuple(str(one) for one in systems),
        n_ports=len(ports),
        system=np.asarray(system, dtype=np.int8),
        port=np.asarray(port, dtype=np.int32),
        day=jst_ordinal(table.column("t")),
        h_min=_int16(table, "h_min"),
        minute_of_day=_int16(table, "minute_of_day"),
        dow_type=_dow_type(_strings(table, "target_dow_type")),
        weight=np.asarray(
            table.column("weight").combine_chunks().to_numpy(zero_copy_only=False),
            dtype=np.float32,
        ),
        labels={target.label: _int8(table, target.label) for target in TARGETS},
        counts={target.counts: _int16(table, target.counts) for target in TARGETS},
    )


def _port_keys(system_id: Strings, station_id: Strings) -> Strings:
    """**`(system_id, station_id)` の組**を 1 本の文字列にする（衝突を避けるため）。"""
    return np.asarray(np.char.add(np.char.add(system_id, "/"), station_id), dtype=np.str_)


def _dow_type(values: Strings) -> Int8:
    """曜日種別を **`DOW_TYPE_ORDER` の番号**にする。**知らない値は例外にする。**

    **`DOW_TYPES` の番号ではない**（`weekday` は 2）。並びの正は
    `features/calendar.py` の `DOW_TYPE_ORDER` 1 つで、**配信側
    （`models/predictor.py`）も同じ定数を読む**——以前は両方が別々に `sorted()` を
    呼んでいて、**規約が 2 か所に無名で在った**（W5 プラン §12 の 132）。

    `DOW_TYPE_ORDER` は昇順なので、`searchsorted` にそのまま渡せる。
    """
    order = np.array(DOW_TYPE_ORDER, dtype=np.str_)
    index = np.searchsorted(order, values)
    if not bool(np.all(order[np.clip(index, 0, len(order) - 1)] == values)):
        raise ValueError("dow_type に想定外の値が入っています")
    return np.asarray(index, dtype=np.int8)


def jst_ordinal(column: pa.ChunkedArray) -> Int32:
    """基準時刻を **JST の暦日**の通し番号（`date.toordinal()`）にする。

    分割は日単位で、日は JST で切る（出力のパスと同じ）。UTC で切ると 9 時間ずれる。
    """
    epoch_ms = np.asarray(
        column.cast(pa.int64()).combine_chunks().to_numpy(zero_copy_only=False), dtype=np.int64
    )
    jst_days = (epoch_ms + JST_OFFSET_MS) // _MS_PER_DAY
    return np.asarray(jst_days + date(1970, 1, 1).toordinal(), dtype=np.int32)


def days_in(samples: Samples) -> tuple[date, ...]:
    """含まれている JST の暦日（昇順）。"""
    return tuple(date.fromordinal(int(one)) for one in np.unique(samples.day))


def _strings(table: pa.Table, name: str) -> Strings:
    return np.array(table.column(name).to_pylist(), dtype=np.str_)


def _int8(table: pa.Table, name: str) -> Int8:
    return np.asarray(
        table.column(name).combine_chunks().to_numpy(zero_copy_only=False), dtype=np.int8
    )


def _int16(table: pa.Table, name: str) -> Int16:
    return np.asarray(
        table.column(name).combine_chunks().to_numpy(zero_copy_only=False), dtype=np.int16
    )


def concat(tables: Sequence[pa.Table], columns: Sequence[str] | None = NEEDED_COLUMNS) -> pa.Table:
    """日ごとのファイルを 1 つにする。**既定では `NEEDED_COLUMNS` に絞る。**

    `columns` を `None` にすると全列を残す（LightGBM は 62 列を使う）。
    """
    if columns is None:
        return pa.concat_tables(list(tables))
    return pa.concat_tables([table.select(list(columns)) for table in tables])
