"""B2 が効いているかの測りと、下限 K の選び方（`eval/b2_effect.py`。W6 の PR A、契約 44）。

**基準は W6 プラン §6.1 に、9/27 のデータを見る前に書いた。** ここではその基準を、
**表の行ごとに**留める——`choose_floor` の 5 行、「効いた」の 2 つの条件、ブートストラップ
が種で決まること、系統ごとに引き直すこと。**測りの数（+2.38% など）は実データで確かめる**
（PR A の完了条件 1）ので、ここでは作り（同じ行で比べる・重みで合わせる）を見る。
"""

from dataclasses import replace
from datetime import date, datetime, time
from typing import Final

import numpy as np
import pyarrow.compute as pc
import pytest

from bikechance_ml.baselines import climatology
from bikechance_ml.baselines.artifact import Artifact
from bikechance_ml.baselines.climatology import FromSamples
from bikechance_ml.eval import b2_effect
from bikechance_ml.eval.b2_effect import Effect, Pair, RowErrors
from bikechance_ml.eval.dataset import to_samples
from bikechance_ml.features.arrays import Float64
from bikechance_ml.features.grid import JST
from bikechance_ml.jobs.fit_baseline import build_artifact
from tests import eval_fixture as fixture

DAY0, DAY1, DAY2 = fixture.DAYS

#: 測る時刻（`to_samples` に渡す。値には効かない）。
AT: Final = datetime.combine(DAY2, time(hour=12), tzinfo=JST)

#: 4 組（2 系統 × 2 ターゲット）。
SYSTEMS: Final[tuple[str, ...]] = ("hellocycling", "docomo-cycle")
TARGET_NAMES: Final[tuple[str, ...]] = ("bike", "dock")


# ── 「効いた」と K（W6 プラン §6.1 の表）────────────────────
def pairs_with(not_worse: int) -> tuple[Pair, ...]:
    """4 組のうち `not_worse` 組で、B3 が落とした確率以下。**残りは B3 のほうが悪い。**"""
    keys = [(system, target) for system in SYSTEMS for target in TARGET_NAMES]
    return tuple(
        Pair(
            system=system,
            target=target,
            rows=100,
            used=0.5,
            b1=0.05,
            b3=0.040,
            dropped=0.041 if index < not_worse else 0.039,
            ece_b3=0.0,
            ece_dropped=0.0,
        )
        for index, (system, target) in enumerate(keys)
    )


def effect(*, gain: float = 0.02, low: float = 0.01, not_worse: int = 4) -> Effect:
    return Effect(
        pairs=pairs_with(not_worse), gain=gain, low=low, high=gain + 0.01, versus_b1=-0.01
    )


HELPED: Final = effect()
NOT_HELPED: Final = effect(gain=-0.02, low=-0.03)


@pytest.mark.parametrize(
    ("helped", "chosen"),
    [
        ((True, True, True), 3),
        ((False, True, True), 4),
        # 揃わない結果は厚いほうに寄せる（3 が効いても 4 が効かなければ 3 は選ばない）
        ((True, False, True), 5),
        ((False, False, True), 5),
        # 5 が効かなければ、3・4 がどうでも配らない
        ((True, True, False), None),
        ((False, False, False), None),
        ((True, False, False), None),
    ],
)
def test_k_is_the_smallest_k_above_which_everything_helped(
    helped: tuple[bool, bool, bool], chosen: int | None
) -> None:
    """**W6 プラン §6.1 の表そのもの。** k 以上がすべて「効いた」になる最小の k。"""
    effects = {k: HELPED if ok else NOT_HELPED for k, ok in zip((3, 4, 5), helped, strict=True)}
    assert b2_effect.choose_floor(effects) == chosen


def test_no_candidate_means_not_served() -> None:
    """候補が無ければ配らない（保守側）。"""
    assert b2_effect.choose_floor({}) is None


def test_a_single_candidate_stands_alone() -> None:
    """候補が 1 つでも同じ規則（それ自体が効けばそれ、効かなければ配らない）。"""
    assert b2_effect.choose_floor({5: HELPED}) == 5
    assert b2_effect.choose_floor({5: NOT_HELPED}) is None


def test_helped_needs_a_positive_gain_and_interval() -> None:
    """**基準の 1**：全体の「落とすと」が正で、95% 区間の下限も正。"""
    assert effect().helped
    assert not effect(gain=0.0, low=-0.001).helped
    assert not effect(gain=-0.01, low=-0.02).helped


