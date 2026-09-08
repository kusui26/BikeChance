"""除外規則（`features/exclude.py`、開発プラン §6.1、データ辞書 §10.1）。

**この 1 ファイルの主題は「`t` 時点で判定できるものだけを使うこと」と、
「内訳の合計が落とした行数に一致すること」。**
"""

import numpy as np

from bikechance_ml.features.arrays import Bools
from bikechance_ml.features.asof import NO_ROW
from bikechance_ml.features.constants import MAX_STALENESS_S
from bikechance_ml.features.exclude import (
    exclude_at_base,
    exclude_at_target,
    phantom_mask,
    too_old,
)

OPEN, SUSPENDED, MISSING = 7, 1, -1
MINUTE_MS = 60_000


def base(
    *,
    rows: list[int],
    bikes: list[int],
    docks: list[int] | None = None,
    flags: list[int] | None = None,
    ages_s: list[int] | None = None,
    phantom: list[bool] | None = None,
) -> tuple[Bools, dict[str, int]]:
    """1 ポート × N 格子点の除外を計算する。"""
    count = len(rows)
    grid = np.arange(count, dtype=np.int64)[None, :] * MINUTE_MS
    age = np.array([ages_s or [0] * count], dtype=np.int64) * 1000
    result = exclude_at_base(
        feature_row=np.array([rows], dtype=np.int32),
        observed_at_ms=grid - age,
        grid_ms=grid,
        bikes=np.array([bikes], dtype=np.int16),
        docks=np.array([docks or [5] * count], dtype=np.int16),
        flags=np.array([flags or [OPEN] * count], dtype=np.int16),
        phantom=np.array(phantom or [False], dtype=np.bool_),
    )
    return result.keep, result.counts


def test_missing_asof_is_excluded() -> None:
    keep, counts = base(rows=[NO_ROW, 0], bikes=[3, 3])
    assert keep.tolist() == [[False, True]]
    assert counts["no_asof_at_t"] == 1


def test_stale_observation_is_excluded() -> None:
    """**600 秒より古い観測は欠損として扱う**（W3 プラン §9.3）。"""
    keep, _ = base(rows=[0, 0], bikes=[3, 3], ages_s=[MAX_STALENESS_S, MAX_STALENESS_S + 1])
    assert keep.tolist() == [[True, False]]


def test_unobserved_value_is_excluded() -> None:
    """`-1` は「観測されなかった」。**0 ではない**（データ辞書 §2 の (3)）。"""
    keep, counts = base(rows=[0, 0, 0], bikes=[3, MISSING, 3], docks=[5, 5, MISSING])
    assert keep.tolist() == [[True, False, False]]
    assert counts["unobserved_at_t"] == 2


def test_suspended_station_is_excluded_at_the_base_time() -> None:
    keep, counts = base(rows=[0, 0], bikes=[3, 3], flags=[OPEN, SUSPENDED])
    assert keep.tolist() == [[True, False]]
    assert counts["suspended_at_t"] == 1


def test_phantom_station_is_excluded_by_id_not_by_name() -> None:
    """実測で「日本バプテスト京都教会」が `%テスト%` に一致した（データ辞書 §10.1）。"""
    assert phantom_mask([("docomo-cycle", "5753")]).tolist() == [True]
    assert phantom_mask([("hellocycling", "5753")]).tolist() == [False]
    keep, counts = base(rows=[0, 0], bikes=[3, 3], phantom=[True])
    assert not keep.any()
    assert counts["phantom"] == 2


def test_reasons_are_counted_once_each() -> None:
    """**優先順に 1 つだけ数える。** 内訳の合計が落とした行数と一致する。"""
    keep, counts = base(
        rows=[NO_ROW, 0, 0], bikes=[MISSING, MISSING, 3], flags=[SUSPENDED, SUSPENDED, OPEN]
    )
    assert int((~keep).sum()) == sum(counts.values()) == 2
    assert counts["no_asof_at_t"] == 1
    assert counts["unobserved_at_t"] == 1
    assert counts["suspended_at_t"] == 0


# ── 目標時刻の側 ──────────────────────────────────────────────
def target(
    *, feature: list[int], label: list[int], bikes: list[int], alive: list[bool] | None = None
) -> tuple[Bools, dict[str, int]]:
    count = len(label)
    grid = np.arange(count, dtype=np.int64)[None, :] * MINUTE_MS
    result = exclude_at_target(
        alive=np.array([alive or [True] * count], dtype=np.bool_),
        feature_row=np.array([feature], dtype=np.int32),
        label_row=np.array([label], dtype=np.int32),
        observed_at_ms=grid,
        target_ms=grid,
        bikes=np.array([bikes], dtype=np.int16),
        docks=np.array([[5] * count], dtype=np.int16),
    )
    return result.keep, result.counts


def test_collision_is_excluded() -> None:
    """`as-of(t+h)` が `as-of(t)` と同じ観測なら落とす（W3-12。実測 0 件）。"""
    keep, counts = target(feature=[3, 3], label=[3, 4], bikes=[1, 1])
    assert keep.tolist() == [[False, True]]
    assert counts["collision"] == 1


def test_missing_label_asof_is_excluded_and_not_a_collision() -> None:
    """**どちらも `NO_ROW` でも衝突ではない。** 順に見るので二重に数えない。"""
    keep, counts = target(feature=[NO_ROW], label=[NO_ROW], bikes=[1])
    assert not keep.any()
    assert counts["no_asof_at_target"] == 1
    assert counts["collision"] == 0


def test_rows_already_dropped_at_the_base_are_not_counted_again() -> None:
    """基準側で落ちた行を目標側でも数えると、内訳が母数を超える。"""
    _, counts = target(feature=[3], label=[3], bikes=[1], alive=[False])
    assert sum(counts.values()) == 0


def test_too_old_ignores_rows_without_an_observation() -> None:
    at = np.array([[600_000]], dtype=np.int64)
    observed = np.array([[0]], dtype=np.int64)
    assert too_old(at, observed, np.array([[NO_ROW]], dtype=np.int32)).tolist() == [[False]]
    assert too_old(at, observed, np.array([[0]], dtype=np.int32)).tolist() == [[False]]
    assert too_old(at + 1000, observed, np.array([[0]], dtype=np.int32)).tolist() == [[True]]
