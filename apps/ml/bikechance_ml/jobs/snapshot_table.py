"""配列形式のスナップショット → 学習用の長形式の表（W2 プラン §5.6、§9.2）。

**このファイルに副作用を置かない。** 読み書きは `io/supabase.py` にあり、ここは
データ変換だけを持つ。純粋なので境界値をフィクスチャで全部通せる（CLAUDE.md §3）。

なぜ長形式にするか：学習はポート単位の時系列を読む。配列形式は 1 フィード更新 1 行で
**書き込み**に最適化されていて、特定ポートの時系列を得るには全行の展開が要る
（開発プラン §6.7）。`station_id, observed_at` 順に並べた列指向にしておけば、
必要なポートの必要な期間だけを読める。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

#: 「登録済みだが、このフィードに現れなかった」を表す値（W1 プラン §11.1）。0 と区別する。
MISSING: Final[int] = -1

#: 圧縮。zstd は gzip より小さく、読み出しも速い（開発プラン §5.6）。
COMPRESSION: Final[str] = "zstd"

#: 列と型の契約（W2 プラン §9.2）。**`idx` は書かない**。生 JSON から作り直すと
#: 割り当てが変わり得るので、再構築で不変な `station_id` だけを外に出す（W2-10）。
SCHEMA: Final[pa.Schema] = pa.schema(
    [
        pa.field("system_id", pa.string(), nullable=False),
        pa.field("station_id", pa.string(), nullable=False),
        pa.field("observed_at", pa.timestamp("ms", tz="UTC"), nullable=False),
        # **as-of 結合はこの列で切る**（開発プラン §6.2、W3 プラン §9.1）。本番の推論が
        # 使えるのは「取り込み済み」のものだけなので、`observed_at` で切ると、本番では
        # まだ届いていないスナップショットで学習することになる（train/serve skew）。
        # HELLO は公開遅延の中央値が 67 秒・最大 226 秒で、5 分グリッド点の約 24% が
        # これに当たる
        pa.field("fetched_at", pa.timestamp("ms", tz="UTC"), nullable=False),
        pa.field("bikes", pa.int16(), nullable=False),
        pa.field("docks", pa.int16(), nullable=False),
        pa.field("flags", pa.int16(), nullable=False),
        pa.field("reported_age_s", pa.int16(), nullable=False),
    ]
)


class InconsistentLedgerError(ValueError):
    """台帳の `idx` が 0 起点で密でない。配列の位置とポートの対応が崩れる。"""


class InconsistentSnapshotError(ValueError):
    """スナップショットの配列の長さが揃っていない、または台帳より長い。"""


@dataclass(frozen=True)
class StationRow:
    """台帳の 1 行。位置の割り当てに使う 2 列だけを持つ。"""

    station_id: str
    idx: int


@dataclass(frozen=True)
class Snapshot:
    """1 フィード更新ぶん。4 本の配列は同じ長さで、`idx` の順に並ぶ。

    `observed_at` はフィードが名乗る観測時刻、`fetched_at` は収集器が取り込みを
    終えた時刻。**この 2 つは別物で、学習の as-of は後者で切る**（`SCHEMA` の注記）。
    """

    observed_at: datetime
    fetched_at: datetime
    bikes: Sequence[int]
    docks: Sequence[int]
    flags: Sequence[int]
    reported_age_s: Sequence[int]

    def length(self) -> int:
        return len(self.bikes)


def hour_window(at: datetime) -> tuple[datetime, datetime]:
    """`at` の**直前の 1 時間**を半開区間 `[開始, 終わり)` で返す（UTC）。

    毎時 7 分に動かすので、`at` が 05:07 なら 04:00〜05:00 を畳む。境界の観測が
    どちらに入るかで迷わないよう、区間は必ず半開で扱う（W2 プラン §4 の 9）。
    """
    end = at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return end - timedelta(hours=1), end


def parquet_path(system_id: str, hour_start: datetime) -> str:
    """バケット内のパス（§9.2）。日時は **UTC**。同じ時間帯は必ず同じパスに写像する。"""
    at = hour_start.astimezone(UTC)
    return f"{system_id}/date={at:%Y-%m-%d}/hour={at:%H}/part.parquet"


def station_ids_by_idx(rows: Sequence[StationRow]) -> tuple[str, ...]:
    """`idx` 順に並べた `station_id`。**0 起点で密**でなければ例外にする。

    「配列の i 番目が idx=i のポート」という前提が崩れると、値が別のポートに付く。
    静かに間違うくらいなら止める。
    """
    ordered = sorted(rows, key=lambda row: row.idx)
    for position, row in enumerate(ordered):
        if row.idx != position:
            raise InconsistentLedgerError(f"idx が密でない: {position} 番目が {row.idx}")
    return tuple(row.station_id for row in ordered)


def _check(snapshot: Snapshot, n_stations: int) -> None:
    at = f"{snapshot.observed_at:%Y-%m-%dT%H:%M:%SZ}"
    length = snapshot.length()
    lengths = (length, len(snapshot.docks), len(snapshot.flags), len(snapshot.reported_age_s))
    if len(set(lengths)) != 1:
        raise InconsistentSnapshotError(f"{at}: 配列の長さが揃っていない")
    if length > n_stations:
        # 台帳から行は消えない（W1 プラン §11.1）。長ければ台帳の読み違い
        raise InconsistentSnapshotError(f"{at}: 配列長 {length} が台帳 {n_stations} を超えた")


def to_table(
    system_id: str,
    station_ids: Sequence[str],
    snapshots: Sequence[Snapshot],
) -> pa.Table:
    """スナップショット群を長形式の表にする。**`station_id, observed_at` 順に並べる。**

    **配列長より外側の `idx` は行にしない**（§9.2）。当時まだ台帳に無かったポートで、
    行を作ると「登録済みだが現れなかった」（`-1`）と区別できなくなる。
    """
    columns = _accumulate(station_ids, snapshots)
    table = pa.table(
        {
            "system_id": pa.array([system_id] * len(columns.station_id), type=pa.string()),
            "station_id": pa.array(columns.station_id, type=pa.string()),
            "observed_at": pa.array(columns.observed_at, type=pa.timestamp("ms", tz="UTC")),
            "fetched_at": pa.array(columns.fetched_at, type=pa.timestamp("ms", tz="UTC")),
            "bikes": pa.array(columns.bikes, type=pa.int16()),
            "docks": pa.array(columns.docks, type=pa.int16()),
            "flags": pa.array(columns.flags, type=pa.int16()),
            "reported_age_s": pa.array(columns.reported_age_s, type=pa.int16()),
        },
        schema=SCHEMA,
    )
    # 学習はポート単位の時系列を読む。ここで並べておくと必要な範囲だけを読める（§9.2）
    return table.sort_by([("station_id", "ascending"), ("observed_at", "ascending")])


@dataclass
class _Columns:
    """組み立て途中の列。表を作るまでの間だけ使う。"""

    station_id: list[str]
    observed_at: list[datetime]
    fetched_at: list[datetime]
    bikes: list[int]
    docks: list[int]
    flags: list[int]
    reported_age_s: list[int]


def _accumulate(station_ids: Sequence[str], snapshots: Sequence[Snapshot]) -> _Columns:
    columns = _Columns([], [], [], [], [], [], [])
    for snapshot in sorted(snapshots, key=lambda one: one.observed_at):
        _check(snapshot, len(station_ids))
        length = snapshot.length()
        columns.station_id.extend(station_ids[:length])
        columns.observed_at.extend([snapshot.observed_at] * length)
        columns.fetched_at.extend([snapshot.fetched_at] * length)
        columns.bikes.extend(snapshot.bikes)
        columns.docks.extend(snapshot.docks)
        columns.flags.extend(snapshot.flags)
        columns.reported_age_s.extend(snapshot.reported_age_s)
    return columns


def count_missing(table: pa.Table) -> int:
    """`bikes` が `-1`（観測されなかった）の行数。収集品質の目安として記録する。"""
    return int(pc.sum(pc.equal(table.column("bikes"), MISSING)).as_py() or 0)


def to_parquet_bytes(table: pa.Table) -> bytes:
    """表を Parquet のバイト列にする。同じ表からは同じ行の集合が読める。"""
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression=COMPRESSION)
    return bytes(sink.getvalue())
