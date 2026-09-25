"""B2 の下限の形（`baselines/climatology.py` の `DayFloor`。W6 の PR A、D-37、契約 44）。

**主題は 3 つ。**

  * **配る側の下限は曜日種別ごと**で、「配らない」を表せる。土日祝は 9/28 に K を入れる
    までは配らない（W6 プラン §6.1）
  * **学習の行は、配る側より `fit_offset` 日低い下限で判定する**。プロファイルから作ると
    自分の日を丸ごと引くので、1 日下げれば**配るセル ＝ 混合が学習の行で見たセル**になる
    （W5 プランの所見 179）
  * **組めない形は作らない**——曜日種別の欠け・何も配らない・学習の行が 1 日を切る

セルは `(ポート, 曜日種別, 15 分枠)` の順に並ぶ（`cell_key`）。下限を曜日種別の軸に当てる
ところは、**軸を取り違えても例外が出ない**ので、ポートと曜日種別で日数を変えて確かめる。
"""

from types import MappingProxyType
from typing import Final

import numpy as np
import pytest

from bikechance_ml.baselines import climatology
from bikechance_ml.baselines.climatology import (
    PROFILE_FLOOR,
    SAMPLES_FLOOR,
    SERVE_DAYS,
    SLOTS_PER_DAY,
    DayFloor,
    FloorError,
    weekend_floor,
)
from bikechance_ml.eval.dataset import TARGETS, Samples, to_samples
from bikechance_ml.features.arrays import Int64
from bikechance_ml.features.calendar import DOW_TYPE_ORDER
from tests import eval_fixture as fixture

BIKE, _ = TARGETS

#: 形の見本：**土曜は配らない・日祝 4 日・平日 3 日、学習の行は 1 日低く。**
SHAPED: Final = DayFloor(serve={"sat": None, "sun_holiday": 4, "weekday": 3}, fit_offset=1)

#: 「配らない」を日数にしたもの。**どんな日数も届かない**ことだけを見る（値そのものは見ない）。
FAR_BEYOND_ANY_DAYS: Final[int] = 10**12


# ── 既定（9/28 に K を入れるまで）────────────────────────────
def test_the_default_serves_weekdays_only() -> None:
    """**PR の既定は、土日祝を配らない**（W6 プラン §6.1）。平日は 3 日のまま（D-30）。

    9/28 に K が決まったら、`SERVE_DAYS` とこの検査を一緒に直す。**測れなかったときは
    このまま当てはめ直し #1 に使える**ので、既定は保守側に置く。
    """
    assert dict(SERVE_DAYS) == {"sat": None, "sun_holiday": None, "weekday": 3}
    assert SERVE_DAYS["weekday"] == climatology.MIN_CELL_DAYS


def test_profiles_lower_the_fit_rows_by_one_day_and_samples_do_not() -> None:
    """**引く量で下げ幅が決まる。** プロファイルは自分の日を丸ごと引くので 1 日、学習
    サンプルは行を 1 つ引くだけで日数が減らないので 0 日。"""
    assert (PROFILE_FLOOR.serve, PROFILE_FLOOR.fit_offset) == (SERVE_DAYS, 1)
    assert (SAMPLES_FLOOR.serve, SAMPLES_FLOOR.fit_offset) == (SERVE_DAYS, 0)
    assert climatology.PROFILE_FIT_OFFSET_DAYS == 1


# ── 組めない形 ────────────────────────────────────────────────
@pytest.mark.parametrize(
    "serve",
    [
        {"sun_holiday": 3, "weekday": 3},  # 土曜が欠けている
        {"sat": 3, "sun_holiday": 3, "weekday": 3, "holiday": 3},  # 知らない種別
    ],
)
def test_a_floor_names_every_dow_type(serve: dict[str, int | None]) -> None:
    """**欠けた種別を黙って「配らない」にしない。** `DOW_TYPE_ORDER` の種別を全部持つ。"""
    with pytest.raises(FloorError, match="曜日種別"):
        DayFloor(serve=serve)


def test_a_floor_serves_somewhere() -> None:
    """**何も配らない下限は作れない**——B2 を使わないなら、作り方ごと外す話である。"""
    with pytest.raises(FloorError, match="配らない"):
        DayFloor(serve=dict.fromkeys(DOW_TYPE_ORDER, None))


