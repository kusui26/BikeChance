"""入出力を並行に呼ぶ（W5 プラン §12 の 169）。

**リポジトリでスレッドを作るのはここだけ。** 1 か所に閉じ込めるのは、並行にしてよい
条件（呼ぶ相手がスレッド安全で、副作用の順序に意味が無いこと）を**呼び出しごとに
考え直さずに済ませる**ためである。使う側は `gather` に関数と入力を渡すだけでよい。

**なぜ要るのか。** `/ml/evaluate` は 1 日ぶんの予測ログを読む——2 システムで
**576 ファイル・84 MB**、そのうえ実測の Parquet が 58 時間ぶんある。1 往復を
直列に積むと、往復の待ち時間だけで `maxDuration` の 240 秒を超える
（2026-09-17 の初回がそれで殺された。§12 の 169）。**計算ではなく待ちが支配的**なので、
スレッドで待ちを重ねれば済む。

守っている約束は 3 つ。

  * **入力と同じ順で返す**。並びが走るたびに変わると、要約の差分が読めなくなる
  * **最初の例外がそのまま出る**。`TruncatedListingError` のような「測るのをやめる」
    合図を握り潰さない
  * **1 本ならスレッドを作らない**。試験と小さな入力は今までと同じ道を通る
"""

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Final

#: スレッドに付ける名前の接頭辞。**実行ログで出どころが分かるように。**
THREAD_PREFIX: Final[str] = "bikechance-io"


def gather[T, R](work: Callable[[T], R], items: Sequence[T], *, workers: int) -> tuple[R, ...]:
    """`work` を並行に呼び、**入力と同じ順**で返す。

    `work` は**スレッド安全でなければならない**。本番で渡すのは `SupabaseIo` の
    読み取りで、中身は `httpx.Client`（スレッド安全。接続プールは既定で 100 本）を
    要求ごとに叩くだけである。**書き込みはここに通さない**——順序と冪等性の議論が
    増えるわりに、書き込みは 1 日 1 回・数回しかない。

    `workers` は**同時に開く接続の数**。プールの上限より十分に小さく取る。
    """
    if workers <= 1 or len(items) <= 1:
        return tuple(work(one) for one in items)
    with ThreadPoolExecutor(
        max_workers=min(workers, len(items)), thread_name_prefix=THREAD_PREFIX
    ) as pool:
        # **`map` が 3 つとも面倒を見る。** 入力の順に返し、最初の例外をそのまま送出し、
        # **例外で抜けるときは並んでいる未着手ぶんを自分で取り消す**（CPython の
        # `Executor.map` は結果を返す生成器の `finally` で `future.cancel()` する。
        # 2026-09-17 に実装を読んで確かめた）。だから `shutdown` に `cancel_futures`
        # を足しても何も変わらない——**効かない belt は置かない。**
        # 抜けるときに待つのは、すでに走り出している高々 `workers` 本だけである。
        return tuple(pool.map(work, items))
