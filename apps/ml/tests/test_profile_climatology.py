"""B2（気候値）をポートプロファイルから作る（`baselines/profile_climatology.py`、PR D）。

**主題は 3 つ。**

  * **同じセルを指していること**——プロファイルの行と学習サンプルの行が、同じ
    `(ポート, 曜日種別, 15 分枠)` の番号に落ちる（W5-01）。ここがずれると、例外は
    出ずに**別のポートの履歴が配られる**（§12 の 110・132 と同じ形）
  * **自分の日を引いていること**——配信は「その日を 1 点も含まないプロファイル」で
    予測するので、学習期間の行に当てはめるときも**その日ぶんを丸ごと引く**。引くのは
    **答えが入った日（到着日）**で、学習の行は**配る側より 1 日低い下限**で判定する
    （W5 プランの所見 179・180、D-37）
  * **抽出重みを引かないこと**——重みは 25 と 100 で、格子点は高々 84 である。行ごとの
    leave-one-out をそのまま当てると分母が負になる（W5 プラン §12 の 143）
"""

from dataclasses import replace
from datetime import date
from typing import Final

import numpy as np
import pyarrow as pa
import pytest

from bikechance_ml.baselines import climatology, profile_climatology
from bikechance_ml.baselines.climatology import MIN_CELL_POINTS, SLOTS_PER_DAY
from bikechance_ml.eval.dataset import TARGETS, Samples, to_samples
from bikechance_ml.features import build, profile
from bikechance_ml.features.arrays import Bools
from bikechance_ml.features.calendar import DOW_TYPE_ORDER
from tests import eval_fixture as fixture
from tests import features_fixture
from tests import profile_fixture as pf

BIKE, DOCK = TARGETS
DAY0, DAY1, DAY2 = fixture.DAYS

#: 既定のサンプル行（10:00 に 5 分先を聞く）は **枠 40** に着く（605 // 15）。
SLOT: Final[int] = 40

#: `cells()` を直に呼ぶ検査で使う並び（**昇順**。`np.unique` が作る形）。
PORTS: Final[tuple[str, ...]] = ("docomo-cycle/a", "hellocycling/a", "hellocycling/b")

#: 日をまたぐ行の基準の時刻（23:55）。15 分先を聞くと**翌日の 00:10（枠 0）**に着く。
LATE: Final[int] = 23 * 60 + 55
MIDNIGHT_SLOT: Final[int] = 0

#: 金曜と、その翌日の土曜（曜日種別が変わる夜をまたぐ）。
FRIDAY: Final = date(2026, 9, 11)
SATURDAY: Final = date(2026, 9, 12)


def weekday_floor(days: int) -> climatology.DayFloor:
    """平日の配る側の下限だけを変えた形（**学習の行は 1 日低いまま**）。"""
    return climatology.DayFloor(
        serve={**climatology.SERVE_DAYS, "weekday": days},
        fit_offset=climatology.PROFILE_FIT_OFFSET_DAYS,
    )


def cell_row(
    *,
    system: str = "hellocycling",
    station: str = "a",
    dow: str = "weekday",
    slot: int = SLOT,
    n: int = 6,
    n_bike_ok: int = 3,
    n_dock_ok: int = 6,
    n_days: int = 3,
) -> dict[str, object]:
    """1 セルぶん。**流量と台数の列は 0 で埋める**（B2 は読まない）。

    **既定は本番の下限を満たすセル**（3 日。D-30）。下限そのものを見る検査は
    `n_days` を明示して渡す。
    """
    return {
        "system_id": system,
        "station_id": station,
        "dow_type": dow,
        "slot15": slot,
        "n_days": n_days,
        "n": n,
        "n_suspended": 0,
        "n_bike_ok": n_bike_ok,
        "n_dock_ok": n_dock_ok,
        "sum_bikes": 0,
        "sum_bikes_sq": 0,
        "sum_rentals_60": 0,
        "sum_returns_60": 0,
    }