@pytest.mark.parametrize(
    ("serve", "fit_offset"),
    [
        ({"sat": 1, "sun_holiday": 1, "weekday": 1}, 1),  # 学習の行が 0 日になる
        ({"sat": 1, "sun_holiday": None, "weekday": 3}, 1),  # いちばん低い種別で 0 日になる
        ({"sat": 3, "sun_holiday": 3, "weekday": 3}, -1),  # 学習の行のほうが厚い
    ],
)
def test_the_fit_rows_never_go_below_one_day(serve: dict[str, int | None], fit_offset: int) -> None:
    """**学習の行の下限は 1 日以上。** 0 日なら、自分の日しか無いセル（答えそのもの）を引く。"""
    with pytest.raises(FloorError, match="1 日を切る"):
        DayFloor(serve=serve, fit_offset=fit_offset)


# ── 形の読み方 ────────────────────────────────────────────────
def test_uniform_is_the_shape_before_pr_a() -> None:
    """**全曜日種別に同じ下限、学習の行も同じ**（版 1 と PR B の成果物はこの形で読む）。"""
    floor = DayFloor.uniform(3)
    assert dict(floor.serve) == {"sat": 3, "sun_holiday": 3, "weekday": 3}
    assert floor.fit_offset == 0


def test_the_legacy_number_is_the_lowest_served_floor() -> None:
    """`b2_min_days` に書く 1 つの数：**配る種別のうち最も低い下限**（配らない種別は数えない）。"""
    assert SHAPED.legacy_days == 3
    assert DayFloor(serve={"sat": 5, "sun_holiday": 4, "weekday": None}).legacy_days == 4


def test_the_arrays_follow_the_dow_type_order() -> None:
    """配列は `DOW_TYPE_ORDER`（sat・sun_holiday・weekday）の並び。学習の行は 1 日低い。"""
    assert DOW_TYPE_ORDER == ("sat", "sun_holiday", "weekday")
    served, fitted = SHAPED.serve_by_dow(), SHAPED.fit_by_dow()
    assert served[1:].tolist() == [4, 3]
    assert fitted[1:].tolist() == [3, 2]


def test_not_served_stays_out_of_reach_after_lowering() -> None:
    """**配らない種別は、学習の行で 1 日下げても届かない**（下げて折り返したりもしない）。"""
    assert int(SHAPED.serve_by_dow()[0]) > FAR_BEYOND_ANY_DAYS
    assert int(SHAPED.fit_by_dow()[0]) > FAR_BEYOND_ANY_DAYS


def test_describe_is_the_line_in_the_report() -> None:
    """報告書と `model_versions.metrics.climate` に残る 1 行。**配らない種別もそう書く。**"""
    assert (
        SHAPED.describe() == "sat 配らない・sun_holiday 4 日・weekday 3 日（学習の行は 1 日低く）"
    )
    assert DayFloor.uniform(3).describe() == "sat 3 日・sun_holiday 3 日・weekday 3 日"


def test_weekend_floor_changes_only_the_weekend() -> None:
    """**候補の版は土日祝の配る側だけが違う**（平日と学習の行の下げ幅はそのまま）。"""
    candidate = weekend_floor(PROFILE_FLOOR, 4)
    assert dict(candidate.serve) == {"sat": 4, "sun_holiday": 4, "weekday": 3}
    assert candidate.fit_offset == 1
    assert weekend_floor(PROFILE_FLOOR, None) == PROFILE_FLOOR


def test_the_floor_does_not_follow_the_dict_it_was_built_from() -> None:
    """**作ったあとで元の辞書を書き換えても、形は動かない**（表と成果物が同じ形を読む）。"""
    serve: dict[str, int | None] = {"sat": None, "sun_holiday": 4, "weekday": 3}
    floor = DayFloor(serve=serve)
    serve["weekday"] = 99
    assert floor.serve["weekday"] == 3
    assert isinstance(floor.serve, MappingProxyType)


# ── 表に当てる（配る側）──────────────────────────────────────
def _days_by_port_and_dow(values: list[list[int]]) -> Int64:
    """ポートごと・曜日種別ごとに、96 枠すべてへ同じ日数を入れた並び（`cell_key` の順）。"""
    return np.asarray(
        [days for port in values for days in port for _ in range(SLOTS_PER_DAY)], dtype=np.int64
    )


def test_each_cell_meets_the_floor_of_its_own_dow_type() -> None:
    """**曜日種別の軸に当てる。** ポート 2 つで日数を変え、軸の取り違えを見分けられるようにする。

    ポート 0 は（sat 5・sun 4・平日 2）、ポート 1 は（sat 2・sun 9・平日 3）日。下限は
    （sat 配らない・sun 4・平日 3）。軸を取り違えると、別のポートや別の種別の日数で判定する。
    """
    days = _days_by_port_and_dow([[5, 4, 2], [2, 9, 3]])
    met = climatology.meets_by_dow(days, SHAPED.serve_by_dow()).reshape(2, 3, SLOTS_PER_DAY)
    assert met.all(axis=2).tolist() == [[False, True, False], [False, True, True]]
    assert met.any(axis=2).tolist() == met.all(axis=2).tolist(), "同じ種別の枠は同じ判定"


