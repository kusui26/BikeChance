"""学習と推論が**同じ 61 列**を出すこと（W4 プラン §4 の W4-04、§6.3）。

**口約束にしない。** CLAUDE.md §2 の 4 は「特徴量は単一実装を学習と推論で共用する」と
書いているが、W3 の段 8 の推論は実際には別経路だった（W3 プラン §13.4）。**一致を
宣言するのではなく、落ちる検査にする。**

守るのは 3 つ。
  * 同じ観測・同じ基準時刻から、`build_day` の 1 点と `build_now` が**完全に一致する**
  * **推論が読む窓だけ**でも同じ値になる（`serving_windows`）。ここが崩れると、
    「学習は 25 時間読める、推論は 3 時間しか読めない」がそのまま skew になる
  * ラベルと抽出は推論の出力に**入らない**

**許容誤差を入れない。** ずれたら「なぜずれたか」を追う（W4 プラン §5.2）。
"""

from datetime import UTC, date, datetime, timedelta
from typing import Final

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from bikechance_ml.features import build, neighbors, static, weather
from bikechance_ml.features.constants import CHANGE_CAP_MINUTES
from bikechance_ml.features.grid import JST
from bikechance_ml.features.reference import (
    StationAttributeRow,
    StationGeoRow,
    SystemReference,
)
from bikechance_ml.features.schema import SERVING_DROPPED, SERVING_SCHEMA, feature_columns
from bikechance_ml.jobs.snapshot_table import SCHEMA as SNAPSHOT_SCHEMA
from bikechance_ml.models import matrix
from tests import features_fixture as fixture

#: 突き合わせる列。**61 列すべて**（`h_min` を含む）。
COMPARED = feature_columns()

#: 行を突き合わせる鍵。
KEYS = ("system_id", "station_id", "h_min")


def _base_times(table: pa.Table) -> tuple[datetime, ...]:
    """出力に現れた基準時刻（重複を除く）。"""
    stamps = table.column("t").to_pylist()
    return tuple(sorted({one.replace(tzinfo=UTC) for one in stamps}))


def _rows_by_key(table: pa.Table, at: datetime) -> dict[tuple[object, ...], dict[str, object]]:
    """基準時刻で絞り、鍵で引ける形にする。"""
    stamp = pa.scalar(at, type=table.schema.field("t").type)
    selected = table.filter(pc.equal(table.column("t"), stamp))
    return {tuple(row[key] for key in KEYS): row for row in selected.to_pylist()}


def _windowed(table: pa.Table, at: datetime) -> pa.Table:
    """**推論が実際に読む範囲**だけに絞る（`serving_windows`）。

    束ねるのに `or_kleene` を使う。`or_` は null を伝播するので、null を含む列に
    掛けると**行が黙って消える**（W3 プラン §12 の 91 と同じ罠）。ここは
    `observed_at` が非 null なので差は出ないが、書き方を揃えておく。
    """
    observed = table.column("observed_at")
    masks = [
        pc.and_(
            pc.greater_equal(observed, pa.scalar(start, type=observed.type)),
            pc.less(observed, pa.scalar(end, type=observed.type)),
        )
        for start, end in build.serving_windows(at)
    ]
    keep = masks[0]
    for mask in masks[1:]:
        keep = pc.or_kleene(keep, mask)
    return table.filter(keep)


def _windowed_weather(at: datetime) -> weather.Weather:
    """**推論が実際に読む範囲**の予報だけ（`weather.serving_window`）。

    観測と同じ理由でここを絞る。学習は 1 日ぶんの発行を持っているが、推論は 3 時間ぶんしか
    読まない。**絞っても同じ値が出る**ことを確かめないと、窓の長さがそのまま skew になる
    （`minutes_since_last_change` で 1 度やった。W4 プラン §4 の W4-10）。
    """
    start, end = weather.serving_window(at)
    return weather.to_weather(
        tuple(one for one in fixture.load_weather_rows() if start <= one.available_at < end)
    )


def _compare(
    day_rows: dict[tuple[object, ...], dict[str, object]], ready: pa.Table, at: datetime
) -> int:
    """一致した行数を返す。**1 つでも違えば落とす。**"""
    now_rows = {tuple(row[key] for key in KEYS): row for row in ready.to_pylist()}
    matched = 0
    for key, expected in day_rows.items():
        actual = now_rows.get(key)
        assert actual is not None, f"{at:%H:%M} の {key} が推論側に無い"
        for name in COMPARED:
            assert actual[name] == expected[name], (
                f"{at:%H:%M} {key} の {name} が違う: "
                f"学習 {expected[name]!r} / 推論 {actual[name]!r}"
            )
        matched += 1
    return matched


