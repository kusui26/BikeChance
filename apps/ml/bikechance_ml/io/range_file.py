"""Storage のオブジェクトを **Range 要求で一部だけ読む**（W6 の PR D、W6-03）。

**配信が読むプロファイルは 1 ファイル 33.6 MB で、1 周期に要るのはその 5% ほど**（7〜8 行群。
W6 プランの所見 204）。GET 1 回でファイル全体を落とす読み口（`SupabaseIo.download`）では、
並びを揃えても部分読みが効かない（所見 187）。ここは 2 段で読む。

  1. **末尾**（`bytes=-65536`）で、全体の大きさ（`Content-Range`）とフッタを取る。フッタが
     長ければ（PR C の版は 263 KB）、足りないぶんをもう 1 回
  2. フッタのメタデータから**選んだ行群のバイト範囲**を求め、隣り合う範囲はまとめて、並行に取る

pyarrow には**取った範囲だけを持つファイル**（`SparseFile`）として渡す。**取っていない範囲を
読まれたら止める**——黙って 0 を返すと、壊れた値を読む。

**版が読みの途中で入れ替わっても混ぜない。** Storage の応答は Cloudflare のキャッシュを通る
（2026-09-27 に本番で `cf-cache-status: HIT`・`age` 約 1 時間 45 分）。置き直しの直後は、要求ごとに
古い版と新しい版が返りうるので、**応答を受け取るたびに ETag と大きさを最初の末尾と比べ**
（フッタを pyarrow に渡す前に）、**揃わなければ 1 度だけ最初から読み直し、それでも揃わなければ
止める**（`ChangedWhileReadingError`）。

**HTTP は知らない。** 取るのは `FetchesRanges`（`SupabaseIo.download_range`）で、ここは範囲を
決めて組み立てるだけである——だから偽の取り口で、範囲・まとめ方・版の食い違いを検査できる。
"""

import io
import struct
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from functools import reduce
from typing import Final, Protocol

import pyarrow as pa
import pyarrow.parquet as pq

from bikechance_ml.io import fanout

#: 末尾から最初に取る長さ。**pyarrow が最初に末尾から読む長さ（64 KiB）と同じ**にする——
#: 短いと、pyarrow の最初の読みが取っていない範囲に掛かる。
TAIL_PROBE_B: Final[int] = 64 * 1024

#: Parquet の末尾の 8 バイト：フッタの長さ（4 バイト・リトルエンディアン）と目印 `PAR1`。
TRAILER_B: Final[int] = 8
FOOTER_LENGTH_FORMAT: Final[str] = "<I"
PARQUET_MAGIC: Final[bytes] = b"PAR1"

#: 行群の範囲を並行に取る数。1 周期は多くて 8 範囲（隣り合う行群はまとめる）。
FETCH_WORKERS: Final[int] = 4

#: フッタのメタデータを見て、読む行群の番号を返す関数（`profile.wanted_groups` を包んだもの）。
type ChooseGroups = Callable[[pq.FileMetaData], Sequence[int]]


class RangeFileError(RuntimeError):
    """部分読みで止まった。**埋めて進まない**（下の 3 つの親）。"""


class NotFetchedError(RangeFileError):
    """取っていない範囲を読もうとした。**0 で埋めて返さない。**"""


class CorruptFileError(RangeFileError):
    """末尾が Parquet の形でない、または頼んだ範囲を含まない応答が返った。"""


class ChangedWhileReadingError(RangeFileError):
    """読んでいる途中で版が入れ替わった（ETag か大きさが揃わない、途中で消えた）。"""


@dataclass(frozen=True)
class Piece:
    """Range 要求 1 回の応答。**どこから始まる何バイトか・全体の大きさ・版（ETag）。**"""

    start: int
    body: bytes
    size: int
    etag: str | None

    @property
    def stop(self) -> int:
        return self.start + len(self.body)


class FetchesRanges(Protocol):
    """Range 要求を送る口。**無ければ None**（`SupabaseIo.download_range`）。"""

    def download_range(self, bucket: str, path: str, byte_range: str) -> Piece | None: ...


@dataclass(frozen=True)
class Span:
    """ファイルの中の半開区間 `[start, stop)`。"""

    start: int
    stop: int

    def header(self) -> str:
        """`Range` の見出しの値。**終わりを含む**書き方にする（`bytes=0-9` は 10 バイト）。"""
        return f"bytes={self.start}-{self.stop - 1}"