def _table(rows: list[dict[str, object]], schema: pa.Schema) -> pa.Table:
    return pa.table(
        {
            name: pa.array([one[name] for one in rows], type=schema.field(name).type)
            for name in schema.names
        },
        schema=schema,
    )


def to_profile(rows: list[dict[str, object]]) -> pa.Table:
    return _table(rows, profile.PROFILE_SCHEMA)


def to_daily(rows: list[dict[str, object]]) -> pa.Table:
    return _table(rows, profile.DAILY_SCHEMA)


def samples_of(rows: list[dict[str, object]]) -> Samples:
    return to_samples(fixture.to_table(rows))


def one_sample(
    day: date = DAY0, station: str = "a", system: str = "hellocycling", weight: float = 100.0
) -> dict[str, object]:
    """枠 40 に着く 1 行（貸出も返却も当たり）。"""
    return fixture.row(day, system, station, 5, 3, 6, 1, 1, weight=weight)


def late_sample(day: date, dow: str = "weekday") -> dict[str, object]:
    """**日をまたぐ** 1 行（23:55 に 15 分先。翌日の枠 0 に着く。`dow` は到着の曜日種別）。"""
    return fixture.row(day, "hellocycling", "a", 15, 3, 6, 1, 1, minute_of_day=LATE, dow_type=dow)


def source_of(
    samples: Samples,
    rows: list[dict[str, object]],
    dailies: dict[date, pa.Table] | None = None,
    *,
    day: date = DAY2,
) -> profile_climatology.FromProfile:
    """**ポートの並びはサンプルが決める。** 検査でも本番と同じ順に揃える。"""
    return profile_climatology.FromProfile(
        day=day,
        profile=to_profile(rows),
        dailies=profile_climatology.Dailies(
            by_day={
                one.toordinal(): profile_climatology.day_cells(table, samples.ports)
                for one, table in (dailies or {}).items()
            }
        ),
        ports=samples.ports,
    )


def only(samples: Samples) -> Bools:
    """`keep`（全行）。**プロファイルから作るときは見られない**が、口は同じである。"""
    return np.ones(len(samples), dtype=np.bool_)


# ── セルの番号（W5-01）──────────────────────────────────────────
def test_a_profile_row_and_a_sample_row_land_in_the_same_cell() -> None:
    """**これが PR D の全体。** 指すセルがずれると、別のポートの履歴が配られる。"""
    samples = samples_of([one_sample()])
    wanted = climatology.cell_key(
        samples.port.astype(np.int64),
        samples.dow_type.astype(np.int64),
        climatology.slot15(samples),
    )
    key, inside = profile_climatology.cells(to_profile([cell_row()]), samples.ports)
    assert inside.tolist() == [True]
    assert key.tolist() == wanted.tolist()


