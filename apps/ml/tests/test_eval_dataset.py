"""学習サンプルの開き方（`eval/dataset.py`）。

**この 1 ファイルの主題は「番号の振り方で別のポートを混ぜないこと」。**
`station_id` はシステムを跨いで衝突する（実測 2,608 件）。
"""

import numpy as np
import pytest

from bikechance_ml.eval.dataset import (
    BUCKET_LABELS,
    TARGETS,
    bucket_index,
    days_in,
    to_samples,
)
from tests import eval_fixture as fixture

BIKE, DOCK = TARGETS


def test_the_same_station_id_in_two_systems_is_two_ports() -> None:
    """**気候値は「その場所のふだん」。** 同じ ID の別ポートを混ぜてはいけない。"""
    rows = [
        fixture.row(fixture.DAYS[0], "hellocycling", "1", 5, 3, 3, 1, 1),
        fixture.row(fixture.DAYS[0], "docomo-cycle", "1", 5, 3, 3, 1, 1),
    ]
    samples = to_samples(fixture.to_table(rows))
    assert samples.n_ports == 2
    assert samples.port[0] != samples.port[1]


def test_systems_are_numbered_and_kept() -> None:
    rows = [
        fixture.row(fixture.DAYS[0], "hellocycling", "a", 5, 3, 3, 1, 1),
        fixture.row(fixture.DAYS[0], "docomo-cycle", "b", 5, 3, 3, 1, 1),
    ]
    samples = to_samples(fixture.to_table(rows))
    assert samples.systems == ("docomo-cycle", "hellocycling")
    assert sorted(samples.system.tolist()) == [0, 1]


def test_the_port_names_are_in_the_number_order() -> None:
    """**名前と番号は同じ並び。** 成果物もプロファイルも名前で持っており、番号に直す
    並びが 2 か所に在ると、片方を直したときに静かに別のセルを指す
    （W5 プラン §12 の 142。§12 の 132 と同じ形）。
    """
    rows = [
        fixture.row(fixture.DAYS[0], "hellocycling", "b", 5, 3, 3, 1, 1),
        fixture.row(fixture.DAYS[0], "docomo-cycle", "a", 5, 3, 3, 1, 1),
    ]
    samples = to_samples(fixture.to_table(rows))
    assert samples.ports == ("docomo-cycle/a", "hellocycling/b")
    assert [samples.ports[one] for one in samples.port] == ["hellocycling/b", "docomo-cycle/a"]
    assert samples.n_ports == len(samples.ports)


def test_take_keeps_the_port_names() -> None:
    """行を絞っても**名前の並びは元のまま**（番号が指す先がずれないため）。"""
    rows = [
        fixture.row(fixture.DAYS[0], "hellocycling", "a", 5, 3, 3, 1, 1),
        fixture.row(fixture.DAYS[1], "hellocycling", "b", 5, 3, 3, 1, 1),
    ]
    samples = to_samples(fixture.to_table(rows))
    assert samples.take(np.array([True, False])).ports == samples.ports


def test_take_keeps_the_reference_sizes() -> None:
    """**行を絞っても参照表の大きさは変わらない。** 番号が指す先がずれるため。"""
    rows = [
        fixture.row(fixture.DAYS[0], "hellocycling", "a", 5, 3, 3, 1, 1),
        fixture.row(fixture.DAYS[1], "hellocycling", "b", 5, 3, 3, 1, 1),
    ]
    samples = to_samples(fixture.to_table(rows))
    part = samples.take(np.array([True, False]))
    assert len(part) == 1
    assert part.n_ports == samples.n_ports == 2
    assert part.systems == samples.systems


def test_targets_pair_the_label_with_its_own_counts() -> None:
    """`y_bike` は `bikes`、`y_dock` は `docks`（§12 の 100）。"""
    rows = [fixture.row(fixture.DAYS[0], "hellocycling", "a", 5, 7, 2, 1, 1)]
    samples = to_samples(fixture.to_table(rows))
    assert samples.count_of(BIKE).tolist() == [7]
    assert samples.count_of(DOCK).tolist() == [2]
    assert BIKE.label == "y_bike"
    assert DOCK.counts == "docks"


def test_days_are_jst_calendar_days() -> None:
    rows = [
        fixture.row(fixture.DAYS[0], "hellocycling", "a", 5, 3, 3, 1, 1, minute_of_day=0),
        fixture.row(fixture.DAYS[0], "hellocycling", "a", 5, 3, 3, 1, 1, minute_of_day=1435),
    ]
    samples = to_samples(fixture.to_table(rows))
    assert days_in(samples) == (fixture.DAYS[0],)


def test_unknown_dow_type_is_refused() -> None:
    """**知らない値を静かに 0 番に落とさない。**"""
    rows = [fixture.row(fixture.DAYS[0], "hellocycling", "a", 5, 3, 3, 1, 1, dow_type="???")]
    with pytest.raises(ValueError, match="想定外"):
        to_samples(fixture.to_table(rows))


def test_bucket_boundaries() -> None:
    counts = np.array([0, 1, 2, 3, 5, 6, 10, 11, 999], dtype=np.int16)
    labels = [BUCKET_LABELS[one] for one in bucket_index(counts)]
    assert labels == ["0", "1", "2", "3-5", "3-5", "6-10", "6-10", "11+", "11+"]
