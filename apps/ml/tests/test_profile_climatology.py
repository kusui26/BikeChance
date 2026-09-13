"""B2（気候値）をポートプロファイルから作る（`baselines/profile_climatology.py`、PR D）。

**主題は 3 つ。**

  * **同じセルを指していること**——プロファイルの行と学習サンプルの行が、同じ
    `(ポート, 曜日種別, 15 分枠)` の番号に落ちる（W5-01）。ここがずれると、例外は
    出ずに**別のポートの履歴が配られる**（§12 の 110・132 と同じ形）
  * **自分の日を引いていること**——配信は「その日を 1 点も含まないプロファイル」で
    予測するので、学習期間の行に当てはめるときも**その日ぶんを丸ごと引く**
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
from bikechance_ml.baselines.climatology import MIN_CELL_DAYS, MIN_CELL_SAMPLES, SLOTS_PER_DAY
from bikechance_ml.eval.dataset import TARGETS, Samples, to_samples
from bikechance_ml.features import profile
from bikechance_ml.features.arrays import Bools
from bikechance_ml.features.calendar import DOW_TYPE_ORDER
from tests import eval_fixture as fixture
from tests import profile_fixture as pf

BIKE, DOCK = TARGETS
DAY0, DAY1, DAY2 = fixture.DAYS

#: 既定のサンプル行（10:00 に 5 分先を聞く）は **枠 40** に着く（605 // 15）。
SLOT: Final[int] = 40

#: `cells()` を直に呼ぶ検査で使う並び（**昇順**。`np.unique` が作る形）。
PORTS: Final[tuple[str, ...]] = ("docomo-cycle/a", "hellocycling/a", "hellocycling/b")


def cell_row(
    *,
    system: str = "hellocycling",
    station: str = "a",
    dow: str = "weekday",
    slot: int = SLOT,
    n: int = 6,
    n_bike_ok: int = 3,
    n_dock_ok: int = 6,
    n_days: int = 2,
) -> dict[str, object]:
    """1 セルぶん。**流量と台数の列は 0 で埋める**（B2 は読まない）。"""
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
    """**プロファイル側で下限を持ち直さない。** 配信と報告が同じ数を見る。"""
    samples = samples_of([one_sample()])
    table = profile_climatology.fit(to_profile([cell_row()]), ports=samples.ports, target=BIKE)
    assert (table.min_samples, table.min_days) == (MIN_CELL_SAMPLES, MIN_CELL_DAYS)


def test_a_higher_floor_can_be_asked_for() -> None:
    """**下限を上げるかは 09-23 に決める**（PR K）。渡せるようにはしておく。"""
    ports = samples_of([one_sample()]).ports
    built = to_profile([cell_row(n=6, n_days=2)])
    assert profile_climatology.fit(built, ports=ports, target=BIKE, min_samples=8).cells == 0
    assert profile_climatology.fit(built, ports=ports, target=BIKE, min_days=3).cells == 0


def test_a_table_with_the_wrong_columns_is_refused() -> None:
    """**足りない列を捏造しない**（W3 プラン §12 の 97）。`daily` を渡しても通さない。"""
    with pytest.raises(profile.SchemaMismatchError):
        profile_climatology.fit(to_daily([cell_row()]), ports=PORTS, target=BIKE)


# ── 自分の日を引く ────────────────────────────────────────────
def test_the_rows_own_day_is_taken_out() -> None:
    """3 日ぶん（9 点・当たり 6）から自分の日（3 点・当たり 3）を引くと 3/6 になる。"""
    samples = samples_of([one_sample(DAY0)])
    source = source_of(
        samples,
        [cell_row(n=9, n_bike_ok=6, n_days=3)],
        {DAY0: to_daily([cell_row(n=3, n_bike_ok=3)])},
    )
    table = source.table(samples, BIKE, only(samples))
    applied = source.leave_out(table, samples, BIKE, np.full(1, 0.9))
    assert applied.probability.tolist() == pytest.approx([0.5])
    assert applied.used.tolist() == [True]


def test_two_days_are_not_enough_once_your_own_day_is_gone() -> None:
    """**下限は引いたあとで判定する。** 残り 1 日の平均は「別の日の観測」でしかない。"""
    samples = samples_of([one_sample(DAY0)])
    source = source_of(
        samples,
        [cell_row(n=6, n_bike_ok=6, n_days=2)],
        {DAY0: to_daily([cell_row(n=3, n_bike_ok=3)])},
        day=DAY1,
    )
    table = source.table(samples, BIKE, only(samples))
    applied = source.leave_out(table, samples, BIKE, np.full(1, 0.9))
    assert applied.probability.tolist() == pytest.approx([0.9])
    assert applied.fell_back == 1


def test_the_sampling_weight_is_not_subtracted() -> None:
    """**W5 プラン §12 の 143。** 重みは 25 と 100 で、格子点の数は高々 84 である。

    行ごとの leave-one-out をそのまま当てると分母が負になり、**B2 が全行 B1 に落ちた
    まま B3 の係数が決まる**。配信では B2 が効くので、そこが train/serve skew になる。
    """
    rows = [cell_row(n=9, n_bike_ok=6, n_days=3)]
    samples = samples_of([one_sample(DAY0, weight=100.0)])
    table = profile_climatology.fit(to_profile(rows), ports=samples.ports, target=BIKE)

    by_row = climatology.predict_leave_one_out(table, samples, BIKE, np.full(1, 0.9))
    assert by_row.fell_back == 1, "重みを引くと 9 − 100 で分母が負になる"

    source = source_of(samples, rows, {DAY0: to_daily([cell_row(n=3, n_bike_ok=3)])})
    assert source.leave_out(table, samples, BIKE, np.full(1, 0.9)).fell_back == 0


def test_a_cell_the_day_did_not_touch_subtracts_nothing() -> None:
    """その日がそのセルに 1 点も入れていなければ、**引く量は 0**。"""
    samples = samples_of([one_sample(DAY0)])
    source = source_of(
        samples,
        [cell_row(n=6, n_bike_ok=3, n_days=2)],
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
        samples, [cell_row(n=6, n_bike_ok=3, n_days=2)], {DAY0: profile.DAILY_SCHEMA.empty_table()}
    )
    table = source.table(samples, BIKE, only(samples))
    applied = source.leave_out(table, samples, BIKE, np.full(1, 0.9))
    assert applied.probability.tolist() == pytest.approx([0.5])


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


# ── 端から端まで（PR B と PR D の接ぎ目）──────────────────────
def test_the_rate_is_the_share_of_grid_points_where_you_could_rent() -> None:
    """観測 → `build_day` → `roll` → B2 の表 → 予測。**1 本つながっていること。**

    ポート a は 1 日じゅう 3 台あり（借りられる）、b は 0 台（借りられない）。
    2 日ぶん転がせば、**B2 の率は 1.0 と 0.0** になる。
    """
    rolled = _rolled(
        [("hellocycling", "a", 3, 6, pf.OPEN), ("hellocycling", "b", 0, 9, pf.OPEN)],
        [pf.OPEN, pf.OPEN],
    )
    samples = samples_of([one_sample(DAY1, station="a"), one_sample(DAY1, station="b")])
    table = profile_climatology.fit(rolled, ports=samples.ports, target=BIKE)
    applied = climatology.predict(table, samples, np.full(2, 0.5))
    assert applied.probability.tolist() == pytest.approx([1.0, 0.0])
    assert applied.used.tolist() == [True, True]


def test_a_suspended_port_counts_against_the_rate() -> None:
    """**休止していた時間は分母に入る。** 借りられなかったのは利用者にとって同じである。

    a は 1 日目だけ休止（台数はある）。2 日ぶんでは 6 点のうち 3 点しか借りられない。
    """
    rolled = _rolled([("hellocycling", "a", 3, 6, pf.OPEN)], [pf.SUSPENDED, pf.OPEN])
    samples = samples_of([one_sample(DAY1, station="a")])
    table = profile_climatology.fit(rolled, ports=samples.ports, target=BIKE)
    applied = climatology.predict(table, samples, np.full(1, 0.9))
    assert applied.probability.tolist() == pytest.approx([0.5])


def _rolled(ports: list[tuple[str, str, int, int, int]], flags_per_day: list[int]) -> pa.Table:
    """2 日ぶんを観測から組み立てて転がす。`flags_per_day` は日ごとの `flags`。"""
    rolled: pa.Table | None = None
    for day, flags in zip((DAY0, DAY1), flags_per_day, strict=True):
        rows = [
            (system, station, bikes, docks, flags) for system, station, bikes, docks, _ in ports
        ]
        rolled = profile.roll(rolled, pf.daily(pf.observations(rows, day=day), day=day), None)
    assert rolled is not None
    return rolled