def test_an_interval_touching_zero_does_not_count() -> None:
    """**区間の下限が 0 以下なら効かない**（ちょうど 0 も「効いた」にしない）。"""
    assert not effect(gain=0.02, low=0.0).helped
    assert not effect(gain=0.02, low=-0.001).helped


def test_three_of_four_pairs_must_not_be_worse() -> None:
    """**基準の 2**：4 組のうち 3 組以上。1 組の大きな益で、ほかの組の害を隠さない。"""
    assert effect(not_worse=3).helped
    assert not effect(not_worse=2).helped


def test_a_tie_is_not_worse() -> None:
    """「B3 の Brier が落とした確率**以下**」——同じなら数える。"""
    tied = tuple(replace(one, dropped=one.b3) for one in pairs_with(0))
    assert Effect(pairs=tied, gain=0.0, low=0.0, high=0.0, versus_b1=0.0).pairs_not_worse == 4


def test_the_minimum_is_the_one_in_the_plan() -> None:
    """数は W6 プラン §6.1 のまま（**変えると 9/25 に決めた基準から外れる**）。"""
    assert b2_effect.MIN_PAIRS_NOT_WORSE == 3
    assert (b2_effect.BOOTSTRAP_REPS, b2_effect.BOOTSTRAP_SEED) == (1_000, 20260927)
    assert b2_effect.INTERVAL_PERCENTILES == (2.5, 97.5)


# ── ポートを単位にしたブートストラップ ─────────────────────────
def sums(values: list[tuple[float, float]]) -> tuple[Float64, Float64]:
    """ポートごとの（B3 の二乗誤差の和、落とした確率の二乗誤差の和）。"""
    b3, dropped = zip(*values, strict=True)
    return np.asarray(b3, dtype=np.float64), np.asarray(dropped, dtype=np.float64)


def test_the_interval_is_fixed_by_the_seed() -> None:
    """**同じ種なら同じ区間**（報告の数を、誰が回しても同じにする）。"""
    rng = np.random.default_rng(0)
    groups = [sums([(float(a), float(a) * 1.02 + float(b)) for a, b in rng.random((50, 2))])]
    assert b2_effect.bootstrap_interval(groups) == b2_effect.bootstrap_interval(groups)
    assert b2_effect.bootstrap_interval(groups) != b2_effect.bootstrap_interval(groups, seed=1)


def test_the_interval_collapses_when_every_port_says_the_same() -> None:
    """どのポートも「落とすと +10%」なら、どう引き直しても +10%。"""
    groups = [sums([(1.0, 1.1)] * 5), sums([(2.0, 2.2)] * 3)]
    low, high = b2_effect.bootstrap_interval(groups)
    assert (low, high) == pytest.approx((0.1, 0.1))


def test_ports_are_drawn_within_their_own_system() -> None:
    """**系統ごとに引き直す**（系統の大きさを引き直しで変えない）。

    系統 A は 1 ポートだけで、落とすと大きく悪くなる。系統 B は 10 ポートで、差が無い。
    系統ごとに引けば A のポートは毎回ちょうど 1 度入るので、区間は 1 点に潰れる。
    全ポートを混ぜて引くと、A が 0 回や 2 回入る引き方ができて区間が広がる。
    """
    groups = [sums([(100.0, 200.0)]), sums([(1.0, 1.0)] * 10)]
    low, high = b2_effect.bootstrap_interval(groups)
    expected = (200.0 + 10.0 - (100.0 + 10.0)) / (100.0 + 10.0)
    assert (low, high) == pytest.approx((expected, expected))


def row_errors(system: str, stations: list[str], b3: list[float]) -> RowErrors:
    """1 組ぶんの行ごとの二乗誤差（落とした確率は B3 の 2 倍にしておく）。"""
    b3_errors = np.asarray(b3, dtype=np.float64)
    return RowErrors(
        system=system,
        station=np.asarray(stations, dtype=np.str_),
        b1=b3_errors,
        b3=b3_errors,
        dropped=2 * b3_errors,
    )


