"""特徴量の経路 B（W6 の PR G、W6-20）：**新しい実装は、前の実装と 1 バイトも違わない**（契約 40）。

前の実装は `tests/legacy_feature_path.py` に写してある。ここでは乱数で作った入力を両方に
通し、**出力をバイト列で比べる**（NaN の位置・型・チャンクの分かれ方の違いも拾う）。

入力に混ぜるもの：欠損（`-1`）・負の値・`int16` の両端・重複した ID・空・日またぎ（JST の
0 時）・時刻の同点・マイクロ秒（ミリ秒への切り捨て）・チャンクの分かれた表・台帳に無い
ポート・同じ鍵の 2 度目。**乱数の種は固定**で、落ちた入力は `case` から再現できる。

**届かない入力は混ぜない**：スナップショットの配列の要素は整数だけである（`bool` も
`None` も、読み込みの型検査 `json_shape.as_int_list` が弾く。①はその検査そのものなので、
そこだけは何でも混ぜる）。
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from functools import partial
from typing import Final

import numpy as np
import pyarrow as pa
import pytest

from bikechance_ml.features.arrays import Float32, Int16, Int32, Int64
from bikechance_ml.features.asof import Observations, to_observations
from bikechance_ml.features.constants import MISSING
from bikechance_ml.features.flow import Flow, compute_flow
from bikechance_ml.jobs.snapshot_table import (
    SCHEMA,
    InconsistentSnapshotError,
    Snapshot,
    to_parquet_bytes,
    to_table,
)
from bikechance_ml.json_shape import ShapeError, as_int_list
from tests import legacy_feature_path as legacy

SEED: Final[int] = 20261003
#: 1 つの検査で試す入力の数。**1 件 1 ミリ秒前後**なので、全体で数秒に収まる
CASES: Final[int] = 400

#: JST の 0 時の 5 分前（UTC 14:55）。ここから 10 分の幅で時刻を引くと、日をまたぐ
NEAR_MIDNIGHT: Final[datetime] = datetime(2026, 10, 2, 14, 55, tzinfo=UTC)
NEAR_MIDNIGHT_MS: Final[int] = int(NEAR_MIDNIGHT.timestamp() * 1000)
SPREAD_S: Final[int] = 600

INT16_MIN: Final[int] = -32768
INT16_MAX: Final[int] = 32767

SYSTEMS: Final[tuple[str, ...]] = ("hellocycling", "docomo-cycle", "unknown-system")
#: 重複を起こすために、ID の候補はわざと少なくする。2 バイト文字も混ぜる
STATION_POOL: Final[tuple[str, ...]] = ("1", "2", "10", "a", "駅", "駅2", "", "z9")

#: 流量の窓（分）。**0・負・1 日・既定の 60 を含む**（負の窓でも前と同じ答え）
WINDOWS_MIN: Final[tuple[int, ...]] = (0, 1, 5, 60, 180, 1440, -5)


def _rng(case: int) -> np.random.Generator:
    return np.random.default_rng([SEED, case])


# ── 結果の比べ方 ───────────────────────────────────────────────
type Outcome = tuple[str, object]


def _outcome[T](work: Callable[[], T], digest: Callable[[T], object]) -> Outcome:
    """成功なら `("ok", 中身)`、失敗なら `("error", 例外の種類)`。

    **例外の文言は比べない**（`ArrowInvalid` は呼び方で文言が変わる）。文言まで同じで
    あるべき自前の例外は、呼ぶ側で別に確かめる。
    """
    try:
        return "ok", digest(work())
    except Exception as cause:
        return "error", (type(cause).__name__, _own_message(cause))


def _own_message(cause: Exception) -> str:
    """自前の例外だけ文言を比べる（**どの時刻のどの不整合で止まったか**まで前と同じ）。"""
    own = (InconsistentSnapshotError, ShapeError)
    return str(cause) if isinstance(cause, own) else ""


def _bytes_of(array: Int16 | Int32 | Int64 | Float32) -> tuple[str, bytes]:
    """配列の型とバイト列。**NaN も、その中身のビットまで**比べる。"""
    return str(array.dtype), np.ascontiguousarray(array).tobytes()


def _ipc(table: pa.Table) -> bytes:
    """表のバイト列（Arrow IPC）。**チャンクの分かれ方も含めて**比べる。"""
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return bytes(sink.getvalue())


# ── ① 整数配列の型検査 ─────────────────────────────────────────
class Small(IntEnum):
    """`int` の派生型。**速い道に入らず、前と同じく通る**（値も型もそのまま）。"""

    ONE = 1


#: 混ぜる要素。整数でないもの（`bool`・`None`・小数・文字列・入れ子）と、整数の派生型
ODD_ITEMS: Final[tuple[object, ...]] = (True, False, None, 1.0, 2.5, "3", [1], Small.ONE)


def _random_item(rng: np.random.Generator) -> object:
    if rng.random() < 0.1:
        return ODD_ITEMS[int(rng.integers(0, len(ODD_ITEMS)))]
    # 64 ビットを超える整数も混ぜる（JSON の整数に上限は無い）
    return int(rng.choice([-1, 0, 7, INT16_MIN, INT16_MAX, 2**70, -(2**70)]))


def _random_json(rng: np.random.Generator) -> object:
    """たいていは配列。**ときどき配列でないもの**（`as_list` で止まる）。"""
    kind = rng.random()
    if kind < 0.05:
        return {"bikes": [1]}
    if kind < 0.1:
        return None
    length = int(rng.integers(0, 30))
    clean = rng.random() < 0.5
    return [int(rng.integers(-1, 30)) if clean else _random_item(rng) for _ in range(length)]


def _typed(values: list[int]) -> list[tuple[str, int]]:
    """要素の値と**型**（`Small.ONE` が `int` に化けていないか）。"""
    return [(type(one).__name__, int(one)) for one in values]


def test_as_int_list_matches_the_legacy() -> None:
    for case in range(CASES):
        value = _random_json(_rng(case))
        new = _outcome(partial(as_int_list, value, "bikes"), _typed)
        old = _outcome(partial(legacy.as_int_list, value, "bikes"), _typed)
        assert new == old, f"case {case}"


def test_as_int_list_still_rejects_bools_and_keeps_subclasses() -> None:
    """**速い道は `bool` を通さない**（`type(x) is int` だけが入る）。派生型は前と同じく通す。"""
    with pytest.raises(ShapeError):
        as_int_list([1, True], "bikes")
    assert _typed(as_int_list([Small.ONE, 2], "bikes")) == [("Small", 1), ("int", 2)]
    assert as_int_list([], "bikes") == []


# ── ② 長形式化 ─────────────────────────────────────────────────
def _random_time(rng: np.random.Generator) -> datetime:
    """日をまたぐ 10 分の幅から。**同点が出やすいよう秒は粗く**、ときどきマイクロ秒も足す。"""
    seconds = int(rng.integers(0, SPREAD_S // 30)) * 30
    micro = int(rng.integers(0, 1_000_000)) if rng.random() < 0.3 else 0
    return NEAR_MIDNIGHT + timedelta(seconds=seconds, microseconds=micro)


def _random_values(rng: np.random.Generator, length: int) -> list[int]:
    """欠損・負の値・`int16` の両端を多めに混ぜる。"""
    picks = rng.choice([-1, -1, 0, 1, 5, 30, -7, INT16_MIN, INT16_MAX], size=length)
    return [int(one) for one in picks]


def _random_snapshot(rng: np.random.Generator, n_stations: int) -> Snapshot:
    observed = _random_time(rng)
    length = int(rng.integers(0, n_stations + 1))
    arrays = [_random_values(rng, length) for _ in range(4)]
    return Snapshot(observed, observed + timedelta(seconds=int(rng.integers(0, 300))), *arrays)


def _broken(rng: np.random.Generator, snapshot: Snapshot, n_stations: int) -> Snapshot:
    """**壊れたスナップショット**（長さの不揃い・台帳より長い・`int16` の範囲外）。"""
    kind = rng.random()
    if kind < 0.4:
        return Snapshot(snapshot.observed_at, snapshot.fetched_at, [1], [], [1], [1])
    if kind < 0.7:
        too_long = [0] * (n_stations + 1)
        return Snapshot(snapshot.observed_at, snapshot.fetched_at, *[too_long] * 4)
    over = [*snapshot.bikes, INT16_MAX + 1]
    return Snapshot(snapshot.observed_at, snapshot.fetched_at, over, over, over, over)


def _random_case(rng: np.random.Generator) -> tuple[tuple[str, ...], list[Snapshot]]:
    n_stations = int(rng.integers(0, 12))
    ids = tuple(STATION_POOL[int(one)] for one in rng.integers(0, len(STATION_POOL), n_stations))
    snapshots = [_random_snapshot(rng, n_stations) for _ in range(int(rng.integers(0, 7)))]
    if snapshots and rng.random() < 0.2:
        # **2 つ壊すこともある。** どちらで止まるか（時刻の順に確かめる）まで前と同じであること
        count = min(len(snapshots), int(rng.integers(1, 3)))
        for at in rng.choice(len(snapshots), size=count, replace=False):
            snapshots[int(at)] = _broken(rng, snapshots[int(at)], n_stations)
    return ids, snapshots


def _nothing(table: pa.Table) -> None:
    """通ったかどうかだけを見る。"""
    del table


def _table_digest(table: pa.Table) -> tuple[bytes, bytes]:
    """**毎時の Parquet と同じ書き方**のバイト列と、表そのもののバイト列。"""
    return to_parquet_bytes(table), _ipc(table)


def test_to_table_matches_the_legacy() -> None:
    for case in range(CASES):
        ids, snapshots = _random_case(_rng(case))
        new = _outcome(partial(to_table, "hellocycling", ids, snapshots), _table_digest)
        old = _outcome(partial(legacy.to_table, "hellocycling", ids, snapshots), _table_digest)
        assert new == old, f"case {case}"


def test_to_table_cases_cover_both_paths() -> None:
    """乱数の入力が、**通る道と止まる道の両方**を十分に通っていること（検査の検査）。"""
    outcomes = [
        _outcome(partial(to_table, "hellocycling", *_random_case(_rng(case))), _nothing)[0]
        for case in range(CASES)
    ]
    assert outcomes.count("ok") > CASES * 0.7
    assert outcomes.count("error") > CASES * 0.05


# ── ③ as-of の並べ替え ─────────────────────────────────────────
def _random_long_table(rng: np.random.Generator) -> pa.Table:
    """長形式の表。**ときどきチャンクを分ける**（`concat_tables` の後の形）。"""
    n_rows = int(rng.integers(0, 60))
    observed = NEAR_MIDNIGHT_MS + rng.integers(0, SPREAD_S // 30, n_rows) * 30_000
    columns = {
        "system_id": pa.array(rng.choice(SYSTEMS, n_rows).tolist(), type=pa.string()),
        "station_id": pa.array(rng.choice(STATION_POOL, n_rows).tolist(), type=pa.string()),
        "observed_at": _timestamps(observed),
        "fetched_at": _timestamps(observed + 60_000),
        **{
            name: pa.array(_random_values(rng, n_rows), type=pa.int16())
            for name in ("bikes", "docks", "flags", "reported_age_s")
        },
    }
    return _chunked(rng, pa.table(columns, schema=SCHEMA))


def _timestamps(milliseconds: Int64) -> pa.Array:
    return pa.array(milliseconds, type=pa.int64()).cast(SCHEMA.field("observed_at").type)


def _chunked(rng: np.random.Generator, table: pa.Table) -> pa.Table:
    if table.num_rows < 2 or rng.random() < 0.5:
        return table
    cut = int(rng.integers(1, table.num_rows))
    return pa.concat_tables([table.slice(0, cut), table.slice(cut)])


def _random_keys(rng: np.random.Generator) -> list[tuple[str, str]]:
    """台帳。**無いシステム・観測に無いポート・同じ鍵の 2 度目**を混ぜる。"""
    count = int(rng.integers(0, 16))
    return [(str(rng.choice(SYSTEMS[:2])), str(rng.choice(STATION_POOL))) for _ in range(count)]


def _observations_digest(observations: Observations) -> list[tuple[str, bytes]]:
    return [
        _bytes_of(one)
        for one in (
            observations.starts,
            observations.observed_at_ms,
            observations.fetched_at_ms,
            observations.bikes,
            observations.docks,
            observations.flags,
            observations.reported_age_s,
        )
    ]


def test_to_observations_matches_the_legacy() -> None:
    for case in range(CASES):
        rng = _rng(case)
        table, keys = _random_long_table(rng), _random_keys(rng)
        new = _outcome(partial(to_observations, table, keys), _observations_digest)
        old = _outcome(partial(legacy.to_observations, table, keys), _observations_digest)
        assert new == old, f"case {case}"
        assert new[0] == "ok", f"case {case}: {new}"


# ── ④ 流量 ────────────────────────────────────────────────────
def _random_observations(rng: np.random.Generator) -> Observations:
    """区間の形を直に作る。**空のポート・観測が 1 つだけのポート・全部欠損のポート**を混ぜる。"""
    counts = rng.integers(0, 10, int(rng.integers(0, 8)))
    starts = np.concatenate([[0], np.cumsum(counts)]).astype(np.int64)
    n_rows = int(starts[-1])
    times = [
        np.sort(NEAR_MIDNIGHT_MS + rng.integers(0, SPREAD_S // 30, int(one)) * 30_000)
        for one in counts
    ]
    observed = np.concatenate([np.zeros(0, dtype=np.int64), *times]).astype(np.int64)
    bikes = np.asarray(_random_values(rng, n_rows), dtype=np.int16)
    blank = np.zeros(n_rows, dtype=np.int16)
    return Observations(starts, observed, observed + 60_000, bikes, blank, blank, blank)


def _flow_digest(flow: Flow) -> list[tuple[str, bytes]]:
    return [
        _bytes_of(one)
        for one in (flow.rentals, flow.returns, flow.n_changes, flow.minutes_since_last_change)
    ]


@pytest.mark.parametrize("window_min", WINDOWS_MIN)
def test_compute_flow_matches_the_legacy(window_min: int) -> None:
    for case in range(CASES):
        observations = _random_observations(_rng(case))
        new = _outcome(partial(compute_flow, observations, window_min), _flow_digest)
        old = _outcome(partial(legacy.compute_flow, observations, window_min), _flow_digest)
        assert new == old, f"case {case}"
        assert new[0] == "ok", f"case {case}: {new}"


def test_compute_flow_on_tables_matches_the_legacy() -> None:
    """**推論と学習の通り道そのまま**（長形式 → as-of の形 → 流量）で、新旧の鎖を比べる。"""
    for case in range(CASES):
        rng = _rng(case)
        table, keys = _random_long_table(rng), _random_keys(rng)
        new = _flow_digest(compute_flow(to_observations(table, keys)))
        old = _flow_digest(legacy.compute_flow(legacy.to_observations(table, keys)))
        assert new == old, f"case {case}"


def test_compute_flow_refuses_keys_that_do_not_fit() -> None:
    """**鍵が `int64` に収まらない入力は止める**（黙ってあふれると、隣のポートを数える）。

    実データ（2 万ポート・数日の幅）では起きない。時刻が壊れた入力だけが当たる。
    """
    starts = np.array([0, 1, 2], dtype=np.int64)
    times = np.array([-(2**62), 2**62], dtype=np.int64)
    bikes = np.array([1, 2], dtype=np.int16)
    observations = Observations(starts, times, times, bikes, bikes, bikes, bikes)
    with pytest.raises(ValueError, match="int64"):
        compute_flow(observations)


def test_compute_flow_writes_nothing_for_ports_seen_less_than_twice() -> None:
    """**観測された行が 2 つ未満のポートは 0 と NaN のまま**（前と同じ。「分からない」）。"""
    starts = np.array([0, 3, 5], dtype=np.int64)
    times = NEAR_MIDNIGHT_MS + np.arange(5, dtype=np.int64) * 60_000
    bikes = np.array([MISSING, 4, MISSING, 3, 1], dtype=np.int16)
    observations = Observations(starts, times, times, bikes, bikes, bikes, bikes)
    flow = compute_flow(observations)
    assert flow.rentals.tolist() == [0, 0, 0, 0, 2]
    assert np.isnan(flow.minutes_since_last_change[:3]).all()
    assert flow.minutes_since_last_change[3:].tolist() == [180.0, 0.0]