def _now_inputs(
    day_inputs: build.DayInputs,
    at: datetime,
    table: pa.Table,
    forecast: weather.Weather | None = None,
    system_id: str = "hellocycling",
) -> build.NowInputs:
    """**予報を渡さなければ学習と同じものを使う**（窓の効果を見たいときだけ渡す）。"""
    return build.NowInputs(
        at=at,
        system_id=system_id,
        reference=day_inputs.reference,
        table=table,
        weather=day_inputs.weather if forecast is None else forecast,
    )


def _serve_all(
    day_inputs: build.DayInputs,
    at: datetime,
    table: pa.Table,
    forecast: weather.Weather | None = None,
) -> pa.Table:
    """本番と同じく**システムごとに 1 回ずつ**回し、結果を束ねる。

    `/ml/infer/{system}` は 1 系統ずつ走るので、`build_now` も 1 系統ぶんしか行を
    作らない（他系統は近傍のためだけに居る）。**学習の 1 日ぶんと突き合わせるには、
    本番と同じ回数だけ回して束ねる**必要がある。
    """
    systems = sorted({system_id for system_id, _ in day_inputs.reference.facts.station_keys()})
    parts = [
        build.build_now(_now_inputs(day_inputs, at, table, forecast, system_id)).table
        for system_id in systems
    ]
    return pa.concat_tables(parts)


def _run(windowed: bool) -> tuple[int, int]:
    """`build_day` の出した行を、基準時刻ごとに `build_now` と突き合わせる。"""
    day_inputs = fixture.build_inputs()
    built = build.build_day(day_inputs)
    times = _base_times(built.table)
    matched = 0
    for at in times:
        table = _windowed(day_inputs.table, at) if windowed else day_inputs.table
        forecast = _windowed_weather(at) if windowed else day_inputs.weather
        matched += _compare(
            _rows_by_key(built.table, at), _serve_all(day_inputs, at, table, forecast), at
        )
    return len(times), matched


# ── 一致 ──────────────────────────────────────────────────────
def test_build_now_matches_build_day() -> None:
    """**同じ観測から、同じ 61 列が出る。**"""
    times, matched = _run(windowed=False)
    assert times > 0, "突き合わせる基準時刻が無い（フィクスチャを疑う）"
    assert matched > 0, "突き合わせた行が無い"


def test_the_serving_window_is_enough() -> None:
    """**推論が読む窓だけでも同じ値になる。**

    ここが落ちるなら、窓が短いか、窓の長さに依存する特徴量が増えたかのどちらかである
    （`minutes_since_last_change` は上限を入れて閉じた。W4 プラン §4 の W4-10）。
    """
    _, matched = _run(windowed=True)
    assert matched > 0


def test_the_two_paths_cover_the_same_rows() -> None:
    """**推論は抽出をしない**ので、学習が出した行は必ず推論にもある。"""
    day_inputs = fixture.build_inputs()
    built = build.build_day(day_inputs)
    at = _base_times(built.table)[0]
    served = _serve_all(day_inputs, at, day_inputs.table)
    day_keys = set(_rows_by_key(built.table, at))
    now_keys = {tuple(row[key] for key in KEYS) for row in served.to_pylist()}
    assert day_keys <= now_keys
    # 抽出を通っていないので、推論のほうが多い
    assert len(now_keys) >= len(day_keys)


def test_a_neighbour_system_only_needs_its_current_state() -> None:
    """**他系統は「いまの状態」だけでよい。** 履歴を読まなくても値が変わらない。

    近傍の集計（`nb_bikes_sum_300m` ほか）は基準時刻の 1 点しか見ない。だから推論は
    自系統だけ 200 分ぶん読み、**他系統は直近の 1 本**で足りる（W4 プラン §6.3）。
    ここが崩れると、他系統ぶんも 200 分読むことになって所要が倍になる。
    """
    day_inputs = fixture.build_inputs()
    built = build.build_day(day_inputs)
    at = _base_times(built.table)[-1]
    full = build.build_now(_now_inputs(day_inputs, at, day_inputs.table)).table

    table = day_inputs.table
    observed = table.column("observed_at")
    own = pc.equal(table.column("system_id"), pa.scalar("hellocycling"))
    recent = pc.greater_equal(observed, pa.scalar(at - timedelta(minutes=15), type=observed.type))
    trimmed = table.filter(pc.or_kleene(own, recent))
    assert trimmed.num_rows < table.num_rows, "他系統の行が削れていない"

    partial = build.build_now(_now_inputs(day_inputs, at, trimmed)).table
    assert partial.to_pylist() == full.to_pylist()


