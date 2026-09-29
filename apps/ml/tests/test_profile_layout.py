"""`profile.parquet` の並びと行群（**W5 の契約 28**。W6 プラン §6.3 の PR C）。

**契約 28 の検査 3 本**をここに置く。

  ① **どの行群も `(dow_type, slot15)` を 1 組しか含まない**（行群の統計の最小と最大が等しい）
  ② **1 周期ぶんの読みが 2 MB 未満**（**読んだバイトを数える**。絞った結果の行数ではなく）
  ③ **並べ替えた表から作った B3 の成果物が、前の並びから作ったものとバイト一致する**
     （`created_at` を除く）

**崩れても例外は出ず、配信の下りが黙って 10 倍になる**（W5 プラン §6.10 の「先に測った」②′）。
だから名前を付けて検査する。

**② は本番と同じ規模の合成の表で測る。** 小さい表では何を壊しても 2 MB に届かず、検査が
空振りする。値の分布は 2026-09-26 の本番の `profile.parquet` から写し、厚さは**窓が満ちた
状態**（平日 20 日）にした——平日の窓が厚いほど和が大きく、周期が重くなる（W6 プランの
所見 204）。いちばん重い周期は 1.69 MB で、本番の 9/26 の版（平日 12 日）の 1.56 MB と、
窓が満ちたときの見込み（約 1.73 MB）のあいだにある。**書き方を既定に戻すと 2.22 MB で落ちる。**
"""

import gzip
import io
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from functools import reduce
from pathlib import Path
from typing import Final

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from bikechance_ml.baselines.artifact import from_bytes
from bikechance_ml.features import profile
from bikechance_ml.features.calendar import DOW_TYPE_ORDER
from bikechance_ml.features.constants import GRID_MINUTES, GRID_POINTS_PER_DAY, HORIZONS_MIN
from bikechance_ml.features.grid import profile_path
from bikechance_ml.jobs import fit_baseline
from bikechance_ml.jobs.build_features import to_parquet_bytes
from bikechance_ml.jobs.build_profiles import DELTA_ENCODED, STATISTICS_COLUMNS, profile_bytes
from bikechance_ml.jobs.snapshot_table import COMPRESSION
from tests import eval_fixture
from tests import features_fixture as observed
from tests.test_window import write_samples

#: 1 周期の読みの上限（W5 プラン §6.10 の J2 の完了条件 3。W6 プラン §8.3 の SQL と同じ値）。
CYCLE_BUDGET_B: Final[int] = 2_000_000

#: Parquet の先頭の目印（4 バイト）。**読み手は読まなくてよい**ので、数えた量から外れうる。
PARQUET_MAGIC: Final[bytes] = b"PAR1"

#: 小さい表の平日の日数。**配る側の下限（平日 3 日）にちょうど届く**（B2 がセルを配る）。
WEEKDAYS: Final[int] = 3


class CountingFile:
    """**読んだバイトを数えるファイル。** pyarrow は `read` で取りに来る。"""

    #: pyarrow が開く前に見る。
    closed = False

    def __init__(self, body: bytes) -> None:
        self._body = io.BytesIO(body)
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._body.read(size)
        self.bytes_read += len(chunk)
        return chunk

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return self._body.seek(offset, whence)

    def tell(self) -> int:
        return self._body.tell()


def _bytes_read(body: bytes, indices: Sequence[int]) -> int:
    """その行群だけを読んだとき、**ファイルから実際に読んだバイト数**（フッタの読みを含む）。"""
    counting = CountingFile(body)
    pq.ParquetFile(counting).read_row_groups(list(indices))
    return counting.bytes_read


# ── 小さい表（本番と同じ入口で作る）────────────────────────────
def _weekday_daily() -> pa.Table:
    """学習サンプルの検査と同じ観測（ポート 4・枠 32〜56）から作った平日の `daily`。"""
    inputs = profile.DayInputs(
        day=observed.DAY, table=observed.load_snapshots(), holidays=frozenset()
    )
    return profile.build_day(inputs)


def _relabelled(table: pa.Table, dow: str) -> pa.Table:
    index = table.schema.get_field_index("dow_type")
    labels = pa.array([dow] * table.num_rows, type=pa.string())
    return table.set_column(index, table.schema.field(index), labels)


def _small() -> pa.Table:
    """**土曜 1 日と平日 3 日**を転がした `profile`。ポートと枠ごとにセルの有無が違う。"""
    weekday = _weekday_daily()
    dailies = [_relabelled(weekday, "sat"), *([weekday] * WEEKDAYS)]
    return reduce(lambda rolled, one: profile.roll(rolled, one, None), dailies, None)


