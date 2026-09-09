"""除外規則（開発プラン §6.1、W3 プラン §9.3、データ辞書 §10.1）。

**`t` 時点で判定できるものだけを使う。** `stations.is_active` で除外すると、日次
ジョブが上書きする「現在の」状態が過去のサンプルに遡って効き、生存者バイアスが入る。
`capacity = 0` でも除外しない（ドコモの `capacity` は動的値で、任意の一瞬に 0 の
ポートが常に 4.0% ある）。

**補間しない。** 欠損は欠損のまま落とす。

理由は**優先順に 1 つだけ**数える。同じ行が複数の理由に当たることは普通にあるので、
足し上げて母数を超えないようにしておくほうが、内訳として読める。
"""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from bikechance_ml.features.arrays import Bools, Int16, Int32, Int64
from bikechance_ml.features.asof import NO_ROW
from bikechance_ml.features.constants import (
    FLAG_RENTING,
    FLAG_RETURNING,
    MAX_STALENESS_S,
    MISSING,
    PHANTOM_STATIONS,
)

_MS_PER_SECOND = 1000


@dataclass(frozen=True)
class Excluded:
    """残す行と、落とした理由の内訳。"""

    keep: Bools
    counts: dict[str, int]

    def total_dropped(self) -> int:
        return sum(self.counts.values())


def phantom_mask(keys: Sequence[tuple[str, str]]) -> Bools:
    """実在しないポート（EDA #1 のドコモ `5753`）。**名前では弾かない。**

    実測で「日本バプテスト京都教会」が `%テスト%` に一致した。値の妥当性か、
    明示した ID の一覧で弾く（データ辞書 §10.1）。
    """
    return np.array([key in PHANTOM_STATIONS for key in keys], dtype=np.bool_)


def exclude_at_base(
    *,
    feature_row: Int32,
    observed_at_ms: Int64,
    grid_ms: Int64,
    bikes: Int16,
    docks: Int16,
    flags: Int16,
    phantom: Bools,
    alive: Bools | None = None,
) -> Excluded:
    """基準時刻 `t` の側の除外。行列の形は `(ポート, 基準時刻)`。

    `alive` を渡すと、そこから始める。**推論はこれで自系統だけに絞る**（台帳は
    2 システムを 1 つにまとめてあり、他系統は近傍のためだけに居る。W4 プラン §6.3）。
    内訳が他系統のぶんで水増しされない。
    """
    reasons = (
        ("phantom", np.broadcast_to(phantom[:, None], feature_row.shape).astype(np.bool_)),
        ("no_asof_at_t", np.asarray(feature_row == NO_ROW, dtype=np.bool_)),
        ("stale_at_t", too_old(grid_ms, observed_at_ms, feature_row)),
        ("unobserved_at_t", np.asarray((bikes == MISSING) | (docks == MISSING), dtype=np.bool_)),
        ("suspended_at_t", _suspended(flags)),
    )
    start = (
        np.ones(feature_row.shape, dtype=bool)
        if alive is None
        else np.broadcast_to(alive, feature_row.shape)
    )
    return _layer(reasons, np.asarray(start, dtype=np.bool_))


def exclude_at_target(
    *,
    alive: Bools,
    feature_row: Int32,
    label_row: Int32,
    observed_at_ms: Int64,
    target_ms: Int64,
    bikes: Int16,
    docks: Int16,
) -> Excluded:
    """`t + h` の側の除外。**停止は除外しない**（停止していればラベルが 0 になる）。

    `alive` には基準時刻の側を通った行を渡す。**内訳を二重に数えないため**で、
    こうしておけば「基準側の理由」と「目標側の理由」の合計が落とした行数に一致する。
    """
    reasons = (
        ("no_asof_at_target", np.asarray(label_row == NO_ROW, dtype=np.bool_)),
        ("stale_at_target", too_old(target_ms, observed_at_ms, label_row)),
        (
            "unobserved_at_target",
            np.asarray((bikes == MISSING) | (docks == MISSING), dtype=np.bool_),
        ),
        (
            "collision",
            np.asarray((label_row == feature_row) & (label_row != NO_ROW), dtype=np.bool_),
        ),
    )
    return _layer(reasons, alive)


def too_old(at_ms: Int64, observed_at_ms: Int64, row: Int32) -> Bools:
    """`at - observed_at > 600 秒` か。該当の観測が無い行はここでは偽にする。"""
    age_s = (at_ms - observed_at_ms) / _MS_PER_SECOND
    return np.asarray((row != NO_ROW) & (age_s > MAX_STALENESS_S), dtype=np.bool_)


def _suspended(flags: Int16) -> Bools:
    """運用停止か。**貸出と返却の両方のビットが立っていなければ停止**とみなす。

    実際に現れる値は 7 / 1 / -1 だけ（データ辞書 §2 の (9)）なので、これは
    「`flags = 1`」と同義になる。ビットで書いておけば、事業者が 3 や 5 を出し始めても
    意味が変わらない。
    """
    both = FLAG_RENTING | FLAG_RETURNING
    return np.asarray((flags < 0) | ((flags & both) != both), dtype=np.bool_)


def _layer(reasons: Sequence[tuple[str, Bools]], alive: Bools) -> Excluded:
    """優先順に 1 つだけ数える。**内訳の合計が落とした行数と一致する。**"""
    counts: dict[str, int] = {}
    for name, hit in reasons:
        caught = alive & hit
        counts[name] = int(caught.sum())
        alive = np.asarray(alive & ~hit, dtype=np.bool_)
    return Excluded(keep=alive, counts=counts)