def test_the_dow_type_number_is_the_sorted_one() -> None:
    """**`weekday` は 2**（`DOW_TYPE_ORDER`）。`DOW_TYPES` の順で振ると 1 つずれる。"""
    rows = [cell_row(dow=kind) for kind in DOW_TYPE_ORDER]
    key, _ = profile_climatology.cells(to_profile(rows), PORTS)
    assert [int(one) // SLOTS_PER_DAY % len(DOW_TYPE_ORDER) for one in key] == [0, 1, 2]
    assert DOW_TYPE_ORDER[2] == "weekday"


def test_a_port_that_was_never_sampled_is_dropped() -> None:
    """プロファイルには**抽出されなかったポート**が入る（実測：09-12 の版で 193）。

    番号を振れないので落とす。**気候値の層が無いだけで B1 は正しく出る。**
    """
    rows = [cell_row(station="a"), cell_row(station="zzz")]
    _, inside = profile_climatology.cells(to_profile(rows), PORTS)
    assert inside.tolist() == [True, False]


def test_an_empty_port_order_drops_everything() -> None:
    """**空の並びで添字を作らない**（`order[-1]` は末尾に回り込む）。"""
    key, inside = profile_climatology.cells(to_profile([cell_row()]), ())
    assert inside.tolist() == [False]
    assert key.tolist() == [0]


def test_two_systems_with_the_same_station_id_do_not_mix() -> None:
    """`station_id` は**システムを跨いで衝突する**（実測 2,608 件）。"""
    rows = [
        cell_row(system="docomo-cycle", station="a", n=6, n_bike_ok=0),
        cell_row(system="hellocycling", station="a", n=6, n_bike_ok=6),
    ]
    samples = samples_of([one_sample(system="docomo-cycle"), one_sample(system="hellocycling")])
    table = profile_climatology.fit(to_profile(rows), ports=samples.ports, target=BIKE)
    applied = climatology.predict(table, samples, np.full(2, 0.5))
    assert applied.probability.tolist() == pytest.approx([0.0, 1.0])


# ── 表を作る ──────────────────────────────────────────────────
def test_the_counts_become_the_rate() -> None:
    """**割るのは 1 か所**（`climatology.table_of`）。6 点のうち 3 点で借りられた。"""
    samples = samples_of([one_sample()])
    table = profile_climatology.fit(
        to_profile([cell_row(n=6, n_bike_ok=3)]), ports=samples.ports, target=BIKE
    )
    applied = climatology.predict(table, samples, np.full(1, 0.9))
    assert applied.probability.tolist() == pytest.approx([0.5])
    assert applied.used.tolist() == [True]
    assert table.cells == 1


def test_the_target_picks_its_own_column() -> None:
    """貸出は `n_bike_ok`、返却は `n_dock_ok`。**同じセルで別の率になる。**"""
    built = to_profile([cell_row(n=6, n_bike_ok=3, n_dock_ok=6)])
    samples = samples_of([one_sample()])
    for target, wanted in ((BIKE, 0.5), (DOCK, 1.0)):
        table = profile_climatology.fit(built, ports=samples.ports, target=target)
        applied = climatology.predict(table, samples, np.full(1, 0.9))
        assert applied.probability.tolist() == pytest.approx([wanted]), target.name


def test_one_day_is_not_enough() -> None:
    """**下限は日数でも見る**（§12 の 101）。1 日ぶんの平均は「その日の観測」である。"""
    samples = samples_of([one_sample()])
    table = profile_climatology.fit(
        to_profile([cell_row(n=3, n_days=1)]), ports=samples.ports, target=BIKE
    )
    assert table.cells == 0
    applied = climatology.predict(table, samples, np.full(1, 0.9))
    assert applied.probability.tolist() == pytest.approx([0.9])
    assert applied.used.tolist() == [False]


def test_the_floors_are_the_same_ones_the_report_quotes() -> None:
    """**プロファイル側で下限を持ち直さない。** 配信と報告が同じ数を見る。

    **数えるものが違うので下限も別の定数である**（§12 の 167）——こちらは
    **格子点**（1 日 3 点）、学習サンプルのほうは**行**（1 日 0.3 行）である。
    """
    samples = samples_of([one_sample()])
    table = profile_climatology.fit(to_profile([cell_row()]), ports=samples.ports, target=BIKE)
    assert (table.min_samples, table.floor) == (MIN_CELL_POINTS, climatology.PROFILE_FLOOR)
    assert table.floor.serve == climatology.SERVE_DAYS
    assert table.floor.fit_offset == climatology.PROFILE_FIT_OFFSET_DAYS == 1
    assert table.max_days == {"sat": 0, "sun_holiday": 0, "weekday": 3}


def test_a_higher_floor_can_be_asked_for() -> None:
    """**既定より高い下限も渡せる**（測るため。配る側は既定を使う）。

    下限を上げるかは 09-23 に決めた（PR K）——**2 → 3 日**（D-30）。この検査は
    「既定で使えるセルが、もっと高い下限では落ちる」ことを見る。
    """
    ports = samples_of([one_sample()]).ports
    built = to_profile([cell_row(n=6, n_days=3)])
    assert profile_climatology.fit(built, ports=ports, target=BIKE).cells == 1
    assert profile_climatology.fit(built, ports=ports, target=BIKE, min_samples=8).cells == 0
    assert (
        profile_climatology.fit(built, ports=ports, target=BIKE, floor=weekday_floor(4)).cells == 0
    )


def test_two_days_are_no_longer_enough() -> None:
    """**2 日のセルは使わない**（2026-09-23 に 2 → 3 日。D-30、W5 プラン §12 の 177）。

    **2 日ぶんの B2 は、配らないほうが当たっていた。** 1 セル最大 6 点の率は 13 値しか
    取らず、祝日に配った確率を B2 を落とした確率に置き換えると Brier −7.59% だった。
    """
    ports = samples_of([one_sample()]).ports
    assert (
        profile_climatology.fit(
            to_profile([cell_row(n=6, n_days=2)]), ports=ports, target=BIKE
        ).cells
        == 0
    )
    assert (
        profile_climatology.fit(
            to_profile([cell_row(n=9, n_days=3)]), ports=ports, target=BIKE
        ).cells
        == 1
    )


def test_a_table_with_the_wrong_columns_is_refused() -> None:
    """**足りない列を捏造しない**（W3 プラン §12 の 97）。`daily` を渡しても通さない。"""
    with pytest.raises(profile.SchemaMismatchError):
        profile_climatology.fit(to_daily([cell_row()]), ports=PORTS, target=BIKE)


# ── 自分の日を引く ────────────────────────────────────────────
def test_the_rows_own_day_is_taken_out() -> None:
    """4 日ぶん（12 点・当たり 9）から自分の日（3 点・当たり 3）を引くと 6/9 になる。

    **引いたあとも下限を満たすよう 4 日にしてある**（残り 3 日。学習の行の下限は 2 日）。
    """
    samples = samples_of([one_sample(DAY0)])
    source = source_of(
        samples,
        [cell_row(n=12, n_bike_ok=9, n_days=4)],
        {DAY0: to_daily([cell_row(n=3, n_bike_ok=3)])},
    )
    table = source.table(samples, BIKE, only(samples))
    applied = source.leave_out(table, samples, BIKE, np.full(1, 0.9))
    assert applied.probability.tolist() == pytest.approx([6 / 9])
    assert applied.used.tolist() == [True]


def test_a_cell_at_the_floor_is_seen_by_the_blend() -> None:
    """**配る側の下限ちょうどのセルを、学習の行も引く**（W5 プランの所見 179、D-37）。

    自分の日を引くと 1 日薄くなる。配る側と同じ下限（3 日）で判定していたころは、
    **3 日のセルは配られるのに、混合の当てはめでは一度も引かれなかった**——係数は
    そのセルの値を見ないまま決まる（EDA #8：害の約 8 割がここから来ていた）。
    学習の行は 1 日低い下限（2 日）で判定するので、残り 2 日で引ける。
    """
    samples = samples_of([one_sample(DAY0)])
    source = source_of(
        samples,
        [cell_row(n=9, n_bike_ok=6, n_days=3)],
        {DAY0: to_daily([cell_row(n=3, n_bike_ok=3)])},
        day=DAY1,
    )
    table = source.table(samples, BIKE, only(samples))
    assert table.cells == 1, "配る側でも使うセル"
    applied = source.leave_out(table, samples, BIKE, np.full(1, 0.9))
    assert applied.used.tolist() == [True]
    assert applied.probability.tolist() == pytest.approx([(6 - 3) / (9 - 3)])


def test_a_cell_below_the_floor_stays_out_of_the_blend() -> None:
    """**配らないセルは、学習の行でも引かない**（下げるのは自分の日の 1 日ぶんだけ）。

    2 日のセルは配る側（3 日）で使わない。自分の日を引けば残り 1 日で、学習の行の
    下限（2 日）にも届かない。**下げすぎると、配信では引けないセルを混合が見る**
    （W5 プラン §12 の 141 と同じ形）。
    """
    samples = samples_of([one_sample(DAY0)])
    source = source_of(
        samples,
        [cell_row(n=6, n_bike_ok=6, n_days=2)],
        {DAY0: to_daily([cell_row(n=3, n_bike_ok=3)])},
        day=DAY1,
    )
    table = source.table(samples, BIKE, only(samples))
    assert table.cells == 0
    applied = source.leave_out(table, samples, BIKE, np.full(1, 0.9))
    assert applied.probability.tolist() == pytest.approx([0.9])
    assert applied.fell_back == 1


def test_a_dow_type_that_is_not_served_is_left_out_on_both_sides() -> None:
    """**配らない曜日種別は、配信でも学習の行でも引かない**（土日祝の既定。D-37）。

    厚いセル（5 日）でも同じ。混合が「配信では出ない値」を見て係数を決めないように。
    土日祝の下限を与えれば（9/28 に K を決めたあと）、同じセルが両方で引ける。
    """
    samples = samples_of([one_sample(SATURDAY) | {"target_dow_type": "sat"}])
    source = source_of(
        samples,
        [cell_row(dow="sat", n=15, n_bike_ok=9, n_days=5)],
        {SATURDAY: to_daily([cell_row(dow="sat", n=3, n_bike_ok=3)])},
        day=SATURDAY,
    )
    table = source.table(samples, BIKE, only(samples))
    assert table.cells == 0, "既定では土曜を配らない"
    assert table.max_days == {"sat": 5, "sun_holiday": 0, "weekday": 0}, "厚さは数えてある"
    assert source.leave_out(table, samples, BIKE, np.full(1, 0.9)).fell_back == 1

    served = replace(source, floor=climatology.weekend_floor(source.floor, 3))
    table = served.table(samples, BIKE, only(samples))
    assert table.cells == 1
    applied = served.leave_out(table, samples, BIKE, np.full(1, 0.9))
    assert applied.probability.tolist() == pytest.approx([(9 - 3) / (15 - 3)])


def test_the_sampling_weight_is_not_subtracted() -> None:
    """**W5 プラン §12 の 143。** 重みは 25 と 100 で、格子点の数は高々 84 である。

    行ごとの leave-one-out をそのまま当てると分母が負になり、**B2 が全行 B1 に落ちた
    まま B3 の係数が決まる**。配信では B2 が効くので、そこが train/serve skew になる。
    """
    rows = [cell_row(n=12, n_bike_ok=9, n_days=4)]
    samples = samples_of([one_sample(DAY0, weight=100.0)])
    table = profile_climatology.fit(to_profile(rows), ports=samples.ports, target=BIKE)

    by_row = climatology.predict_leave_one_out(table, samples, BIKE, np.full(1, 0.9))
    assert by_row.fell_back == 1, "重みを引くと 12 − 100 で分母が負になる"

    source = source_of(samples, rows, {DAY0: to_daily([cell_row(n=3, n_bike_ok=3)])})
    assert source.leave_out(table, samples, BIKE, np.full(1, 0.9)).fell_back == 0


def test_a_cell_the_day_did_not_touch_subtracts_nothing() -> None:
    """その日がそのセルに 1 点も入れていなければ、**引く量は 0**。"""
    samples = samples_of([one_sample(DAY0)])
    source = source_of(
        samples,
        [cell_row(n=6, n_bike_ok=3, n_days=3)],
        {DAY0: to_daily([cell_row(slot=SLOT + 1, n=3, n_bike_ok=3)])},
    )
    table = source.table(samples, BIKE, only(samples))
    applied = source.leave_out(table, samples, BIKE, np.full(1, 0.9))
    assert applied.probability.tolist() == pytest.approx([0.5])


def test_a_day_without_a_daily_is_left_out_of_the_blend() -> None:
    """**引けない日を混ぜない。** 引かずに混ぜると B2 が自分の答えを見たまま係数に効く。"""
    samples = samples_of([one_sample(DAY0), one_sample(DAY1)])
    source = source_of(samples, [cell_row()], {DAY0: to_daily([cell_row(n=3, n_bike_ok=3)])})
    assert source.blend_rows(samples).tolist() == [True, False]


def test_an_empty_daily_subtracts_nothing() -> None:
    """観測が 1 つも無かった日。**落ちずに 0 を引く。**"""
    samples = samples_of([one_sample(DAY0)])
    source = source_of(
        samples, [cell_row(n=6, n_bike_ok=3, n_days=3)], {DAY0: profile.DAILY_SCHEMA.empty_table()}
    )
    table = source.table(samples, BIKE, only(samples))
    applied = source.leave_out(table, samples, BIKE, np.full(1, 0.9))
    assert applied.probability.tolist() == pytest.approx([0.5])


# ── 到着日を引く（W5 プランの所見 180）──────────────────────────
def test_the_arrival_day_is_the_next_day_after_midnight() -> None:
    """`t + h` が日をまたげば、到着日は基準の日の翌日（**枠と同じ足し算**で決める）。

    **ちょうど 0:00 に着く行（23:55 に 5 分先）も翌日**——枠は 0 に戻る（`target_slot`）。
    """
    exactly = fixture.row(DAY0, "hellocycling", "a", 5, 3, 6, 1, 1, minute_of_day=LATE)
    samples = samples_of([one_sample(DAY0), late_sample(DAY0), exactly])
    assert profile_climatology.arrival_day(samples).tolist() == [
        DAY0.toordinal(),
        DAY1.toordinal(),
        DAY1.toordinal(),
    ]
    assert climatology.slot15(samples).tolist() == [SLOT, MIDNIGHT_SLOT, MIDNIGHT_SLOT]


def test_a_row_that_crosses_midnight_takes_out_the_day_it_arrives() -> None:
    """**答えが入っているのは到着日の寄与である**（所見 180）。

    月曜 23:55 に 15 分先を聞く行の答えは火曜 00:10 の観測で、火曜の `daily` に入って
    いる。**基準の日（月曜）を引いていたころは、答えがセルに残ったまま**当てはめていた。
    """
    samples = samples_of([late_sample(DAY0)])
    source = source_of(
        samples,
        [cell_row(slot=MIDNIGHT_SLOT, n=12, n_bike_ok=6, n_days=4)],
        {
            DAY0: to_daily([cell_row(slot=MIDNIGHT_SLOT, n=3, n_bike_ok=0)]),
            DAY1: to_daily([cell_row(slot=MIDNIGHT_SLOT, n=3, n_bike_ok=3)]),
        },
    )
    table = source.table(samples, BIKE, only(samples))
    applied = source.leave_out(table, samples, BIKE, np.full(1, 0.9))
    # 火曜（答え）を引く。月曜を引くと (6 − 0) / (12 − 3) = 2/3 になる
    assert applied.probability.tolist() == pytest.approx([(6 - 3) / (12 - 3)])


def test_a_row_that_arrives_on_another_dow_type_loses_its_day() -> None:
    """**違う曜日種別に着く行も、到着日の 1 日が引かれる**（所見 180）。

    金曜 23:55 → 土曜 00:10。セルは土曜のもので、**金曜の `daily` に土曜のセルは無い**。
    基準の日で引いていたころは何も引かれず、日数も減らなかった——下限ちょうどの
    セルを、学習の行だけが「自分の日を含んだまま」引けていた。
    """
    samples = samples_of([late_sample(FRIDAY, dow="sat")])
    source = source_of(
        samples,
        [cell_row(dow="sat", slot=MIDNIGHT_SLOT, n=9, n_bike_ok=6, n_days=3)],
        {
            FRIDAY: to_daily([cell_row(slot=MIDNIGHT_SLOT, n=3, n_bike_ok=3)]),
            SATURDAY: to_daily([cell_row(dow="sat", slot=MIDNIGHT_SLOT, n=3, n_bike_ok=3)]),
        },
        day=SATURDAY,
    )
    own = profile_climatology.own_day(source.dailies, samples, BIKE)
    assert (own.days.tolist(), own.total.tolist()) == ([1], [3.0]), "土曜の 1 日ぶん"
    served = replace(source, floor=climatology.weekend_floor(source.floor, 3))
    table = served.table(samples, BIKE, only(samples))
    applied = served.leave_out(table, samples, BIKE, np.full(1, 0.9))
    assert applied.probability.tolist() == pytest.approx([(6 - 3) / (9 - 3)])


def test_a_row_whose_arrival_day_cannot_be_taken_out_is_left_out_of_the_blend() -> None:
    """**到着日の `daily` が無ければ混合に入れない**（基準の日の `daily` があっても）。"""
    samples = samples_of([one_sample(DAY0), late_sample(DAY0)])
    source = source_of(samples, [cell_row()], {DAY0: to_daily([cell_row()])}, day=DAY2)
    assert source.blend_rows(samples).tolist() == [True, False]


def test_a_row_that_arrives_after_the_profile_day_needs_nothing_taken_out() -> None:
    """**学習の最終日の夜に出て翌日に着く行**は、答えがプロファイルに入っていない。

    引く量は 0 で正しいので混合に使える（捨てると、最終日の夜の行だけが欠ける）。
    """
    samples = samples_of([late_sample(DAY1)])
    source = source_of(
        samples, [cell_row(slot=MIDNIGHT_SLOT)], {DAY1: to_daily([cell_row()])}, day=DAY1
    )
    assert source.blend_rows(samples).tolist() == [True]
    own = profile_climatology.own_day(source.dailies, samples, BIKE)
    assert (own.days.tolist(), own.total.tolist()) == ([0], [0.0])


# ── 渡し間違いを止める ────────────────────────────────────────
def test_a_different_port_order_is_refused() -> None:
    """**番号の振り直しは静かに別のセルを指す**（§12 の 132 と同じ形）。"""
    samples = samples_of([one_sample()])
    source = replace(source_of(samples, [cell_row()]), ports=("other/a",))
    with pytest.raises(profile_climatology.PortsMismatchError):
        source.table(samples, BIKE, only(samples))


def test_describe_names_the_version_and_the_days() -> None:
    """報告書に**どの版で作ったか**が出る（検証日を含む版を渡していないか読める）。"""
    samples = samples_of([one_sample()])
    source = source_of(samples, [cell_row()], {DAY0: to_daily([cell_row()])}, day=DAY1)
    assert "2026-09-08" in source.describe()


def test_the_floor_reaches_the_table_through_the_source() -> None:
    """**`FromProfile` が自分の下限を当てはめに渡している。**

    `describe()` が正しくても、`table()` が既定を使っていれば報告と中身が食い違う
    ——**決めた値が効くところまで**見る（§12 の 166 と同じ抜けを塞ぐ）。
    """
    samples = samples_of([one_sample()])
    source = source_of(samples, [cell_row(n=6, n_days=3)])
    assert source.table(samples, BIKE, only(samples)).floor == climatology.PROFILE_FLOOR
    assert source.table(samples, BIKE, only(samples)).cells == 1
    higher = replace(source, floor=weekday_floor(4))
    assert higher.table(samples, BIKE, only(samples)).floor == weekday_floor(4)
    assert higher.table(samples, BIKE, only(samples)).cells == 0
    assert replace(source, min_points=7).table(samples, BIKE, only(samples)).min_samples == 7


def test_describe_quotes_the_floor() -> None:
    """**どの下限で作った B2 かが報告書と登録簿に残る**（§12 の 167 と同じ作法）。

    当てはめ直した報告書にこれが出ることが、**下限を上げたことが効いた証拠**になる（D-30）。
    """
    samples = samples_of([one_sample()])
    source = source_of(samples, [cell_row()], {DAY0: to_daily([cell_row()])}, day=DAY1)
    assert source.describe() == (
        "プロファイル（profiles/date=2026-09-08、引ける日 1、下限 2 点・sat 配らない・"
        "sun_holiday 配らない・weekday 3 日（学習の行は 1 日低く）、自分の日は到着日で引く）"
    )
    assert "weekday 4 日" in replace(source, floor=weekday_floor(4)).describe()


# ── 端から端まで（PR B と PR D の接ぎ目）──────────────────────
def test_the_rate_is_the_share_of_grid_points_where_you_could_rent() -> None:
    """観測 → `build_day` → `roll` → B2 の表 → 予測。**1 本つながっていること。**

    ポート a は 1 日じゅう 3 台あり（借りられる）、b は 0 台（借りられない）。
    **3 日ぶん**転がせば（下限 3 日。D-30）、**B2 の率は 1.0 と 0.0** になる。
    """
    rolled = _rolled(
        [("hellocycling", "a", 3, 6, pf.OPEN), ("hellocycling", "b", 0, 9, pf.OPEN)],
        [pf.OPEN, pf.OPEN, pf.OPEN],
    )
    samples = samples_of([one_sample(DAY1, station="a"), one_sample(DAY1, station="b")])
    table = profile_climatology.fit(rolled, ports=samples.ports, target=BIKE)
    applied = climatology.predict(table, samples, np.full(2, 0.5))
    assert applied.probability.tolist() == pytest.approx([1.0, 0.0])
    assert applied.used.tolist() == [True, True]


def test_a_suspended_port_counts_against_the_rate() -> None:
    """**休止していた時間は分母に入る。** 借りられなかったのは利用者にとって同じである。

    a は 1 日目だけ休止（台数はある）。3 日ぶんでは 9 点のうち 6 点しか借りられない。
    """
    rolled = _rolled([("hellocycling", "a", 3, 6, pf.OPEN)], [pf.SUSPENDED, pf.OPEN, pf.OPEN])
    samples = samples_of([one_sample(DAY1, station="a")])
    table = profile_climatology.fit(rolled, ports=samples.ports, target=BIKE)
    applied = climatology.predict(table, samples, np.full(1, 0.9))
    assert applied.probability.tolist() == pytest.approx([6 / 9])


def _rolled(ports: list[tuple[str, str, int, int, int]], flags_per_day: list[int]) -> pa.Table:
    """3 日ぶんを観測から組み立てて転がす。`flags_per_day` は日ごとの `flags`。"""
    rolled: pa.Table | None = None
    for day, flags in zip((DAY0, DAY1, DAY2), flags_per_day, strict=True):
        rows = [
            (system, station, bikes, docks, flags) for system, station, bikes, docks, _ in ports
        ]
        rolled = profile.roll(rolled, pf.daily(pf.observations(rows, day=day), day=day), None)
    assert rolled is not None
    return rolled


# ── 特徴量の `prof_p_*` と同じ量であること（W5-01、W5 プラン §6.10 の PR J1）────
def test_prof_p_is_the_b2_rate_of_the_same_cell() -> None:
    """**`prof_p_bike` / `prof_p_dock` は B2 の率そのもの**（W5-01）。同じ行から同じ値が出る。

    特徴量（`features/build.py` → `profile.Lookup`）と B2（`fit` → `climatology.predict`）は
    **別の道**でセルを引く。下限を全曜日種別 1 点 1 日に下げれば、**B2 が引けた行と
    `prof_n_days > 0` の行が一致し、率もビット単位で一致する**——しなければ、どちらかが別の
    セルを指している（枠・曜日種別・ポートの番号のどれか）。
    """
    built = build.build_day(features_fixture.build_inputs()).table
    samples = to_samples(built)
    days = np.asarray(built.column("prof_n_days").to_numpy(), dtype=np.int64)
    assert 0 < int((days > 0).sum()) < len(days), "引ける行と引けない行の両方が要る"
    for target in TARGETS:
        table = profile_climatology.fit(
            features_fixture.load_profile(),
            ports=samples.ports,
            target=target,
            min_samples=1,
            floor=climatology.DayFloor.uniform(1),
        )
        applied = climatology.predict(table, samples, np.full(len(samples), -1.0))
        assert applied.used.tolist() == (days > 0).tolist(), target.name
        feature = built.column(f"prof_p_{target.name}").to_numpy(zero_copy_only=False)
        rate = np.asarray(applied.probability, dtype=np.float32)
        assert (
            rate[applied.used].tobytes()
            == np.asarray(feature[applied.used], dtype=np.float32).tobytes()
        ), target.name