def _keys(table: pa.Table) -> list[tuple[str, int]]:
    dows = table.column("dow_type").to_pylist()
    slots = table.column("slot15").to_pylist()
    return [(str(dow), int(slot)) for dow, slot in zip(dows, slots, strict=True)]


def _metadata(body: bytes) -> pq.FileMetaData:
    return pq.ParquetFile(io.BytesIO(body)).metadata


def _columns(group: pq.RowGroupMetaData) -> dict[str, pq.ColumnChunkMetaData]:
    return {group.column(i).path_in_schema: group.column(i) for i in range(group.num_columns)}


def _only_key(group: pq.RowGroupMetaData) -> tuple[str, int]:
    """行群の `(dow_type, slot15)`。**統計の最小と最大が等しいことを確かめて**取る（①）。"""
    columns = _columns(group)
    stats = [columns[name].statistics for name in profile.ROW_GROUP_KEY]
    assert all(one is not None and one.has_min_max for one in stats), "鍵に統計が無い"
    assert all(one.min == one.max for one in stats), "1 つの行群に 2 組以上がある"
    return (str(stats[0].min), int(stats[1].min))


def _group_keys(body: bytes) -> list[tuple[str, int]]:
    metadata = _metadata(body)
    return [_only_key(metadata.row_group(index)) for index in range(metadata.num_row_groups)]


# ── ① 1 行群 ＝ 1 つの `(dow_type, slot15)` ─────────────────────
def test_every_row_group_holds_one_dow_type_and_slot() -> None:
    """① **どの行群も `(dow_type, slot15)` を 1 組しか含まず、組は昇順に 1 度ずつ現れる。**"""
    table = _small()
    assert _group_keys(profile_bytes(table)) == sorted(set(_keys(table)))


def test_the_groups_are_cut_at_the_key_not_by_rows() -> None:
    """**行数ではなく鍵の変わり目で切る。** セルの数は枠ごとに違う（観測の無い枠がある）。

    行数で切ると境目がずれ、1 周期が触る行群が増える（W5 の実測。日祝の周期で 1.51 → 2.35 MB）。
    """
    table = _small()
    metadata = _metadata(profile_bytes(table))
    sizes = [metadata.row_group(index).num_rows for index in range(metadata.num_row_groups)]
    counts = Counter(_keys(table))
    assert sizes == [counts[key] for key in sorted(counts)]
    assert len(set(sizes)) > 1, "大きさの揃った行群では、行数で切っても通ってしまう"


def test_a_table_in_the_old_order_is_not_written() -> None:
    """**並びが崩れた表は、書く前に止める**（黙って書くと、下りが 10 倍になるだけで気づけない）。"""
    old = _small().sort_by([(one, "ascending") for one in profile.KEY_COLUMNS])
    with pytest.raises(profile.LayoutError):
        profile_bytes(old)


def test_an_unknown_dow_type_is_not_written() -> None:
    """**知らない曜日種別**があれば、並びを確かめられないので止める。"""
    with pytest.raises(profile.LayoutError, match="曜日種別"):
        profile_bytes(_relabelled(_small(), "holiday"))


def test_an_empty_profile_is_written_and_read_back() -> None:
    """**観測が 1 つも無い日も止まらない。** 行群 0 のファイルになり、読むと 0 行・同じ列。"""
    empty = profile.PROFILE_SCHEMA.empty_table()
    assert profile.row_groups(empty) == ()
    back = pq.read_table(io.BytesIO(profile_bytes(empty)))
    assert back.num_rows == 0
    assert back.schema == profile.PROFILE_SCHEMA


def test_the_file_reads_back_as_the_same_table() -> None:
    """**読み戻すと、中身も並びも同じ表になる**（行群に分けても、差分で書いても）。"""
    table = _small()
    assert pq.read_table(io.BytesIO(profile_bytes(table))).equals(table)


def test_the_same_table_gives_the_same_bytes() -> None:
    """**同じ表からは同じバイト列が出る。** `--compare`（D-28）はバイト列で突き合わせる。"""
    table = _small()
    assert profile_bytes(table) == profile_bytes(table)


# ── 書き方（W6 プランの所見 204）───────────────────────────────
def test_only_the_row_group_key_has_statistics() -> None:
    """**統計は行群を選ぶ鍵の 2 列だけ。** ほかの列の統計はフッタを太らせるだけ（359 → 263 KB）。"""
    group = _metadata(profile_bytes(_small())).row_group(0)
    with_stats = {name for name, one in _columns(group).items() if one.is_stats_set}
    assert with_stats == set(profile.ROW_GROUP_KEY)
    assert STATISTICS_COLUMNS == profile.ROW_GROUP_KEY


