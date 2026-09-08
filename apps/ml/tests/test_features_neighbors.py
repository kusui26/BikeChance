"""近傍の集約（`features/neighbors.py`、W3 プラン §9.5）。

**この 1 ファイルの主題は 2 つ。**

  * **近傍 0 は実データ**（300 m で 27.8%、500 m で 10.2%）。合計は 0、平均は NULL
  * **事業者を跨ぐ組がある。** 「全部」と「同一システムのみ」を取り違えない
"""

import numpy as np

from bikechance_ml.features.neighbors import (
    NeighborLinks,
    count_over_neighbors,
    mean_over_neighbors,
    observed_nan,
    observed_or_zero,
    sum_over_neighbors,
    to_links,
)
from bikechance_ml.features.reference import NeighborRow, SystemReference

#: 4 ポート。0 と 1 は 80 m、0 と 2 は 400 m（別システム）、3 は孤立。
LINKS = NeighborLinks(
    source=np.array([0, 0, 1, 2], dtype=np.int32),
    target=np.array([1, 2, 0, 0], dtype=np.int32),
    distance_m=np.array([80, 400, 80, 400], dtype=np.int16),
    same_system=np.array([True, False, True, False], dtype=np.bool_),
    n_stations=4,
)

#: `(ポート, 基準時刻)` の台数。基準時刻は 2 点。
BIKES = np.array([[1, 1], [2, 3], [4, 5], [9, 9]], dtype=np.int32)


def test_counts_include_zero_for_isolated_ports() -> None:
    """**近傍 0 は 0 のまま。** 欠損にしない。"""
    assert LINKS.counts().tolist() == [2, 1, 1, 0]


def test_sum_is_zero_for_isolated_ports() -> None:
    """`reduceat` の穴：空の区間で隣の値が紛れ込まないこと。"""
    assert sum_over_neighbors(LINKS, BIKES).tolist() == [[6, 8], [1, 1], [1, 1], [0, 0]]


def test_radius_filter_uses_distance() -> None:
    """300 m は `distance_m` で絞る。**表を 2 つ持たない。**"""
    near = LINKS.within(300, same_system_only=False)
    assert near.counts().tolist() == [1, 1, 0, 0]
    assert sum_over_neighbors(near, BIKES).tolist() == [[2, 3], [1, 1], [0, 0], [0, 0]]


def test_same_system_filter_drops_the_other_operator() -> None:
    """HELLO とドコモは別事業者で、利用者はふつう乗り換えられない。"""
    same = LINKS.within(500, same_system_only=True)
    assert same.counts().tolist() == [1, 1, 0, 0]


def test_count_over_neighbors_counts_matching_ports() -> None:
    empty = np.array([[False, True], [True, False], [False, False], [True, True]], dtype=np.bool_)
    assert count_over_neighbors(LINKS, empty).tolist() == [[1, 0], [0, 1], [0, 1], [0, 0]]


def test_mean_is_null_when_no_neighbour_has_a_value() -> None:
    """**平均は 0 で埋めない。** 「周りが空」と「周りに何も無い」を混ぜない。"""
    values = np.array([[1.0, 1.0], [0.5, np.nan], [np.nan, np.nan], [1.0, 1.0]])
    mean = mean_over_neighbors(LINKS, values)
    assert mean[0, 0] == 0.5
    assert np.isnan(mean[0, 1])
    assert np.isnan(mean[3, 0])


def test_unobserved_neighbours_do_not_contribute_minus_one() -> None:
    """`-1` をそのまま足すと合計が減る。**足す前に 0 に、平均では NaN にする。**"""
    raw = np.array([[-1, 3]], dtype=np.int16)
    assert observed_or_zero(raw).tolist() == [[0, 3]]
    assert np.isnan(observed_nan(raw)[0, 0])


# ── 参照データからの組み立て ──────────────────────────────────
def test_to_links_maps_ids_to_positions_across_systems() -> None:
    """**近傍はシステムを跨ぐ。** 相手のシステムまで見て位置を引く。"""
    systems = (
        SystemReference(
            "hellocycling",
            (),
            (),
            (
                NeighborRow("a", "hellocycling", "b", 80, True),
                NeighborRow("a", "docomo-cycle", "x", 250, False),
            ),
        ),
        SystemReference(
            "docomo-cycle", (), (), (NeighborRow("x", "hellocycling", "a", 250, False),)
        ),
    )
    keys = (("hellocycling", "a"), ("hellocycling", "b"), ("docomo-cycle", "x"))
    links = to_links(systems, keys)
    assert links.source.tolist() == [0, 0, 2]
    assert links.target.tolist() == [1, 2, 0]
    assert links.same_system.tolist() == [True, False, False]


def test_to_links_drops_rows_whose_partner_is_not_in_the_ledger() -> None:
    """台帳が正。**位置の無い相手は置き場所が無い。**"""
    systems = (
        SystemReference(
            "hellocycling", (), (), (NeighborRow("a", "hellocycling", "gone", 80, True),)
        ),
    )
    links = to_links(systems, (("hellocycling", "a"),))
    assert len(links.source) == 0
    assert links.counts().tolist() == [0]


def test_to_links_is_sorted_by_source() -> None:
    """区間で畳むので、**`source` の昇順**であることが前提になる。"""
    systems = (
        SystemReference(
            "hellocycling",
            (),
            (),
            (
                NeighborRow("c", "hellocycling", "a", 80, True),
                NeighborRow("a", "hellocycling", "c", 80, True),
            ),
        ),
    )
    keys = (("hellocycling", "a"), ("hellocycling", "b"), ("hellocycling", "c"))
    links = to_links(systems, keys)
    assert links.source.tolist() == sorted(links.source.tolist())
