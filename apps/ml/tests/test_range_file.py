"""Range 要求で一部だけ読む（`io/range_file.py`、W6 の PR D、W6-03）。

守るのは 5 つ。

  * **選んだ行群だけを読む**——中身は全部を読んだときと同じ、落とすのは末尾・フッタ・選んだ範囲だけ
  * **隣り合う行群は 1 回にまとめ、間の行群は取らない**（下りの予算。1 周期 2 MB 未満）
  * **フッタが末尾の 64 KiB より長ければ、足りないぶんだけを取る**（PR C の版は 263 KB）
  * **版が読みの途中で入れ替わったら、1 度だけ読み直し、それでも揃わなければ止める**
  * **取っていない範囲は読まない**（0 で埋めて返さない）

**本番の書き手（`profile_bytes`）で書いたファイル**を、本番と同じ規則で範囲を返す偽の Storage
（`range_fixture`）から読む。
"""

import itertools
from dataclasses import dataclass, field
from typing import Final

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from bikechance_ml.features import profile
from bikechance_ml.io import range_file
from bikechance_ml.io.range_file import (
    TAIL_PROBE_B,
    ChangedWhileReadingError,
    CorruptFileError,
    NotFetchedError,
    Piece,
    Span,
    SparseFile,
)
from bikechance_ml.jobs.build_profiles import profile_bytes
from tests import range_fixture
from tests.range_fixture import MemoryStorage

PATH: Final[str] = "profiles/date=2026-09-26/profile.parquet"

#: 2 ポート × 3 曜日種別 × 96 枠。**288 行群なので、フッタが 64 KiB を超える**（本番と同じ形）。
TABLE: Final[pa.Table] = range_fixture.full_profile(
    (("docomo-cycle", "d1"), ("hellocycling", "h1"))
)
BODY: Final[bytes] = profile_bytes(TABLE)

#: 平日の 08:00 の周期が引く 8 セル（`serving_cells` の実例）。**32〜36 は隣り合う**。
CELLS: Final[tuple[tuple[str, int], ...]] = tuple(
    ("weekday", slot) for slot in (32, 33, 34, 35, 36, 38, 40, 44)
)


def _choose(cells: tuple[tuple[str, int], ...]) -> range_file.ChooseGroups:
    return lambda metadata: profile.wanted_groups(metadata, cells)


def _read(
    storage: range_file.FetchesRanges, cells: tuple[tuple[str, int], ...] = CELLS
) -> range_file.RangeRead:
    read = range_file.read_row_groups(storage, "gbfs-parquet", PATH, _choose(cells))
    assert read is not None
    return read


def _only(table: pa.Table, cells: tuple[tuple[str, int], ...]) -> pa.Table:
    """全部の表から、そのセルの行だけを**並びを変えずに**取り出す。"""
    keys = [f"{dow}/{slot}" for dow, slot in cells]
    names = pc.binary_join_element_wise(
        table.column("dow_type"), pc.cast(table.column("slot15"), pa.string()), "/"
    )
    return table.filter(pc.is_in(names, value_set=pa.array(keys)))


def _footer_start(body: bytes) -> int:
    tail = Piece(start=0, body=body, size=len(body), etag=None)
    return len(body) - range_file.TRAILER_B - range_file.footer_length(tail)


def _spans(cells: tuple[tuple[str, int], ...]) -> tuple[Span, ...]:
    metadata = pq.ParquetFile(pa.BufferReader(BODY)).metadata
    groups = profile.wanted_groups(metadata, cells)
    return range_file.coalesced([range_file.group_span(metadata.row_group(one)) for one in groups])


# ── 選んだ行群だけを読む ───────────────────────────────────────
def test_only_the_chosen_cells_come_back() -> None:
    """**中身は、全部を読んで絞ったものと同じ**（行も並びも）。"""
    assert _read(MemoryStorage({PATH: BODY})).table.equals(_only(TABLE, CELLS))


