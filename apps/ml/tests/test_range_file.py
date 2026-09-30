"""Range 要求で一部だけ読む（`io/range_file.py`、W6 の PR D、W6-03）。

守るのは 5 つ。

  * **選んだ行群だけを読む**——中身は全部を読んだときと同じ、落とすのは末尾・フッタ・選んだ範囲だけ
  * **隣り合う行群は 1 回にまとめ、間の行群は取らない**（下りの予算。1 周期 2 MB 未満）
  * **フッタが末尾の 64 KiB より長ければ、足りないぶんだけを取る**（PR C の版は 263 KB）
  * **版が読みの途中で入れ替わったら、1 度だけ読み直し、それでも揃わなければ止める**
    （版は応答を受け取ったその場で比べる。フッタを pyarrow に渡す前に）
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
#: 入れ替わった後の版。**ポートが 1 つ多く、中身も大きさも違う**——同じバイトに ETag だけを
#: 変えて返すと、混ぜても読めてしまい、検査が空振りする。
NEWER: Final[pa.Table] = range_fixture.full_profile(
    (("docomo-cycle", "d1"), ("docomo-cycle", "d2"), ("hellocycling", "h1"))
)
NEWER_BODY: Final[bytes] = profile_bytes(NEWER)

#: 1 回の読みの中で、何回目の要求から新しい版が返るか（末尾が 0 回目）。
FROM_FOOTER: Final[int] = 1
FROM_GROUPS: Final[int] = 2


def _tails(requests: list[str]) -> int:
    return sum(one.startswith("bytes=-") for one in requests)


@dataclass
class SwitchingStorage:
    """**1 度目の読みの `switch_at` 回目の要求から、新しい版を返す**偽の Storage。

    読み直した 2 度目は、始めから新しい版で揃う。`flapping` なら 2 度目も同じ所で入れ替わる。
    """

    switch_at: int
    flapping: bool = False
    #: 古い版と新しい版の ETag。**`(None, None)` なら ETag の無い応答**（大きさだけで見分ける）
    etags: tuple[str | None, str | None] = ('"v1"', '"v2"')
    requests: list[str] = field(default_factory=list)

    def download_range(self, bucket: str, path: str, byte_range: str) -> Piece | None:
        self.requests.append(byte_range)
        old, new = (BODY, self.etags[0]), (NEWER_BODY, self.etags[1])
        body, etag = old if self._serves_old() else new
        return range_fixture.serve(body, byte_range, etag)

    def _serves_old(self) -> bool:
        """最後の末尾から数えて `switch_at` 回目より前なら、古い版を返す。

        行群は並行に取るので、数えた位置は前後しうる。**行群はどれも 2 回目以降**なので、
        `switch_at` が 2 以下なら返す版は変わらない。
        """
        starts = [index for index, one in enumerate(self.requests) if one.startswith("bytes=-")]
        early = len(self.requests) - 1 - starts[-1] < self.switch_at
        return early and (len(starts) == 1 or self.flapping)


@pytest.mark.parametrize(
    ("switch_at", "etags"),
    [
        # **フッタの残りから新しい版**：受け取ったその場で比べないと、pyarrow が混ざった
        # フッタを読んで `OSError` で止まり、読み直しに入らない（2026-09-27 に再現）
        (FROM_FOOTER, ('"v1"', '"v2"')),
        # **行群から新しい版**：古い版のフッタの番地で、新しい版の中身を読むところだった
        (FROM_GROUPS, ('"v1"', '"v2"')),
        # **ETag の無い応答**でも、全体の大きさで見分ける
        (FROM_GROUPS, (None, None)),
    ],
)
def test_a_version_change_is_read_again_once(
    switch_at: int, etags: tuple[str | None, str | None]
) -> None:
    """**1 度目の途中で入れ替わる**——読み直した 2 度目で揃い、**新しい版**が読める。"""
    storage = SwitchingStorage(switch_at=switch_at, etags=etags)
    read = _read(storage)
    assert _tails(storage.requests) == 2
    assert read.table.equals(_only(NEWER, CELLS))


@pytest.mark.parametrize("switch_at", [FROM_FOOTER, FROM_GROUPS])
def test_a_version_that_keeps_changing_stops(switch_at: int) -> None:
    """**2 度とも揃わなければ止める**（混ぜた版の値で配らない）。"""
    storage = SwitchingStorage(switch_at=switch_at, flapping=True)
    with pytest.raises(ChangedWhileReadingError):
        _read(storage)
    assert _tails(storage.requests) == 2


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
    """**頼んだ範囲より短い応答**を返す（途中で切れた）。`etag` を変えると、違う版の短い応答。"""

    etag: str = range_fixture.ETAG
    requests: list[str] = field(default_factory=list)

    def download_range(self, bucket: str, path: str, byte_range: str) -> Piece | None:
        self.requests.append(byte_range)
        piece = range_fixture.serve(BODY, byte_range)
        if byte_range.startswith("bytes=-"):
            return piece
        return Piece(start=piece.start, body=piece.body[:-1], size=piece.size, etag=self.etag)


def test_a_response_that_misses_the_span_stops() -> None:
    storage = ShortStorage()
    with pytest.raises(CorruptFileError):
        _read(storage)
    assert _tails(storage.requests) == 1, "壊れた応答は読み直さない"


def test_a_short_response_from_another_version_is_a_change() -> None:
    """**版は範囲より先に見る**——違う版の応答は、範囲が欠けていても入れ替わりとして読み直す。"""
    storage = ShortStorage(etag='"v2"')
    with pytest.raises(ChangedWhileReadingError):
        _read(storage)
    assert _tails(storage.requests) == 2


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