def test_station_id_is_written_as_differences() -> None:
    """**`station_id` は辞書にしない**——行群ごとに 2 万語の辞書を持ち直していた（1 周期の 26%）。"""
    columns = _columns(_metadata(profile_bytes(_small())).row_group(0))
    assert not columns["station_id"].has_dictionary_page
    assert "DELTA_BYTE_ARRAY" in columns["station_id"].encodings
    assert columns["system_id"].has_dictionary_page, "ほかの列は辞書のまま"
    assert dict(DELTA_ENCODED) == {"station_id": "DELTA_BYTE_ARRAY"}


def test_every_column_is_compressed_like_the_other_parquet() -> None:
    """**圧縮は zstd（ほかの Parquet と同じ `COMPRESSION`）**。軽い圧縮に替えると下りが増える。

    合成の表では snappy でも 2 MB を割ったので、② だけでは捕まらない（壊して確かめて分かった）。
    """
    metadata = _metadata(profile_bytes(_small()))
    codecs = {
        one.compression
        for index in range(metadata.num_row_groups)
        for one in _columns(metadata.row_group(index)).values()
    }
    assert codecs == {COMPRESSION.upper()}


# ── ② 1 周期の読み（本番と同じ規模の合成の表）──────────────────
#: 本番の規模（9/26 の `profile.parquet`：6,171,293 行、ポート 21,459）。
PORTS_BY_SYSTEM: Final[Mapping[str, int]] = {"docomo-cycle": 6_176, "hellocycling": 15_283}

#: ポート番号を引く範囲（本番の `station_id` は数字だけ。docomo は 1〜5 桁、HELLO は 2〜5 桁）。
ID_RANGE_BY_SYSTEM: Final[Mapping[str, tuple[int, int]]] = {
    "docomo-cycle": (1, 7_500),
    "hellocycling": (10, 20_000),
}

#: 1 行群から抜けるポートの割合（本番の 1 行群は 21,395〜21,454 行）。
ABSENT_SHARE: Final[float] = 0.002

#: 曜日種別ごとの厚さ。**窓（28 日）が満ちた状態**：平日 20 日・土曜 4 日・日祝 4〜7 日。
#: 本番の 9/26 の版（平日 12 日）より重い側で数える。それより薄いセルの割合も持つ。
DAYS_BY_DOW: Final[Mapping[str, int]] = {"sat": 4, "sun_holiday": 6, "weekday": 20}
THIN_SHARE: Final[float] = 0.02

#: 休止した格子点を含むセルの割合（本番の平日の枠 32 で 3%）。
SUSPENDED_SHARE: Final[float] = 0.03

#: 「借りられた / 返せた」割合のベータ分布（本番の中央は 30 / 36 ≒ 0.83、1 割は 0.25 以下）。
OK_BETA: Final[tuple[float, float]] = (1.6, 0.5)

#: 平均台数の対数正規（本番の台数の和の中央 87 ÷ 36 ≒ 2.4）と、台数のばらつき（変動係数）。
BIKES_MEDIAN: Final[float] = 2.4
BIKES_SIGMA: Final[float] = 1.1
BIKES_CV: Final[tuple[float, float]] = (0.2, 0.8)

#: 流量（60 分窓の和）：0 の割合と、0 でないときの中央（対数正規）。本番は 0 が 26% と 32%。
FLOWS: Final[Mapping[str, tuple[float, float]]] = {
    "sum_rentals_60": (0.26, 20.0),
    "sum_returns_60": (0.32, 15.0),
}
FLOW_SIGMA: Final[float] = 1.2

#: 乱数の種。**固定する**——検査の結果が日によって変わらないように。
SEED: Final[int] = 20260926


def _ports(rng: np.random.Generator) -> tuple[pa.Array, pa.Array]:
    """`(system_id, station_id)` の並び。**文字列の昇順**（`_fold` と同じ並べ方）。"""
    ids = [
        np.sort(
            rng.choice(np.arange(*ID_RANGE_BY_SYSTEM[system]), count, replace=False).astype(str)
        )
        for system, count in PORTS_BY_SYSTEM.items()
    ]
    systems = [np.full(count, system) for system, count in PORTS_BY_SYSTEM.items()]
    return pa.array(np.concatenate(systems)), pa.array(np.concatenate(ids))