def test_the_bytes_are_the_footer_and_the_chosen_groups() -> None:
    """**落とすのは、フッタ（末尾の 64 KiB を含む）と選んだ行群だけ。** 1 バイトも多くない。"""
    read = _read(MemoryStorage({PATH: BODY}))
    footer = len(BODY) - _footer_start(BODY)
    chosen = sum(span.stop - span.start for span in _spans(CELLS))
    assert read.bytes_read == footer + chosen
    # **行群の部分は 1 割も読まない**（1 周期は 8 / 288 行群。この表ではフッタのほうが大きい）
    assert chosen * 10 < _footer_start(BODY)


def test_adjacent_groups_are_one_request_and_gaps_are_not_fetched() -> None:
    """**隣り合う行群（枠 32〜36）は 1 回にまとめ、間の行群（37・39 など）は取らない。**"""
    storage = MemoryStorage({PATH: BODY})
    read = _read(storage)
    data = [Span(one.start, one.stop).header() for one in _spans(CELLS)]
    assert len(data) == 4, "32〜36・38・40・44 の 4 つにまとまるはず"
    # **並行に取る**ので、要求の順は決まらない
    assert sorted(storage.requests[2:]) == sorted(data)
    assert read.requests == 2 + len(data)


def test_the_groups_of_the_real_writer_touch_each_other() -> None:
    """**本番の書き手の行群は、隙間なく並ぶ**（まとめ方はこれに頼っている）。"""
    metadata = pq.ParquetFile(pa.BufferReader(BODY)).metadata
    spans = [
        range_file.group_span(metadata.row_group(one)) for one in range(metadata.num_row_groups)
    ]
    assert all(left.stop == right.start for left, right in itertools.pairwise(spans))


def test_a_long_footer_is_completed_with_one_more_request() -> None:
    """**フッタが末尾の 64 KiB より長ければ、足りないぶんだけをもう 1 回**取る。"""
    storage = MemoryStorage({PATH: BODY})
    _read(storage)
    footer_start = _footer_start(BODY)
    assert len(BODY) - footer_start > TAIL_PROBE_B, "フッタが短い（仕込みの誤り）"
    assert storage.requests[0] == f"bytes=-{TAIL_PROBE_B}"
    assert storage.requests[1] == Span(footer_start, len(BODY) - TAIL_PROBE_B).header()


def test_a_small_file_is_read_with_one_request() -> None:
    """**末尾の 1 回がファイル全体を覆えば、それ以上取らない**（取り直さない）。"""
    small = range_fixture.full_profile((("hellocycling", "h1"),)).slice(0, 5)
    body = profile_bytes(small)
    assert len(body) < TAIL_PROBE_B
    storage = MemoryStorage({PATH: body})
    cells = (("sat", 0), ("sat", 3))
    read = _read(storage, cells)
    assert storage.requests == [f"bytes=-{TAIL_PROBE_B}"]
    assert read.table.equals(_only(small, cells))


def test_a_missing_object_is_none() -> None:
    """**無いものは None**（1 つ古い版に下がるかは呼ぶ側が決める）。"""
    storage = MemoryStorage({})
    assert range_file.read_row_groups(storage, "gbfs-parquet", PATH, _choose(CELLS)) is None
    assert storage.requests == [f"bytes=-{TAIL_PROBE_B}"]


def test_choosing_nothing_gives_an_empty_table_with_the_columns() -> None:
    read = _read(MemoryStorage({PATH: BODY}), ())
    assert read.table.num_rows == 0
    assert read.table.schema == profile.PROFILE_SCHEMA


# ── 版の入れ替わり ─────────────────────────────────────────────
@dataclass
class SwitchingStorage:
    """**末尾を取った後に版が入れ替わる**偽の Storage。`settle_after` 回の読みで揃う。"""

    settle_after: int
    tails: int = 0
    requests: list[str] = field(default_factory=list)

    def download_range(self, bucket: str, path: str, byte_range: str) -> Piece | None:
        self.requests.append(byte_range)
        is_tail = byte_range.startswith("bytes=-")
        self.tails += is_tail
        settled = self.tails > self.settle_after
        etag = '"v2"' if (settled or not is_tail) else '"v1"'
        return range_fixture.serve(BODY, byte_range, etag)


