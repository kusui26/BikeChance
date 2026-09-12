"""ポートプロファイル（開発プラン §6.3 の「ポート履歴プロファイル」、W5 プラン §6.2）。

**「このポートの、この曜日種別の、この 15 分枠は、ふだんどうか」を数える。**

**これは B2（気候値）と同じ量である**（W5-01）。開発プラン §6.3 の `prof_p_bike` と
§7.1 の B2 は、**同じ `(ポート, 曜日種別, 15 分枠)` のセルを別の名前で呼んでいる**。
別々に作れば 2 つの実装が 2 つの数を出すので、**表を 1 つにして両方がそこから引く**。

**抽出した `features/` からは作らない**（W5-02）。1 セル 1 日あたりの行は**抽出後
1.62 行（中央 1）**しか無く、**全格子なら 3 点**（288 格子点 ÷ 96 枠）である
（W5 プラン §2.3d の実測）。気候値が痩せていたのは日数ではなく抽出のせいだった。

**28 日を待たない**（W5-03）。セルごとに `n_days` を持ち、**下限は読む側が決める**。
`capacity_est` が `capacity_days` を持つのと同じ作法である。

**数えるだけで、割らない。** 率（`n_bike_ok / n`）にすると 28 日ぶんを足せなくなるし、
**分母の取り方を先に決めてしまう**——休止していた時間を分母に入れるかどうかは読む側の
判断で、そのために `n_suspended` を別に数えてある。

**ここは純粋な部分だけを持つ。** Parquet を読むのも置くのも `jobs/build_profiles.py`。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from bikechance_ml.features import asof, exclude, flow, labels
from bikechance_ml.features.arrays import Bools, Int16, Int32, Int64
from bikechance_ml.features.calendar import DowType, day_type, dow_type
from bikechance_ml.features.constants import GRID_MINUTES, MISSING
from bikechance_ml.features.grid import Grid, build_grid

#: 1 日を 15 分で割った枠の数。**`baselines/climatology.py` の `SLOTS_PER_DAY` と同じ値**
#: でなければ、B2 のセルとプロファイルのセルが別のものになる（W5-01）。
SLOTS_PER_DAY: Final[int] = 24 * 60 // 15

#: 1 枠に入る 5 分格子点の数。**3**（15 ÷ 5）。`daily` の `n` の上限でもある。
GRID_POINTS_PER_SLOT: Final[int] = 15 // GRID_MINUTES

#: プロファイルの窓（日）。開発プラン §6.3 の「過去 28 日（前日まで）」。
#: **待つための数ではなく、忘れるための数である**（W5-03）。
PROFILE_DAYS: Final[int] = 28

#: 表の形の版。**列や意味を変えたら上げる**（`REFERENCE_SET` と同じ作法）。
PROFILE_SET: Final[str] = "p0"

#: 読む側が下限に使う既定（`baselines/climatology.py` の `MIN_CELL_DAYS` と同じ考え方）。
#: **ここでは切らない**——切ると転がし（`profile(D-1) + daily(D) - daily(D-28)`）で
#: 落としたセルの累計が戻らなくなる。
MIN_CELL_DAYS: Final[int] = 2

#: 出力ファイルの名前。パスは `grid.profile_path` が組み立てる。
DAILY_NAME: Final[str] = "daily"
PROFILE_NAME: Final[str] = "profile"

#: セルを決める鍵。
KEY_COLUMNS: Final[tuple[str, ...]] = ("system_id", "station_id", "dow_type", "slot15")

#: 足し合わせる列と型。**鍵以外はすべて和である**（だから転がせる）。
#:
#: `sum_rentals_60` / `sum_returns_60` は **60 分の移動窓の合計の和**である
#: （`features/flow.py` の `rentals_60` をそのまま足したもの）。「その 15 分枠で
#: 何台出たか」ではない——**`n` で割れば「この時間帯の 1 時間あたりの流量」**になり、
#: 開発プラン §6.3 の `prof_rentals_per_hour` がそれである。**名前で単位が分かるようにする。**
SUM_TYPES: Final[dict[str, pa.DataType]] = {
    # 1 日は最大 3、28 日でも 84。**窓を延ばすなら型を見直す**
    "n": pa.int16(),
    "n_suspended": pa.int16(),
    "n_bike_ok": pa.int16(),
    "n_dock_ok": pa.int16(),
    "sum_bikes": pa.int32(),
    "sum_bikes_sq": pa.int32(),
    "sum_rentals_60": pa.int32(),
    "sum_returns_60": pa.int32(),
}

SUM_COLUMNS: Final[tuple[str, ...]] = tuple(SUM_TYPES)


def _schema(*, with_days: bool) -> pa.Schema:
    fields = [
        pa.field("system_id", pa.string(), nullable=False),
        pa.field("station_id", pa.string(), nullable=False),
        # **その日は 1 種別しか無い**（`daily`）。`profile` には 3 種別が並ぶ
        pa.field("dow_type", pa.string(), nullable=False),
        # 15 分枠（0〜95）。**B2 の `slot15` と同じ切り方**
        pa.field("slot15", pa.int16(), nullable=False),
    ]
    if with_days:
        # **セルに寄与した日数。** 読む側の下限（`MIN_CELL_DAYS`）はこれで判定する
        fields.append(pa.field("n_days", pa.int16(), nullable=False))
    fields.extend(pa.field(name, SUM_TYPES[name], nullable=False) for name in SUM_COLUMNS)
    return pa.schema(fields)


#: `daily.parquet` の列（1 日ぶんの素の集計）。
DAILY_SCHEMA: Final[pa.Schema] = _schema(with_days=False)

#: `profile.parquet` の列（直近 `PROFILE_DAYS` 日の累計）。
PROFILE_SCHEMA: Final[pa.Schema] = _schema(with_days=True)

#: 読み込む余白（時間）。as-of の上限（10 分）と流量の窓（60 分）を覆う最小の整数時間に
#: 1 時間の余裕を足したもの。**ラベルも同時刻履歴も要らない**ので、学習の 25 時間は要らない。
LOOKBACK_HOURS: Final[int] = 2


class SchemaMismatchError(ValueError):
    """読んだ表の列が契約と違う。**足りない列を捏造しない**（W3 プラン §12 の 97）。"""


@dataclass(frozen=True)
class DayInputs:
    """1 日ぶんを数えるのに要るもの。**参照スナップショットも天気も要らない。**

    ポートの並びは**その日の観測から作る**（`station_keys`）。参照スナップショットを
    要求すると、プロファイルが `build_reference` の後でしか作れなくなる——**数えるのに
    要らないものを前提にしない。**
    """

    day: date
    table: pa.Table
    holidays: frozenset[date]


def station_keys(table: pa.Table) -> tuple[tuple[str, str], ...]:
    """その日に現れた `(system_id, station_id)` を昇順に並べる。

    **台帳を持ち込まない。** 観測が無いポートはどのみち 1 点も数えられないので、
    表に出す意味が無い（`n = 0` の行を作らない）。
    """
    if table.num_rows == 0:
        return ()
    columns = ["system_id", "station_id"]
    seen = (
        table.select(columns)
        .group_by(columns)
        .aggregate([])
        .sort_by([(one, "ascending") for one in columns])
    )
    return tuple(
        zip(
            seen.column("system_id").to_pylist(),
            seen.column("station_id").to_pylist(),
            strict=True,
        )
    )


def day_dow_type(day: date, holidays: frozenset[date]) -> DowType:
    """その JST 暦日の曜日種別。**`features/build.py` と同じ関数を通る。**"""
    return dow_type(day_type(day, holidays))


def source_day(day: date) -> date:
    """基準時刻が `day` のとき、**どの日の版を読むか**。答えは**前日**。

    開発プラン §6.2 のリーク防止：「ポート単位の履歴プロファイルは、`t` の**前日
    23:59 まで**のデータで計算した値のみ使う」。`profile(D)` は **D 当日まで**を
    含むので（`build_reference` の `reference(D)` と同じ作法）、`D + 1` の基準時刻が
    読んでよいのは `profile(D)` である。

    **1 行の関数を置くのは、これを 2 か所で書かせないため。** 学習（`build_features`）も
    推論（`jobs/infer.py`）も同じ規則で読む——**「学習は前日、推論は最新」にすると、
    そこが新しい train/serve skew になる**（`read_reference` と同じ理由）。
    """
    return day - timedelta(days=1)


def build_day(inputs: DayInputs) -> pa.Table:
    """1 日ぶんの素の集計（`daily.parquet`）。

    **全格子で数える。** 1 ポート 1 日あたり 288 点、1 枠につき 3 点である（W5-02）。
    """
    keys = station_keys(inputs.table)
    if not keys:
        return DAILY_SCHEMA.empty_table()
    grid = build_grid(inputs.day, lookback_hours=LOOKBACK_HOURS, lookahead_hours=0)
    counted = _count(inputs.table, keys, grid)
    return _to_table(keys, counted, day_dow_type(inputs.day, inputs.holidays))


@dataclass(frozen=True)
class Counted:
    """セルごとの和。`port` は `keys` の添字、`slot` は 0〜95。"""

    slot: Int16
    port: Int32
    values: dict[str, Int64]


def _count(table: pa.Table, keys: Sequence[tuple[str, str]], grid: Grid) -> Counted:
    """格子点の状態をセルに畳む。**割らない。数えるだけ。**"""
    observations = asof.to_observations(table, keys)
    row = asof.as_of_feature(observations, grid.times_ms)[:, grid.day_slice()]
    bikes = asof.gather(observations.bikes, row, MISSING)
    docks = asof.gather(observations.docks, row, MISSING)
    flags = asof.gather(observations.flags, row, MISSING)
    flows = flow.compute_flow(observations)
    return _accumulate(
        usable=usable_points(
            keys=keys,
            feature_row=row,
            observed_at_ms=asof.gather(observations.observed_at_ms, row, 0),
            grid_ms=np.asarray(grid.day_times_ms(), dtype=np.int64)[None, :],
            bikes=bikes,
            docks=docks,
        ),
        bikes=bikes,
        docks=docks,
        flags=flags,
        rentals=asof.gather(flows.rentals, row, 0),
        returns=asof.gather(flows.returns, row, 0),
        n_ports=len(keys),
    )


def usable_points(
    *,
    keys: Sequence[tuple[str, str]],
    feature_row: Int32,
    observed_at_ms: Int64,
    grid_ms: Int64,
    bikes: Int16,
    docks: Int16,
) -> Bools:
    """数えてよい格子点。**`features/exclude.py` の部品をそのまま使う**（契約 6）。

    **`exclude_at_base` はそのまま使えない**——あれは基準時刻 `t` の側の除外で、
    **休止も落とす**。プロファイルには `t` が無く、休止した時間を分母に入れるかは
    読む側の判断なので、ここでは落とさずに `n_suspended` として数える。

    残りの 4 つ（実在しないポート・as-of が無い・観測が古すぎる・観測されていない）は
    **`exclude_at_base` と同じ判定**であり、`tests/test_features_profile.py` が
    「休止を除けば両者は一致する」ことを機械で固定している。
    """
    phantom = np.broadcast_to(exclude.phantom_mask(keys)[:, None], feature_row.shape)
    fresh = ~exclude.too_old(grid_ms, observed_at_ms, feature_row)
    return np.asarray(
        ~phantom & (feature_row != asof.NO_ROW) & (bikes != MISSING) & (docks != MISSING) & fresh,
        dtype=np.bool_,
    )


def _accumulate(
    *,
    usable: Bools,
    bikes: Int16,
    docks: Int16,
    flags: Int16,
    rentals: Int32,
    returns: Int32,
    n_ports: int,
) -> Counted:
    """`(ポート × 格子点)` の行列を `(ポート × 枠)` に畳む。"""
    slot_of = np.arange(usable.shape[1]) // GRID_POINTS_PER_SLOT
    cell = np.broadcast_to(np.arange(n_ports)[:, None], usable.shape) * SLOTS_PER_DAY + slot_of
    taken = cell[usable]
    size = n_ports * SLOTS_PER_DAY
    summed = {
        name: np.bincount(taken, weights=values, minlength=size).astype(np.int64)
        for name, values in _terms(usable, bikes, docks, flags, rentals, returns).items()
    }
    index = np.flatnonzero(summed["n"] > 0)
    return Counted(
        slot=np.asarray(index % SLOTS_PER_DAY, dtype=np.int16),
        port=np.asarray(index // SLOTS_PER_DAY, dtype=np.int32),
        values={name: values[index] for name, values in summed.items()},
    )


def _terms(
    usable: Bools, bikes: Int16, docks: Int16, flags: Int16, rentals: Int32, returns: Int32
) -> dict[str, Int64]:
    """格子点 1 つが各列に足す量。**`labels.py` と同じ定義でラベルを数える。**

    `n_bike_ok` は `y_bike(t)` そのもの——「その時刻に借りられたか」で、B2 が当てる
    ラベルと同じ式である（W5-01）。休止していれば 0 になるので、**`n_suspended` を
    分母から引けば「開いていたときに借りられた割合」**が出る。
    """
    taken = bikes[usable].astype(np.int64)
    return {
        "n": np.ones(taken.size, dtype=np.int64),
        "n_suspended": exclude.suspended(flags)[usable].astype(np.int64),
        "n_bike_ok": labels.y_bike(bikes, flags)[usable].astype(np.int64),
        "n_dock_ok": labels.y_dock(docks, flags)[usable].astype(np.int64),
        "sum_bikes": taken,
        "sum_bikes_sq": np.square(taken),
        "sum_rentals_60": rentals[usable].astype(np.int64),
        "sum_returns_60": returns[usable].astype(np.int64),
    }


def _to_table(keys: Sequence[tuple[str, str]], counted: Counted, kind: DowType) -> pa.Table:
    """数えた結果を `daily.parquet` の形にする。**鍵の昇順で書く。**

    ポートの名前は `take` で引く（2 百万行ぶんの Python の繰り返しを作らない）。
    """
    port = pa.array(counted.port)
    return pa.table(
        {
            "system_id": pa.array([one[0] for one in keys]).take(port),
            "station_id": pa.array([one[1] for one in keys]).take(port),
            "dow_type": pa.array([kind] * len(counted.slot), type=pa.string()),
            "slot15": counted.slot,
            **{name: counted.values[name] for name in SUM_COLUMNS},
        },
        schema=DAILY_SCHEMA,
    )


# ── 転がす（純粋）──────────────────────────────────────────────
def roll(previous: pa.Table | None, today: pa.Table, expired: pa.Table | None) -> pa.Table:
    """`profile(D) = profile(D-1) + daily(D) - daily(D-28)`。

    **28 日ぶんを毎日読み直さない**（1 回 150〜400 MB になる）。`build_reference` の
    `capacity_daily_max` と同じ持ち回りで、読むのは 3 ファイルだけである（W5-05）。

    **無い日は飛ばす。** `previous` が無ければ当日だけ、`expired` が無ければ引かない。
    **足りないことは `n_days` に出る**ので、黙って埋めない。
    """
    require_schema(today, DAILY_SCHEMA)
    parts = [_as_profile(today, +1)]
    if previous is not None:
        require_schema(previous, PROFILE_SCHEMA)
        parts.insert(0, previous)
    if expired is not None:
        require_schema(expired, DAILY_SCHEMA)
        parts.append(_as_profile(expired, -1))
    return _fold(parts)


def sum_dailies(dailies: Sequence[pa.Table]) -> pa.Table:
    """`daily` を素直に足し合わせる。**転がしの答え合わせ**（検算）に使う。

    転がし（`roll`）と結果が食い違えば、どこかで足し引きが崩れている。
    **本番では使わない**（28 ファイルを読むことになる）。
    """
    for one in dailies:
        require_schema(one, DAILY_SCHEMA)
    return _fold([_as_profile(one, +1) for one in dailies])


def _as_profile(daily: pa.Table, sign: int) -> pa.Table:
    """`daily` を `profile` の形にする。`sign = -1` なら**すべての和を反転**する。"""
    scale = pa.scalar(sign, type=pa.int16())
    columns: dict[str, pa.ChunkedArray | pa.Array] = {
        name: daily.column(name) for name in KEY_COLUMNS
    }
    columns["n_days"] = pa.array(np.full(daily.num_rows, sign, dtype=np.int16))
    columns.update(
        {name: pc.multiply(daily.column(name), scale.cast(SUM_TYPES[name])) for name in SUM_COLUMNS}
    )
    return pa.table(columns, schema=PROFILE_SCHEMA)


def _fold(parts: Sequence[pa.Table]) -> pa.Table:
    """鍵でまとめて足す。**`n_days` が 0 以下になったセルは消す。**

    引いた日にしか寄与が無かったセルは、引いたあと空になる。**0 の行を残すと表が
    単調に太り、「1 度も観測していない」と「28 日前に観測した」が区別できなくなる。**
    """
    joined = pa.concat_tables(parts)
    if joined.num_rows == 0:
        return PROFILE_SCHEMA.empty_table()
    totals = ("n_days", *SUM_COLUMNS)
    grouped = joined.group_by(list(KEY_COLUMNS)).aggregate([(one, "sum") for one in totals])
    alive = grouped.filter(pc.greater(grouped.column("n_days_sum"), 0))
    columns: dict[str, pa.ChunkedArray | pa.Array] = {
        name: alive.column(name) for name in KEY_COLUMNS
    }
    columns.update(
        {one: alive.column(f"{one}_sum").cast(PROFILE_SCHEMA.field(one).type) for one in totals}
    )
    built = pa.table(columns, schema=PROFILE_SCHEMA)
    return built.sort_by([(one, "ascending") for one in KEY_COLUMNS])


def require_schema(table: pa.Table, schema: pa.Schema) -> None:
    """**ファイル自身の列**が契約どおりか（W3 プラン §12 の 97 と同じ理由）。"""
    if table.schema.names != schema.names:
        raise SchemaMismatchError(f"プロファイルの列が違う: {table.schema.names}")


# ── 読む（純粋）────────────────────────────────────────────────
def summarize(profile: pa.Table, *, min_days: int = MIN_CELL_DAYS) -> dict[str, object]:
    """報告用の要約。**読む側が実際に使えるセルの数**を中心に出す。"""
    if profile.num_rows == 0:
        return {"cells": 0, "usable_cells": 0, "ports": 0}
    days = profile.column("n_days")
    usable = pc.greater_equal(days, min_days)
    return {
        "cells": profile.num_rows,
        "usable_cells": int(pc.sum(usable).as_py() or 0),
        "ports": _distinct_ports(profile),
        "min_days": min_days,
        "days_max": int(pc.max(days).as_py() or 0),
        "by_dow_type": _by_dow_type(profile, usable),
        "suspended_share": _suspended_share(profile),
    }


def _distinct_ports(profile: pa.Table) -> int:
    columns = ["system_id", "station_id"]
    return int(profile.select(columns).group_by(columns).aggregate([]).num_rows)


def _by_dow_type(profile: pa.Table, usable: pa.ChunkedArray) -> dict[str, dict[str, int]]:
    """曜日種別ごとのセル数。**週末のセルが 0 のままかどうかがここに出る**（§2.3c）。"""
    counted = (
        profile.append_column("usable", usable)
        .group_by(["dow_type"])
        .aggregate([("usable", "sum"), ("n_days", "max"), ("slot15", "count")])
    )
    return {
        str(kind): {
            "cells": int(cells),
            "usable": int(ok or 0),
            "days_max": int(days or 0),
        }
        for kind, cells, ok, days in zip(
            counted.column("dow_type").to_pylist(),
            counted.column("slot15_count").to_pylist(),
            counted.column("usable_sum").to_pylist(),
            counted.column("n_days_max").to_pylist(),
            strict=True,
        )
    }


def _suspended_share(profile: pa.Table) -> float:
    """全格子点のうち休止していた割合。**分母をどう取るかの判断材料**（W5 プラン §6.2）。"""
    total = int(pc.sum(profile.column("n")).as_py() or 0)
    if total == 0:
        return 0.0
    return round(int(pc.sum(profile.column("n_suspended")).as_py() or 0) / total, 6)
