"""入出力を並行に呼ぶ道具（`io/fanout.py`）。

**ここはリポジトリで唯一スレッドを作る場所**なので、約束を全部テストで留める。

  * **入力と同じ順で返す**（終わった順ではない）
  * **本当に重なる**（直列なら間に合わない時間で終わる）
  * **最初の例外がそのまま出る**（止める合図を握り潰さない）
  * **例外のあと、並んでいるぶんは走らない**（止まるのが遅れない）
  * **1 本ならスレッドを作らない**（試験と小さな入力は今までの道を通る）

時間に頼る検査が 2 つある（重なること・並んでいるぶんを捨てること）。**余裕を大きく
取る**——遅い機械で落ちる検査は、そのうち誰も読まなくなる。
"""

import threading
import time

import pytest

from bikechance_ml.io.fanout import gather

#: 1 件あたりの作り物の待ち時間。**本物の往復の代わり。**
DELAY_S = 0.05

#: 重なっているとみなす上限。直列なら 8 件 × 0.05 = 0.40 秒かかる。
OVERLAP_LIMIT_S = 0.25


def test_the_results_come_back_in_the_order_of_the_input() -> None:
    """**終わった順ではなく、渡した順。** 先の要素をわざと遅くして確かめる。"""

    def slow_first(one: int) -> int:
        time.sleep(DELAY_S if one == 0 else 0.0)
        return one * 10

    assert gather(slow_first, list(range(8)), workers=4) == (0, 10, 20, 30, 40, 50, 60, 70)


def test_the_calls_actually_overlap() -> None:
    """**待ちが重なる。** 直列なら 0.40 秒かかるものが、4 本なら 0.10 秒台で終わる。"""

    def wait(one: int) -> int:
        time.sleep(DELAY_S)
        return one

    started = time.monotonic()
    assert gather(wait, list(range(8)), workers=4) == tuple(range(8))
    assert time.monotonic() - started < OVERLAP_LIMIT_S


def test_more_workers_than_items_is_fine() -> None:
    """**要素より多い本数を渡しても落ちない**（`min` で丸める）。"""
    assert gather(lambda one: one + 1, [1, 2], workers=64) == (2, 3)


def test_an_empty_input_gives_an_empty_result() -> None:
    assert gather(lambda one: one, [], workers=8) == ()


def test_the_first_exception_comes_out() -> None:
    """**止める合図を握り潰さない。** `TruncatedListingError` のような例外が要る。"""

    class RefusedError(RuntimeError):
        pass

    def fail_on_three(one: int) -> int:
        if one == 3:
            raise RefusedError("3 は読めません")
        return one

    with pytest.raises(RefusedError, match="3 は読めません"):
        gather(fail_on_three, list(range(8)), workers=2)


def test_the_queued_work_is_dropped_after_a_failure() -> None:
    """**例外のあと、まだ始まっていないぶんは走らない。**

    走らせてしまうと、1 件 30 秒の往復が残り全部ぶん積まれる。**止まるのが遅れる**のは
    `maxDuration` の中では失敗と同じである。
    """
    seen: list[int] = []
    lock = threading.Lock()

    def fail_first(one: int) -> int:
        with lock:
            seen.append(one)
        if one == 0:
            raise ValueError("最初で止める")
        time.sleep(DELAY_S)
        return one

    started = time.monotonic()
    with pytest.raises(ValueError, match="最初で止める"):
        gather(fail_first, list(range(64)), workers=2)
    # 走り出すのは 2 本ぶんの数件だけ。64 件ぜんぶは通らない
    assert len(seen) < 64
    assert time.monotonic() - started < OVERLAP_LIMIT_S


def test_a_single_worker_does_not_start_a_thread() -> None:
    """**1 本ならスレッドを作らない。** 呼び出し元と同じスレッドで走る。"""
    here = threading.current_thread().name
    assert gather(lambda one: threading.current_thread().name, [1, 2, 3], workers=1) == (
        here,
        here,
        here,
    )


def test_a_single_item_does_not_start_a_thread() -> None:
    """要素が 1 つなら、本数を増やしてもスレッドは要らない。"""
    here = threading.current_thread().name
    assert gather(lambda one: threading.current_thread().name, [1], workers=16) == (here,)


def test_several_items_do_run_on_worker_threads() -> None:
    """**逆も固定する。** 2 件以上・2 本以上なら、呼び出し元の外で走る。"""
    here = threading.current_thread().name
    names = gather(lambda one: threading.current_thread().name, list(range(8)), workers=4)
    assert here not in names
    assert all(name.startswith("bikechance-io") for name in names)
