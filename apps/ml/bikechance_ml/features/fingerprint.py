"""学習サンプルのバイト列の素性（W6-17、開発プラン §7.5）。

**パスだけでは「どのバイト列から当てはめたか」が残らない。** `features/` は同じパスで
作り直すことがある（J1 で全日を v4・一様 1% に作り直した）。だから当てはめに使った日ごとに、
**読んだ Parquet のバイト列そのもの**の SHA-256 と行数を、登録簿とモデルカードに残す。
`experiments/` は作らない（W6-17。登録簿とカードで代える）。

数えるのは `jobs/window.py`（読んだ人が、読んだものの素性を持って帰る）。
"""

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Fingerprint:
    """1 日ぶんの学習サンプルの素性。"""

    rows: int
    #: 読んだ Parquet のバイト列の SHA-256（小文字の 16 進 64 桁）
    sha256: str


def of(body: bytes, rows: int) -> Fingerprint:
    """バイト列の SHA-256 と、その日の行数。"""
    return Fingerprint(rows=rows, sha256=hashlib.sha256(body).hexdigest())