@dataclass(frozen=True)
class RangeRead:
    """部分読みの結果。**落としたバイト数と要求の数も返す**（`inference_log.detail` に出す）。"""

    table: pa.Table
    bytes_read: int
    requests: int


class SparseFile:
    """**取った範囲だけを持つ**、読み取り専用のファイル（pyarrow に渡す）。

    取った範囲は重なりや継ぎ目をまとめて持つので、1 回の読みが 2 つの応答にまたがってもよい。
    **取っていない範囲を読まれたら `NotFetchedError`**——0 で埋めて返さない。
    """

    #: pyarrow が開く前に見る。
    closed = False

    def __init__(self, size: int, pieces: Sequence[Piece]) -> None:
        self._size = size
        self._held = joined(pieces)
        self._position = 0

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        origin = {io.SEEK_SET: 0, io.SEEK_CUR: self._position, io.SEEK_END: self._size}[whence]
        self._position = origin + offset
        return self._position

    def tell(self) -> int:
        return self._position

    def read(self, size: int = -1) -> bytes:
        stop = self._size if size < 0 else min(self._size, self._position + size)
        chunk = self._slice(self._position, stop)
        self._position = max(self._position, stop)
        return chunk

    def _slice(self, start: int, stop: int) -> bytes:
        if stop <= start:
            return b""
        found = next((one for one in self._held if one.start <= start and stop <= one.stop), None)
        if found is None:
            raise NotFetchedError(f"取っていない範囲 [{start}, {stop}) を読もうとした")
        return found.body[start - found.start : stop - found.start]


def joined(pieces: Sequence[Piece]) -> tuple[Piece, ...]:
    """重なり・継ぎ目をまとめる（始まりの順）。**重なったところは先に来た応答のバイトを使う。**"""
    return reduce(_join, sorted(pieces, key=lambda one: one.start), ())


def _join(held: tuple[Piece, ...], piece: Piece) -> tuple[Piece, ...]:
    if not held or piece.start > held[-1].stop:
        return (*held, piece)
    last = held[-1]
    return (*held[:-1], replace(last, body=last.body + piece.body[last.stop - piece.start :]))


# ── 読む ──────────────────────────────────────────────────────
def read_row_groups(
    source: FetchesRanges, bucket: str, path: str, choose: ChooseGroups
) -> RangeRead | None:
    """`choose` がフッタを見て選んだ行群だけを読む。**オブジェクトが無ければ None。**

    版が読みの途中で入れ替わったら、**1 度だけ**最初から読み直す（キャッシュが新しい版に
    揃うまでの短い間）。2 度目も揃わなければ `ChangedWhileReadingError` のまま止める。
    """
    try:
        return _read_once(source, bucket, path, choose)
    except ChangedWhileReadingError:
        return _read_once(source, bucket, path, choose)


def _read_once(
    source: FetchesRanges, bucket: str, path: str, choose: ChooseGroups
) -> RangeRead | None:
    tail = source.download_range(bucket, path, f"bytes=-{TAIL_PROBE_B}")
    if tail is None:
        return None
    footer = _with_footer(source, bucket, path, tail)
    metadata = pq.ParquetFile(SparseFile(tail.size, footer)).metadata
    groups = tuple(choose(metadata))
    spans = _uncovered(coalesced([group_span(metadata.row_group(one)) for one in groups]), footer)
    pieces = (*footer, *_fetch(source, bucket, path, spans, tail))
    return RangeRead(
        table=_read_table(SparseFile(tail.size, pieces), groups),
        bytes_read=sum(len(one.body) for one in pieces),
        requests=len(pieces),
    )


def _with_footer(source: FetchesRanges, bucket: str, path: str, tail: Piece) -> tuple[Piece, ...]:
    """末尾の応答に、**フッタの足りないぶん**を足す（フッタが末尾の応答より長いとき）。"""
    footer_start = tail.size - TRAILER_B - footer_length(tail)
    if footer_start < 0:
        raise CorruptFileError("フッタの長さがファイルより長い")
    if footer_start >= tail.start:
        return (tail,)
    return (tail, _fetch_span(source, bucket, path, Span(footer_start, tail.start), tail))