def test_the_thickness_is_the_most_days_per_dow_type() -> None:
    """**厚さ**（曜日種別ごとの日数の最大）。成果物に書き、候補の版の確かめに使う。"""
    days = _days_by_port_and_dow([[5, 4, 2], [2, 9, 3]])
    assert climatology.max_days_by_dow(days) == {"sat": 5, "sun_holiday": 9, "weekday": 3}
    empty = np.zeros(0, dtype=np.int64)
    assert climatology.max_days_by_dow(empty) == {"sat": 0, "sun_holiday": 0, "weekday": 0}


def test_the_dow_type_comes_back_out_of_the_cell_number() -> None:
    """`dow_of_cell` は `cell_key` の逆（点検が曜日種別ごとに数えるのに使う）。"""
    port = np.asarray([0, 0, 1, 7, 7], dtype=np.int64)
    dow = np.asarray([0, 2, 1, 2, 0], dtype=np.int64)
    slot = np.asarray([0, 95, 40, 0, 95], dtype=np.int64)
    key = climatology.cell_key(port, dow, slot)
    assert climatology.dow_of_cell(key).tolist() == dow.tolist()


def _cell_rows(count: int, *, days: int, dow: str) -> Samples:
    """1 セルに `count` 行（`days` 日）。**到着の曜日種別だけを変える。**"""
    return to_samples(
        fixture.to_table(
            [
                fixture.row(
                    fixture.DAYS[one % days], "hellocycling", "a", 5, 0, 9, 1, 1, dow_type=dow
                )
                for one in range(count)
            ]
        )
    )


@pytest.mark.parametrize("dow", ["sat", "sun_holiday"])
def test_a_weekend_cell_is_not_served_by_default(dow: str) -> None:
    """**厚くても、既定では土日祝を配らない。** K を入れれば同じセルが使える。"""
    samples = _cell_rows(6, days=3, dow=dow)
    keep = np.ones(len(samples), dtype=np.bool_)
    assert climatology.fit(samples, BIKE, keep, min_samples=2).cells == 0
    chosen = weekend_floor(SAMPLES_FLOOR, 3)
    assert climatology.fit(samples, BIKE, keep, min_samples=2, floor=chosen).cells == 1


def test_a_weekday_cell_is_served_by_default() -> None:
    """平日は 3 日で配る（いまのまま）。"""
    samples = _cell_rows(6, days=3, dow="weekday")
    keep = np.ones(len(samples), dtype=np.bool_)
    assert climatology.fit(samples, BIKE, keep, min_samples=2).cells == 1


# ── 学習の行に当てる（`predict_without`）──────────────────────
def test_each_row_is_judged_by_the_floor_of_the_dow_type_it_arrives_on() -> None:
    """**行ごとに、到着の曜日種別の下限（配る側 − 1）で判定する。**

    3 行とも同じ 3 日のセルに着き、自分の日を 1 日引かれて 2 日残る。学習の行の下限は
    sat 配らない・sun 3・平日 2 なので、**平日の行だけが引ける**。下限を行の曜日種別
    ではなく別のもの（ポートやシステム）で引くと、並びが変わる。
    """
    rows = [
        fixture.row(fixture.DAYS[0], "hellocycling", "a", 5, 0, 9, 1, 1, dow_type=dow)
        for dow in DOW_TYPE_ORDER
    ]
    samples = to_samples(fixture.to_table(rows))
    size = samples.n_ports * len(DOW_TYPE_ORDER) * SLOTS_PER_DAY
    table = climatology.table_of(
        n_ports=samples.n_ports,
        total=np.full(size, 9.0),
        positive=np.full(size, 6.0),
        counted=np.full(size, 9, dtype=np.int64),
        days=np.full(size, 3, dtype=np.int64),
        min_samples=1,
        floor=SHAPED,
    )
    own = climatology.Own(
        total=np.full(3, 3.0),
        positive=np.full(3, 3.0),
        counted=np.full(3, 3, dtype=np.int64),
        days=np.ones(3, dtype=np.int64),
    )
    applied = climatology.predict_without(table, samples, np.full(3, 0.9), own)
    assert applied.used.tolist() == [False, False, True]
    assert applied.probability.tolist() == pytest.approx([0.9, 0.9, (6 - 3) / (9 - 3)])