def test_port_sums_add_both_targets_of_a_port() -> None:
    """**ポートの和は両ターゲットを合わせる**（行でなくポートを引き直す）。名前の昇順に並ぶ。"""
    bike = row_errors("hellocycling", ["b", "a", "b"], [1.0, 2.0, 3.0])
    dock = row_errors("hellocycling", ["a", "b"], [10.0, 20.0])
    [(b3, dropped)] = b2_effect.port_sums([bike, dock])
    assert b3.tolist() == [12.0, 24.0]  # a: 2 + 10、b: 1 + 3 + 20
    assert dropped.tolist() == [24.0, 48.0]


def test_the_same_station_id_in_two_systems_stays_two_ports() -> None:
    """`station_id` は**系統を跨いで衝突する**（実測 2,608 件）。系統ごとに束ねる。"""
    hello = row_errors("hellocycling", ["1"], [1.0])
    docomo = row_errors("docomo-cycle", ["1"], [5.0])
    grouped = b2_effect.port_sums([hello, docomo])
    assert [(b3.tolist(), dropped.tolist()) for b3, dropped in grouped] == [
        ([1.0], [2.0]),
        ([5.0], [10.0]),
    ]


# ── 成果物を行に当てる ────────────────────────────────────────
#: 系統ごとのポート。**組ごとに行数を変える**（全体を組の平均で出すと、合わせ直しと違う数になる）。
STATIONS: Final[dict[str, tuple[str, ...]]] = {
    "hellocycling": ("a", "b", "c", "d"),
    "docomo-cycle": ("a", "b"),
}


def informative_rows(day: date) -> list[dict[str, object]]:
    """**B1 には見えず、B2 には見える差。** 台数は全ポート同じで、答えはポートで決まる。

    貸出は a・c が当たり、返却は a・b が当たり。B1（系統 × 台数 × 水平）はどのポートにも
    同じ率を出し、B2（ポート × 曜日種別 × 枠）はポートごとの率を出す。
    """
    return [
        fixture.row(day, system, station, 5, 1, 1, int(index % 2 == 0), int(index < 2))
        for system, names in STATIONS.items()
        for index, station in enumerate(names)
    ]


def informative_artifact(floor: climatology.DayFloor = climatology.SAMPLES_FLOOR) -> Artifact:
    """3 日ぶんで当てはめた版（**件数の下限は 2 に下げる**。平日 3 日で B2 が引ける）。"""
    rows = [one for day in fixture.DAYS for one in informative_rows(day)]
    samples = to_samples(fixture.to_table(rows))
    return build_artifact(samples, fixture.DAYS, FromSamples(min_samples=2, floor=floor))


INFORMATIVE: Final = informative_artifact()
EVAL_ROWS: Final = fixture.to_table(informative_rows(DAY2))


def pooled(one: Effect) -> tuple[float, float]:
    """組の Brier を**行の重みで合わせ直した**「落とすと」と「B1 との差」（重みは一様）。"""
    b1 = sum(pair.b1 * pair.rows for pair in one.pairs)
    b3 = sum(pair.b3 * pair.rows for pair in one.pairs)
    dropped = sum(pair.dropped * pair.rows for pair in one.pairs)
    return (dropped - b3) / b3, (b3 - b1) / b1


def test_b2_that_sees_the_port_helps() -> None:
    """**B2 だけが見える差があれば、落とすと悪くなる**（正の「落とすと」、区間も正）。"""
    measured = b2_effect.measure(INFORMATIVE, EVAL_ROWS, AT)
    assert [(pair.system, pair.target) for pair in measured.pairs] == [
        (system, target) for system in INFORMATIVE.systems for target in TARGET_NAMES
    ]
    assert all(pair.used == 1.0 for pair in measured.pairs)
    assert measured.gain > 0
    assert measured.low > 0
    assert measured.pairs_not_worse >= b2_effect.MIN_PAIRS_NOT_WORSE
    assert measured.helped


def test_the_whole_is_pooled_over_rows_not_averaged_over_pairs() -> None:
    """**全体は行を重みで合わせた Brier で比べる**（組の「落とすと」の平均ではない）。"""
    measured = b2_effect.measure(INFORMATIVE, EVAL_ROWS, AT)
    gain, versus_b1 = pooled(measured)
    assert measured.gain == pytest.approx(gain)
    assert measured.versus_b1 == pytest.approx(versus_b1)
    averaged = sum(pair.gain for pair in measured.pairs) / len(measured.pairs)
    assert measured.gain != pytest.approx(averaged), "組の大きさが違うので、平均とは別の数になる"