# ── 出力の形 ──────────────────────────────────────────────────
def test_serving_output_has_no_labels_or_weights() -> None:
    """**ラベルは未来、抽出は学習の都合。** どちらも推論の出力に入れない。"""
    day_inputs = fixture.build_inputs()
    at = _base_times(build.build_day(day_inputs).table)[0]
    ready = build.build_now(_now_inputs(day_inputs, at, day_inputs.table))
    assert ready.table.schema.names == SERVING_SCHEMA.names
    for name in SERVING_DROPPED:
        assert name not in ready.table.schema.names


def test_every_feature_column_is_served() -> None:
    """61 列が 1 つも欠けずに出る。"""
    assert set(COMPARED) <= set(SERVING_SCHEMA.names)
    assert len(COMPARED) == 61


def test_rows_are_stations_times_horizons() -> None:
    day_inputs = fixture.build_inputs()
    at = _base_times(build.build_day(day_inputs).table)[0]
    ready = build.build_now(_now_inputs(day_inputs, at, day_inputs.table))
    # **1 系統ぶんだけ出る**（他系統は近傍のためだけに居る）
    assert set(ready.table.column("system_id").to_pylist()) == {"hellocycling"}
    assert ready.table.num_rows == ready.stats.rows
    assert ready.stats.rows == ready.stats.predictable * 10
    assert ready.stats.feature_set == "v3"


def test_the_grid_refuses_a_shift_it_does_not_have() -> None:
    """**足し忘れは静かに通さない。** 別の時刻の値が特徴量に入るより、止まるほうがよい。"""
    from bikechance_ml.features.grid import MissingShiftError, build_point_grid

    grid = build_point_grid(datetime(2026, 9, 7, 5, 0, tzinfo=UTC), (0, -60))
    assert grid.shifted(-60).start == 0
    try:
        grid.shifted(-1440)
    except MissingShiftError:
        return
    raise AssertionError("持っていないずらし量が通ってしまった")


def test_the_base_time_must_be_on_the_grid() -> None:
    """5 分格子から外れた基準時刻は**受け取らない**（静かに別の点を指す前に止める）。"""
    from bikechance_ml.features.grid import build_point_grid

    try:
        build_point_grid(datetime(2026, 9, 7, 5, 3, tzinfo=UTC), (0,))
    except ValueError:
        return
    raise AssertionError("格子から外れた時刻が通ってしまった")


# ── 窓の長さで値が変わらないこと（W4 プラン §4 の W4-10）──────
_DORMANT: Final[str] = "flat"
_BUSY: Final[str] = "busy"
#: 最後に動いた時刻（基準日の 00:00 JST からの分）。**上限（180 分）より前**に置く。
_LAST_CHANGE_MINUTE: Final[int] = 8 * 60