def _cells(rng: np.random.Generator, n_ports: int) -> tuple[npt.NDArray[np.int64], ...]:
    """行ごとの `(セル, ポート)`。セルは `曜日種別 × 96 + 枠`。**契約 28 の並びで出る**。"""
    n_cells = len(DOW_TYPE_ORDER) * profile.SLOTS_PER_DAY
    present = rng.random((n_cells, n_ports)) > ABSENT_SHARE
    cell, port = np.nonzero(present)
    return np.asarray(cell, dtype=np.int64), np.asarray(port, dtype=np.int64)


def _n_days(rng: np.random.Generator, dow: npt.NDArray[np.int64]) -> npt.NDArray[np.int16]:
    full = np.asarray([DAYS_BY_DOW[one] for one in DOW_TYPE_ORDER], dtype=np.int64)[dow]
    thin = rng.random(dow.size) < THIN_SHARE
    return np.asarray(np.where(thin, rng.integers(1, full + 1), full), dtype=np.int16)


def _flow(rng: np.random.Generator, size: int, zero: float, median: float) -> npt.NDArray[np.int32]:
    drawn = np.rint(rng.lognormal(np.log(median), FLOW_SIGMA, size))
    return np.asarray(np.where(rng.random(size) < zero, 0, drawn), dtype=np.int32)


def _sums(rng: np.random.Generator, n: npt.NDArray[np.int16]) -> dict[str, npt.NDArray[np.int64]]:
    """和の列。**値の分布は本番の平日の枠 32 から写した**（上の定数）。"""
    size = n.size
    bikes = np.rint(n * rng.lognormal(np.log(BIKES_MEDIAN), BIKES_SIGMA, size))
    spread = 1 + rng.uniform(*BIKES_CV, size) ** 2
    drawn = {
        "n": n,
        "n_suspended": np.where(rng.random(size) < SUSPENDED_SHARE, n, 0),
        "n_bike_ok": rng.binomial(n, rng.beta(*OK_BETA, size)),
        "n_dock_ok": rng.binomial(n, rng.beta(*OK_BETA, size)),
        "sum_bikes": bikes,
        "sum_bikes_sq": np.rint(bikes**2 / n * spread),
        **{name: _flow(rng, size, *shape) for name, shape in FLOWS.items()},
    }
    return {name: np.asarray(values, dtype=np.int64) for name, values in drawn.items()}


def _production_scale() -> pa.Table:
    """本番と同じ規模の `profile`（約 617 万行・288 行群）。**乱数の種は固定。**"""
    rng = np.random.default_rng(SEED)
    systems, stations = _ports(rng)
    cell, port = _cells(rng, len(stations))
    dow = cell // profile.SLOTS_PER_DAY
    n_days = _n_days(rng, dow)
    n = np.asarray(n_days * profile.GRID_POINTS_PER_SLOT, dtype=np.int16)
    numbers = {"slot15": cell % profile.SLOTS_PER_DAY, "n_days": n_days, **_sums(rng, n)}
    columns = {
        "system_id": systems.take(port),
        "station_id": stations.take(port),
        "dow_type": pa.array(DOW_TYPE_ORDER).take(dow),
        **{name: pa.array(values, type=_type_of(name)) for name, values in numbers.items()},
    }
    return pa.table(columns, schema=profile.PROFILE_SCHEMA)


def _type_of(name: str) -> pa.DataType:
    return profile.PROFILE_SCHEMA.field(name).type


def _needed(groups: Mapping[tuple[str, int], int], minute: int, days: tuple[str, str]) -> list[int]:
    """その周期（基準 `minute`、今日と明日の曜日種別 `days`）が要る行群。

    目標時刻 `t + h` の枠と、日をまたいだかで引く（`target_slot`・`target_day_offset`。
    B2 と `prof_*` と同じ規則）。**日をまたぐ周期（21:00〜23:55）は 2 つの曜日種別から取る。**
    """
    horizons = np.asarray(HORIZONS_MIN)
    slots = profile.target_slot(minute, horizons).tolist()
    offsets = profile.target_day_offset(minute, horizons).tolist()
    return sorted(
        {groups[(days[offset], slot)] for slot, offset in zip(slots, offsets, strict=True)}
    )


def _compressed_size(group: pq.RowGroupMetaData) -> int:
    """行群の**圧縮後**の大きさ（落とす量。`total_byte_size` は圧縮前なので使わない）。"""
    return sum(one.total_compressed_size for one in _columns(group).values())