def footer_length(tail: Piece) -> int:
    """末尾の 8 バイトから、フッタの長さを読む。**末尾が `PAR1` でなければ止める。**"""
    trailer = tail.body[-TRAILER_B:]
    if tail.stop != tail.size or len(trailer) < TRAILER_B or trailer[-4:] != PARQUET_MAGIC:
        raise CorruptFileError("末尾が Parquet の形でない")
    (length,) = struct.unpack(FOOTER_LENGTH_FORMAT, trailer[: TRAILER_B - len(PARQUET_MAGIC)])
    return int(length)


def group_span(group: pq.RowGroupMetaData) -> Span:
    """行群 1 つのバイト範囲。**列の断片はつながって並ぶ**ので、最初の始まりから最後の終わり。"""
    chunks = [group.column(index) for index in range(group.num_columns)]
    starts = [_chunk_start(one) for one in chunks]
    stops = [
        start + int(one.total_compressed_size) for start, one in zip(starts, chunks, strict=True)
    ]
    return Span(min(starts), max(stops))


def _chunk_start(chunk: pq.ColumnChunkMetaData) -> int:
    """列の断片の始まり。**辞書のページが在ればそこから**（データのページより前に置かれる）。"""
    if chunk.has_dictionary_page:
        return int(chunk.dictionary_page_offset)
    return int(chunk.data_page_offset)


def coalesced(spans: Sequence[Span]) -> tuple[Span, ...]:
    """継ぎ目の無い範囲を 1 つにまとめる。**間の行群は取らない**（下りの予算を守る）。"""
    return reduce(_merge_span, sorted(spans, key=lambda one: one.start), ())


def _merge_span(merged: tuple[Span, ...], span: Span) -> tuple[Span, ...]:
    if merged and span.start <= merged[-1].stop:
        return (*merged[:-1], Span(merged[-1].start, max(merged[-1].stop, span.stop)))
    return (*merged, span)


def _uncovered(spans: Sequence[Span], held: Sequence[Piece]) -> tuple[Span, ...]:
    """まだ取っていない範囲。**末尾の応答が行群まで覆う小さなファイル**では取り直さない。"""
    pieces = joined(held)
    return tuple(
        span
        for span in spans
        if not any(one.start <= span.start and span.stop <= one.stop for one in pieces)
    )


def _fetch(
    source: FetchesRanges, bucket: str, path: str, spans: Sequence[Span], first: Piece
) -> tuple[Piece, ...]:
    return fanout.gather(
        lambda span: _fetch_span(source, bucket, path, span, first), spans, workers=FETCH_WORKERS
    )


def _fetch_span(source: FetchesRanges, bucket: str, path: str, span: Span, first: Piece) -> Piece:
    """範囲を 1 つ取る。**版は受け取ったその場で、最初の末尾と比べる**（範囲や中身より先に）。

    後でまとめて比べると、フッタの残りが違う版だったとき、pyarrow が混ざったフッタを読んで
    `OSError` で止まり、読み直しに入らない（2026-09-27 に再現）。**途中で消えたときも、
    範囲の欠けた応答が違う版だったときも、入れ替わりとして扱う。**
    """
    piece = source.download_range(bucket, path, span.header())
    if piece is None:
        raise ChangedWhileReadingError("読んでいる途中でオブジェクトが消えた")
    _require_one_version((first, piece))
    if piece.start > span.start or piece.stop < span.stop:
        raise CorruptFileError("頼んだ範囲を含まない応答が返った")
    return piece


def _require_one_version(pieces: Sequence[Piece]) -> None:
    """**受け取った応答が、すべて同じ版**であること（ETag と全体の大きさ）。"""
    if len({(one.etag, one.size) for one in pieces}) > 1:
        raise ChangedWhileReadingError(
            "読んでいる途中で版が入れ替わった（ETag か大きさが揃わない）"
        )


def _read_table(file: SparseFile, groups: Sequence[int]) -> pa.Table:
    """選んだ行群を読む。**1 つも選ばなければ、列だけの空の表。**

    `pre_buffer` を切る：先読みは近い範囲をまとめて読みに来るので、**取っていない隙間**に
    掛かることがある。バイトはもう手元に在るので、先読みで得るものは無い。
    """
    parquet = pq.ParquetFile(file, pre_buffer=False)
    if not groups:
        return parquet.schema_arrow.empty_table()
    return parquet.read_row_groups(list(groups))