def test_without_usable_b2_dropping_it_changes_nothing() -> None:
    """**B2 が 1 セルも引けない版では、落とした確率は B3 そのもの**（同じ係数・同じ入力）。

    「落とすと」は 0、区間も 0 の 1 点、どの組も「落とした確率以下」——効いたとは言わない。
    """
    never = informative_artifact(climatology.DayFloor.uniform(99))
    measured = b2_effect.measure(never, EVAL_ROWS, AT)
    assert all(pair.used == 0.0 and pair.b3 == pair.dropped for pair in measured.pairs)
    assert (measured.gain, measured.low, measured.high) == (0.0, 0.0, 0.0)
    assert measured.pairs_not_worse == 4
    assert not measured.helped


def test_the_measure_is_repeatable() -> None:
    """**同じ成果物と同じ行なら、区間まで同じ**（種が決まっている）。"""
    assert b2_effect.measure(INFORMATIVE, EVAL_ROWS, AT) == b2_effect.measure(
        INFORMATIVE, EVAL_ROWS, AT
    )


def test_a_system_without_rows_has_no_pairs() -> None:
    """その日に行が無い系統は組を作らない。**4 組に満たなければ「効いた」にならない**（保守側）。"""
    hello_only = EVAL_ROWS.filter(pc.equal(EVAL_ROWS.column("system_id"), "hellocycling"))
    measured = b2_effect.measure(INFORMATIVE, hello_only, AT)
    assert {pair.system for pair in measured.pairs} == {"hellocycling"}
    assert len(measured.pairs) == 2
    assert measured.gain > 0
    assert measured.low > 0
    assert not measured.helped, "2 組では基準の 2（4 組のうち 3 組）を満たせない"


def test_rows_are_chosen_by_the_dow_type_they_arrive_on() -> None:
    """**到着時刻の曜日種別で絞る**（翌日の平日に着く行は日曜の行ではない）。"""
    rows = [
        fixture.row(DAY2, "hellocycling", "a", 5, 0, 9, 0, 1, dow_type="sun_holiday"),
        fixture.row(DAY2, "hellocycling", "a", 15, 0, 9, 0, 1, dow_type="weekday"),
        fixture.row(DAY2, "hellocycling", "b", 5, 0, 9, 0, 1, dow_type="sun_holiday"),
    ]
    chosen = b2_effect.arriving_on(fixture.to_table(rows), "sun_holiday")
    assert chosen.column("h_min").to_pylist() == [5, 5]
    assert set(chosen.column("target_dow_type").to_pylist()) == {"sun_holiday"}


# ── 候補の版が本当に k 日の版か ───────────────────────────────
def with_thickness(artifact: Artifact, floor: climatology.DayFloor, thick: int | None) -> Artifact:
    """全ターゲットの B2 に、下限の形と `sun_holiday` の厚さを付ける。"""
    max_days = None if thick is None else {"sat": 2, "sun_holiday": thick, "weekday": 10}
    targets = {
        name: replace(model, b2=replace(model.b2, floor=floor, max_days=max_days))
        for name, model in artifact.targets.items()
    }
    return replace(artifact, targets=targets)


def test_a_candidate_serves_and_holds_exactly_k_days() -> None:
    """配る側の下限が k で、**厚さもちょうど k 日**なら候補 k の版。"""
    floor = climatology.weekend_floor(climatology.PROFILE_FLOOR, 4)
    assert (
        b2_effect.check_candidate(with_thickness(INFORMATIVE, floor, 4), 4, "sun_holiday") is None
    )


@pytest.mark.parametrize(
    ("serve", "thick", "why"),
    [
        (3, 4, "厚い：4 日のセルも配る版になる"),
        (4, 3, "薄い：配る側が 4 日で、1 セルも届かない"),
        (None, 4, "配らない版"),
        (4, None, "厚さの記録が無い（PR A より前の成果物）"),
    ],
)
def test_a_candidate_that_is_not_k_days_is_named(
    serve: int | None, thick: int | None, why: str
) -> None:
    """**違う版で測って K を決めない。** 理由に、どのターゲットのどの数が違うかが出る。"""
    floor = climatology.weekend_floor(climatology.PROFILE_FLOOR, serve)
    wrong = b2_effect.check_candidate(with_thickness(INFORMATIVE, floor, thick), 4, "sun_holiday")
    assert wrong is not None, why
    assert f"sun_holiday の下限 {serve}・厚さ {thick}" in wrong
    assert "k = 4 のはず" in wrong