def _heaviest_cycle(body: bytes) -> list[int]:
    """1 日の 288 周期 × 今日と明日の曜日種別 9 組のうち、**要る行群がいちばん重い周期**。"""
    groups = {key: index for index, key in enumerate(_group_keys(body))}
    metadata = _metadata(body)
    sizes = [_compressed_size(metadata.row_group(i)) for i in range(metadata.num_row_groups)]
    minutes = [slot * GRID_MINUTES for slot in range(GRID_POINTS_PER_DAY)]
    pairs = [(today, tomorrow) for today in DOW_TYPE_ORDER for tomorrow in DOW_TYPE_ORDER]
    cycles = [_needed(groups, minute, pair) for pair in pairs for minute in minutes]
    return max(cycles, key=lambda wanted: sum(sizes[index] for index in wanted))


@pytest.fixture(scope="module")
def production() -> bytes:
    return profile_bytes(_production_scale())


def test_the_heaviest_cycle_reads_less_than_two_megabytes(production: bytes) -> None:
    """② **1 周期ぶんの読みが 2 MB 未満**（J2 の完了条件 3）。**読んだバイトを数える。**

    いちばん重い周期をメタデータで選び、その周期を実際に読む。**フッタの読みも入る**——
    配信は周期ごとにフッタを読むので、それも下りになる。
    """
    heaviest = _heaviest_cycle(production)
    assert len(heaviest) <= len(HORIZONS_MIN)
    assert _bytes_read(production, heaviest) < CYCLE_BUDGET_B


def test_the_counter_sees_every_byte_of_a_full_read() -> None:
    """**数え方が空回りしていない**：全部の行群を読めば、ファイルのほぼ全部を数える。"""
    body = profile_bytes(_small())
    everything = range(_metadata(body).num_row_groups)
    assert _bytes_read(body, everything) >= len(body) - len(PARQUET_MAGIC)


# ── ③ 並びは成果物を変えない ────────────────────────────────────
def _put(root: Path, path: str, body: bytes) -> None:
    whole = root / path
    whole.parent.mkdir(parents=True, exist_ok=True)
    whole.write_bytes(body)


def _root(root: Path, profile_body: bytes) -> Path:
    """`--local` の下を組む：学習サンプル・学習の各日の `daily`・最終日の `profile`。"""
    days = eval_fixture.DAYS
    write_samples(root, dict.fromkeys(days, True))
    daily = to_parquet_bytes(_weekday_daily())
    for day in days:
        _put(root, profile_path(day, profile.DAILY_NAME), daily)
    _put(root, profile_path(days[-1], profile.PROFILE_NAME), profile_body)
    return root


def _fit(root: Path, monkeypatch: pytest.MonkeyPatch) -> bytes:
    """`fit_baseline` を**入口から**走らせ、書き出した成果物を返す（Storage には触れない）。"""
    monkeypatch.setattr(fit_baseline, "read_storage_config", lambda: None)
    monkeypatch.setattr(fit_baseline, "open_storage", lambda config: nullcontext(None))
    out = root / "artifact.json.gz"
    days = eval_fixture.DAYS
    window = ["--from", f"{days[0]}", "--to", f"{days[-1]}"]
    assert fit_baseline.run([*window, "--local", str(root), "--out", str(out)]) == 0
    return out.read_bytes()


def _without_created_at(body: bytes) -> bytes:
    """成果物の JSON から `created_at` の値だけを消す（**ほかは 1 バイトも触らない**）。"""
    return re.sub(rb'"created_at": "[^"]*"', b'"created_at": ""', gzip.decompress(body))


def test_the_b3_artifact_is_the_same_in_either_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """③ **前の並びで置いた版と契約 28 の並びで置いた版から、同じ成果物ができる。**

    比べるのは `created_at` を除いた JSON のバイト列（PR #116 と同じ作法）。
    **B2 が実際にセルを配っていること**も確かめる——配らない版どうしなら、並びに関係なく
    一致してしまう。並べ替えた 2 つのファイルが本当に違う順であることも確かめる。
    """
    table = _small()
    old_table = table.sort_by([(one, "ascending") for one in profile.KEY_COLUMNS])
    assert _keys(old_table) != _keys(table), "同じ順の 2 つでは、並びを変えたことにならない"
    old = _fit(_root(tmp_path / "old", to_parquet_bytes(old_table)), monkeypatch)
    new = _fit(_root(tmp_path / "new", profile_bytes(table)), monkeypatch)
    assert _without_created_at(new) == _without_created_at(old)
    assert all(model.b2.cells > 0 for model in from_bytes(new).targets.values())