def _synthetic_inputs() -> build.DayInputs:
    """**動かないポート**を含む入力。フィクスチャの 3 ポートはよく動くので別に作る。

    `flat` は 08:00 JST に 1 度だけ動き、そのあと 1 度も動かない。学習は 25 時間
    さかのぼるので「N 分前に動いた」と言えるが、推論は 200 分しか読まない。
    **上限が無ければ、ここで必ず食い違う。**
    """
    start = datetime(2026, 9, 6, 0, 0, tzinfo=JST).astimezone(UTC)
    rows: list[dict[str, object]] = []
    for step in range((48 * 60) // 5):
        observed = start + timedelta(minutes=5 * step)
        minute = (observed.astimezone(JST) - datetime(2026, 9, 7, 0, 0, tzinfo=JST)).total_seconds()
        for station_id, bikes in (
            (_DORMANT, 5 if minute // 60 < _LAST_CHANGE_MINUTE // 60 else 4),
            (_BUSY, 3 + (step % 5)),
        ):
            rows.append(
                {
                    "system_id": "hellocycling",
                    "station_id": station_id,
                    "observed_at": observed,
                    "fetched_at": observed + timedelta(seconds=40),
                    "bikes": bikes,
                    "docks": 10,
                    "flags": 7,
                    "reported_age_s": 30,
                }
            )
    table = pa.Table.from_pylist(rows, schema=SNAPSHOT_SCHEMA)
    systems = (
        SystemReference(
            system_id="hellocycling",
            geo=tuple(
                StationGeoRow(
                    station_id=station_id,
                    first_seen_at=datetime(2026, 8, 1, tzinfo=UTC),
                    pref_code=13,
                    muni_code=13101,
                )
                for station_id in (_DORMANT, _BUSY)
            ),
            attributes=tuple(
                StationAttributeRow(
                    station_id=station_id,
                    lat=35.68,
                    lon=139.76,
                    capacity=15,
                    is_charging_station=False,
                    region_id=None,
                )
                for station_id in (_DORMANT, _BUSY)
            ),
            neighbors=(),
        ),
    )
    facts = static.to_facts(systems, {})
    return build.DayInputs(
        day=date(2026, 9, 7),
        reference=build.Reference(
            facts=facts,
            links=neighbors.to_links(systems, facts.station_keys()),
            holidays=frozenset(),
        ),
        table=table,
        # **天気は入れない。** ここで見たいのは `minutes_since_last_change` の上限で、
        # 天気の列は両側とも NULL のまま一致する
        weather=weather.empty(),
    )


def test_a_dormant_port_matches_too() -> None:
    """**動かないポートでも、窓だけで同じ値が出る。**

    `minutes_since_last_change` は唯一「読んだ窓の長さ」で値が変わる列だった。
    上限（`CHANGE_CAP_MINUTES`）を外すとこの検査が落ちる（W4 プラン §4 の W4-10）。
    実測（2026-09-10）で、直近 60 分に動いたポートは HELLO の 36% しかない。
    **これは例外ではなく多数派である。**
    """
    day_inputs = _synthetic_inputs()
    built = build.build_day(day_inputs)
    rows = [row for row in built.table.to_pylist() if row["station_id"] == _DORMANT]
    assert rows, "動かないポートの行が学習側に無い"
    checked = 0
    for at in sorted({row["t"].replace(tzinfo=UTC) for row in rows}):
        served = _serve_all(day_inputs, at, _windowed(day_inputs.table, at))
        checked += _compare(_rows_by_key(built.table, at), served, at)
    assert checked > 0
    # 上限に張り付いた行が実際にあること（この検査が空回りしていない証拠）
    capped = [row for row in rows if row["minutes_since_last_change"] == CHANGE_CAP_MINUTES]
    assert capped, "上限に達した行が無い（仕込みを見直す）"


# ── モデルに渡す行列（W4 プラン §6.5）─────────────────────────
def test_the_model_matrix_matches_too() -> None:
    """**学習と推論で、モデルに渡る行列がビットまで同じ。**

    61 列の値が一致するのは上の検査が見ている。ここで見るのは、その値を
    **62 列の行列に並べた結果**が一致すること——列の順序・型・カテゴリの符号化まで
    含めて、木が同じ位置で同じ値を見るかどうかである。

    ずれても例外は出ない。**確率だけが静かに変わる**ので、機械で固定する。
    """
    day_inputs = fixture.build_inputs()
    built = build.build_day(day_inputs)
    at = _base_times(built.table)[-1]
    served = _serve_all(day_inputs, at, day_inputs.table)

    trained = _rows_by_key(built.table, at)
    day_matrix = matrix.build(_only(built.table, at, sorted(trained)))
    now_matrix = matrix.build(_only(served, None, sorted(trained)))
    assert day_matrix.columns == now_matrix.columns
    assert np.array_equal(day_matrix.values, now_matrix.values, equal_nan=True)


def _only(table: pa.Table, at: datetime | None, keys: list[tuple[object, ...]]) -> pa.Table:
    """鍵の並びで行を選び、**同じ順**にそろえる（表ごとに行順が違うため）。"""
    rows = table.to_pylist()
    if at is not None:
        rows = [row for row in rows if row["t"].replace(tzinfo=UTC) == at]
    by_key = {tuple(row[key] for key in KEYS): row for row in rows}
    return pa.Table.from_pylist([by_key[key] for key in keys], schema=table.schema)