def test_a_version_change_is_read_again_once() -> None:
    """**1 度目は末尾だけが古い版**——読み直した 2 度目で揃い、読める。"""
    storage = SwitchingStorage(settle_after=1)
    read = _read(storage)
    assert storage.tails == 2
    assert read.table.equals(_only(TABLE, CELLS))


def test_a_version_that_keeps_changing_stops() -> None:
    """**2 度とも揃わなければ止める**（混ぜた版の値で配らない）。"""
    with pytest.raises(ChangedWhileReadingError):
        _read(SwitchingStorage(settle_after=5))


@dataclass
class VanishingStorage:
    """**末尾を返した後で消える**（途中で消された）。"""

    requests: list[str] = field(default_factory=list)

    def download_range(self, bucket: str, path: str, byte_range: str) -> Piece | None:
        self.requests.append(byte_range)
        return range_fixture.serve(BODY, byte_range) if byte_range.startswith("bytes=-") else None


def test_an_object_that_vanishes_mid_read_stops() -> None:
    with pytest.raises(ChangedWhileReadingError):
        _read(VanishingStorage())


# ── 壊れた応答 ────────────────────────────────────────────────
def test_a_tail_that_is_not_parquet_stops() -> None:
    with pytest.raises(CorruptFileError):
        _read(MemoryStorage({PATH: b"not a parquet file" * 1_000}))


@dataclass
class IgnoringStorage:
    """**範囲を無視して、いつも全体を返す**（200 で全体。`download_range` はそれも読む）。"""

    requests: list[str] = field(default_factory=list)

    def download_range(self, bucket: str, path: str, byte_range: str) -> Piece | None:
        self.requests.append(byte_range)
        return Piece(start=0, body=BODY, size=len(BODY), etag=range_fixture.ETAG)


def test_a_server_that_ignores_the_range_still_reads_right() -> None:
    assert _read(IgnoringStorage()).table.equals(_only(TABLE, CELLS))


@dataclass
class ShortStorage:
    """**頼んだ範囲より短い応答**を返す（途中で切れた）。"""

    requests: list[str] = field(default_factory=list)

    def download_range(self, bucket: str, path: str, byte_range: str) -> Piece | None:
        self.requests.append(byte_range)
        piece = range_fixture.serve(BODY, byte_range)
        if byte_range.startswith("bytes=-"):
            return piece
        return Piece(start=piece.start, body=piece.body[:-1], size=piece.size, etag=piece.etag)


def test_a_response_that_misses_the_span_stops() -> None:
    with pytest.raises(CorruptFileError):
        _read(ShortStorage())


# ── 取った範囲だけを持つファイル ───────────────────────────────
def test_the_sparse_file_refuses_bytes_it_did_not_fetch() -> None:
    """**取っていない範囲は読まない**——0 で埋めて返すと、壊れた値を読む。"""
    file = SparseFile(10, [Piece(start=2, body=b"cdef", size=10, etag=None)])
    file.seek(2)
    assert file.read(4) == b"cdef"
    file.seek(0)
    with pytest.raises(NotFetchedError):
        file.read(3)


def test_a_read_across_two_touching_pieces_is_served() -> None:
    """**継ぎ目をまたぐ読み**も通る（まとめて持つ）。重なりは先の応答を使う。"""
    pieces = [
        Piece(start=4, body=b"efgh", size=12, etag=None),
        Piece(start=0, body=b"abcd", size=12, etag=None),
        Piece(start=6, body=b"XXij", size=12, etag=None),
    ]
    file = SparseFile(12, pieces)
    file.seek(1)
    assert file.read(8) == b"bcdefghi"
    assert file.seek(0, 2) == 12


def test_coalescing_joins_only_touching_spans() -> None:
    spans = [Span(10, 20), Span(0, 10), Span(25, 30), Span(30, 31)]
    assert range_file.coalesced(spans) == (Span(0, 20), Span(25, 31))


def test_the_range_header_includes_the_last_byte() -> None:
    """**`Range` は終わりを含む**（`bytes=0-9` は 10 バイト）。1 つずれると 1 バイト欠ける。"""
    assert Span(0, 10).header() == "bytes=0-9"
