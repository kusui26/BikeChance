"""当てはめる期間の決め方（`jobs/fit_baseline.py` の `window`）。

**主題は「期間を書かずに走らせても、狙った日が読まれること」**（W5 の PR M、W5-16）。
当てはめ直しを日課にするなら `--from` と `--to` を毎回書いていられないが、**既定が
静かにずれると、古い日で作った成果物を配ることになる**——それは名前（`-YYYYMMDD`）
にも出ないので、気づく手立てが無い。だから既定の境目をここで固定する。

**UTC と JST の境目を 2 点で挟む。** `now` は UTC で渡ってくるが、暦日は JST で切る。
`now.date()` と書いてしまうと **JST の朝 9 時より前に「一昨日まで」になる**——日次の
当てはめ直しは 08:30 JST 以降に走らせる想定なので、まさにその時間帯で外れる。
"""

from datetime import UTC, date, datetime

import pytest

from bikechance_ml.jobs.fit_baseline import DEFAULT_TRAIN_DAYS, WindowError, run, window

#: JST の 2026-09-14 08:30（＝当てはめ直しを走らせる時刻）。**UTC ではまだ 09-13。**
MORNING = datetime(2026, 9, 13, 23, 30, tzinfo=UTC)


def test_the_default_window_ends_yesterday_in_jst() -> None:
    """**既定は「昨日まで」**。当日の `features/` はまだ無い（翌朝に作られる）。"""
    first, last = window(start=None, end=None, days=None, now=MORNING)
    assert last == date(2026, 9, 13)
    assert first == date(2026, 9, 7)


def test_the_default_window_is_seven_days_inclusive() -> None:
    """**両端を含めて 7 日。** 9/7〜9/13 は 7 日（6 ではない）。

    **数え方を `DEFAULT_TRAIN_DAYS` から作らない。** 定数から期待値を作ると、定数を
    変えたときに試験も一緒に動いて何も守らなくなる（W5 §12 の 154 と同じ落とし穴）。
    """
    assert DEFAULT_TRAIN_DAYS == 7
    first, last = window(start=None, end=None, days=None, now=MORNING)
    assert (last - first).days + 1 == 7


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # JST 09-13 23:59 — **UTC ではもう 09-13 14:59**。UTC で切ると「09-12 まで」になる
        (datetime(2026, 9, 13, 14, 59, tzinfo=UTC), date(2026, 9, 12)),
        # JST 09-14 00:00 — 日が変わった最初の 1 分
        (datetime(2026, 9, 13, 15, 0, tzinfo=UTC), date(2026, 9, 13)),
    ],
)
def test_yesterday_is_measured_in_jst(now: datetime, expected: date) -> None:
    """**暦日は JST で切る。** 9 時間ぶんの取り違えは、まるまる 1 日ぶんの差になる。"""
    _, last = window(start=None, end=None, days=None, now=now)
    assert last == expected


def test_days_counts_back_from_the_end() -> None:
    """`--days` は **`--to` から遡る**。1 を渡したら 1 日（0 日でも 2 日でもない）。"""
    assert window(start=None, end="2026-09-12", days=1, now=MORNING) == (
        date(2026, 9, 12),
        date(2026, 9, 12),
    )
    assert window(start=None, end="2026-09-12", days=3, now=MORNING) == (
        date(2026, 9, 10),
        date(2026, 9, 12),
    )


def test_days_without_an_end_counts_back_from_yesterday() -> None:
    """`--days` だけを渡したら、**昨日から遡る**（`--to` の既定と噛み合う）。"""
    assert window(start=None, end=None, days=2, now=MORNING) == (
        date(2026, 9, 12),
        date(2026, 9, 13),
    )


def test_explicit_dates_are_used_as_given() -> None:
    """両端を書いたら、そのまま。**既定は一切混ざらない。**"""
    assert window(start="2026-09-07", end="2026-09-12", days=None, now=MORNING) == (
        date(2026, 9, 7),
        date(2026, 9, 12),
    )


def test_from_and_days_cannot_be_combined() -> None:
    """**どちらも「始まり」を決める。** 片方を黙って無視したら、読む日が変わる。"""
    with pytest.raises(WindowError):
        window(start="2026-09-07", end=None, days=3, now=MORNING)


def test_a_backwards_window_is_refused() -> None:
    """始まりが終わりより後なら止める（`load_days` は空の並びを黙って受ける）。"""
    with pytest.raises(WindowError):
        window(start="2026-09-13", end="2026-09-12", days=None, now=MORNING)


def test_a_single_day_window_is_allowed() -> None:
    """同じ日を両端に置くのは正しい指定（1 日で当てはめる）。"""
    assert window(start="2026-09-12", end="2026-09-12", days=None, now=MORNING) == (
        date(2026, 9, 12),
        date(2026, 9, 12),
    )


def test_zero_or_negative_days_is_refused() -> None:
    """**0 日の窓は作らない。** `--days 0` は「始まりが終わりの翌日」になって止まる。"""
    for bad in (0, -3):
        with pytest.raises(WindowError):
            window(start=None, end="2026-09-12", days=bad, now=MORNING)


def test_the_command_refuses_before_it_opens_storage() -> None:
    """**期間を見るのは Storage を開く前**（`run` の並び順）。

    環境変数が無くても 2 で返ることが、その並びの証拠になる——`open_storage` に
    届いていたら `read_storage_config` が別の失敗を出す。
    """
    assert run(["--from", "2026-09-07", "--days", "3"]) == 2
