"""学習サンプルの列の契約（W3 プラン §9.3、開発プラン §6.3）。

**この `SCHEMA` が正。** 列を足したら `FEATURE_SET` を上げ、モデルカードに記録する
（CLAUDE.md §2 の原則 4）。読む側は必ず明示スキーマを渡す（データ辞書 §7.3）。

v0 に**入れていない**もの（W3 プラン §8）：

  * 履歴プロファイル（`prof_*`）── 過去 28 日の集計。9 月の時点で日数が足りない（W4〜W5）
  * 再配置（`minutes_since_rebalance` ほか）── 検知規則は W4
  * `bikes_same_time_7d` ── 7 日前がまだ無い
  * `is_limited_port` ── 過去 30 日の観測が要る
  * `parking_type` / `parking_hoop` ── 実測で分散ゼロ（データ辞書 §11）

**枠だけ作って NULL を並べることはしない。** 常に NULL の列は、学習側で「欠損の
扱い」を考えさせるだけ損をする。作れるようになった時点で足し、`FEATURE_SET` を上げる。
"""

from collections.abc import Mapping
from typing import Final

import pyarrow as pa

from bikechance_ml.features.constants import LAG_MINUTES

_KEYS: Final[list[pa.Field]] = [
    pa.field("system_id", pa.string(), nullable=False),
    pa.field("station_id", pa.string(), nullable=False),
    # 基準時刻。**JST の 5 分グリッドだが、値は UTC で持つ**（W3 プラン §4.3 の 23）
    pa.field("t", pa.timestamp("ms", tz="UTC"), nullable=False),
    pa.field("h_min", pa.int16(), nullable=False),
]

_LABELS: Final[list[pa.Field]] = [
    pa.field("y_bike", pa.int8(), nullable=False),
    pa.field("y_dock", pa.int8(), nullable=False),
]

_SAMPLING: Final[list[pa.Field]] = [
    # 逆抽出確率。**評価も重み付きで行う**（開発プラン §6.2）
    pa.field("weight", pa.float32(), nullable=False),
    pa.field("stratum", pa.string(), nullable=False),
    pa.field("feature_set", pa.string(), nullable=False),
]

_CALENDAR: Final[list[pa.Field]] = [
    pa.field("minute_of_day", pa.int16(), nullable=False),
    pa.field("minute_of_day_sin", pa.float32(), nullable=False),
    pa.field("minute_of_day_cos", pa.float32(), nullable=False),
    pa.field("dow", pa.int8(), nullable=False),
    pa.field("day_type", pa.string(), nullable=False),
    pa.field("dow_type", pa.string(), nullable=False),
    pa.field("is_holiday", pa.bool_(), nullable=False),
    pa.field("is_day_before_holiday", pa.bool_(), nullable=False),
    pa.field("is_last_business_day", pa.bool_(), nullable=False),
    pa.field("month", pa.int8(), nullable=False),
    # 目標時刻（t + h）の属性。**1 モデルで全水平を扱う鍵**（開発プラン §6.3）
    pa.field("target_minute_of_day_sin", pa.float32(), nullable=False),
    pa.field("target_minute_of_day_cos", pa.float32(), nullable=False),
    pa.field("target_dow_type", pa.string(), nullable=False),
]

_STATIC: Final[list[pa.Field]] = [
    pa.field("lat", pa.float64(), nullable=True),
    pa.field("lon", pa.float64(), nullable=True),
    # HELLO のみ。ドコモは住所が無いので NULL（W3 プラン §9.5）
    pa.field("pref_code", pa.int16(), nullable=True),
    pa.field("muni_code", pa.int32(), nullable=True),
    # ドコモのみ。名前は引けないので整数のカテゴリとして使う（W3-07a）
    pa.field("region_id", pa.int32(), nullable=True),
    pa.field("is_charging_station", pa.bool_(), nullable=True),
    pa.field("station_age_days", pa.float32(), nullable=False),
    # HELLO は宣言値、ドコモは `max(bikes + docks)` の累積（未来を覗かない）
    pa.field("capacity", pa.int16(), nullable=True),
    pa.field("urban_density_500m", pa.int32(), nullable=False),
]

_STATE: Final[list[pa.Field]] = [
    pa.field("bikes", pa.int16(), nullable=False),
    pa.field("docks", pa.int16(), nullable=False),
    # `capacity − bikes − docks`。**HELLO でだけ意味を持ち、負にもなる**（§3.5）
    pa.field("gap", pa.float32(), nullable=True),
    pa.field("fill_ratio", pa.float32(), nullable=True),
    pa.field("is_over_capacity", pa.bool_(), nullable=True),
    # ドコモは恒常的に 0 ＝**情報が無い**ので NULL にする（データ辞書 §11）
    pa.field("staleness_s", pa.int16(), nullable=True),
    # `t − observed_at`。as-of がどれだけ古い観測を指したか
    pa.field("feed_delay_s", pa.int32(), nullable=False),
]

_LAGS: Final[list[pa.Field]] = [
    *[pa.field(f"bikes_lag_{minutes}", pa.int16(), nullable=True) for minutes in LAG_MINUTES],
    pa.field("delta_15", pa.int16(), nullable=True),
    pa.field("delta_30", pa.int16(), nullable=True),
    pa.field("delta_60", pa.int16(), nullable=True),
    pa.field("roll_mean_60", pa.float32(), nullable=True),
    pa.field("roll_min_60", pa.int16(), nullable=True),
    pa.field("roll_max_60", pa.int16(), nullable=True),
]

_FLOW: Final[list[pa.Field]] = [
    # **フィード本来の周期で計算してからグリッドに写す**（W3-11）
    pa.field("rentals_60", pa.int32(), nullable=False),
    pa.field("returns_60", pa.int32(), nullable=False),
    pa.field("n_changes_60", pa.int32(), nullable=False),
    pa.field("minutes_since_last_change", pa.float32(), nullable=True),
]

_HISTORY: Final[list[pa.Field]] = [
    pa.field("bikes_same_time_1d", pa.int16(), nullable=True),
    pa.field("y_bike_same_time_1d", pa.int8(), nullable=True),
    pa.field("y_dock_same_time_1d", pa.int8(), nullable=True),
]

_NEIGHBORS: Final[list[pa.Field]] = [
    # **近傍 0 は実データ**。合計と個数は 0、平均は NULL（W3 プラン §9.5）
    pa.field("nb_count_300m", pa.int32(), nullable=False),
    pa.field("nb_count_500m", pa.int32(), nullable=False),
    pa.field("nb_bikes_sum_300m", pa.int32(), nullable=False),
    pa.field("nb_docks_sum_300m", pa.int32(), nullable=False),
    pa.field("nb_n_empty_500m", pa.int32(), nullable=False),
    pa.field("nb_fill_ratio_mean_500m", pa.float32(), nullable=True),
    # 同一システムのみ。HELLO とドコモは利用者がふつう乗り換えられない（§4.3 の 22）
    pa.field("nb_count_300m_same", pa.int32(), nullable=False),
    pa.field("nb_bikes_sum_300m_same", pa.int32(), nullable=False),
    pa.field("nb_docks_sum_300m_same", pa.int32(), nullable=False),
]

_WEATHER: Final[list[pa.Field]] = [
    # **`t` を含む 1 時間帯の降水量**（直前 1 時間の合計。`features/weather.py`）
    pa.field("precip_mm_now", pa.float32(), nullable=True),
    # **目標時刻（`t + h`）を含む 1 時間帯の降水量。** 水平ごとに値が変わる唯一の天気列
    pa.field("precip_mm_target", pa.float32(), nullable=True),
    # `t` に最も近い毎正時の瞬時値
    pa.field("temp_c", pa.float32(), nullable=True),
    # 同上。**単位は km/h**（Open-Meteo の `wind_speed_10m` がそう返す）
    pa.field("wind_kmh", pa.float32(), nullable=True),
]

#: 天気の列名。**`_WEATHER` から引く**（正を 2 つにしない）。被覆を測る
#: `features/coverage.py` が使う——「どれが天気か」を別の場所に書き写すと、
#: 列を足したときに片方だけが増える。
WEATHER_COLUMNS: Final[tuple[str, ...]] = tuple(field.name for field in _WEATHER)

SCHEMA: Final[pa.Schema] = pa.schema(
    [
        *_KEYS,
        *_LABELS,
        *_SAMPLING,
        *_CALENDAR,
        *_STATIC,
        *_STATE,
        *_LAGS,
        *_FLOW,
        *_HISTORY,
        *_NEIGHBORS,
        *_WEATHER,
    ]
)

#: 推論では作らない列。**ラベルは未来、抽出は学習の都合**（W4 プラン §9 の契約 5）。
SERVING_DROPPED: Final[tuple[str, ...]] = ("y_bike", "y_dock", "weight", "stratum")

#: 推論の出力の列。**`SCHEMA` から引いて作る**（正を 2 つにしない）。
SERVING_SCHEMA: Final[pa.Schema] = pa.schema(
    [field for field in SCHEMA if field.name not in SERVING_DROPPED]
)

#: 学習に使ってはいけない列（開発プラン §6.3、データ辞書 §10.4）。キーと重みと版。
NON_FEATURE_COLUMNS: Final[tuple[str, ...]] = (
    "system_id",
    "station_id",
    "t",
    "y_bike",
    "y_dock",
    "weight",
    "stratum",
    "feature_set",
)


def feature_columns() -> tuple[str, ...]:
    """学習に入れてよい列。**`system_id` はカテゴリとして別途渡す**（ここには含めない）。"""
    return tuple(name for name in SCHEMA.names if name not in NON_FEATURE_COLUMNS)


class MissingColumnError(ValueError):
    """`SCHEMA` の列がそろっていない。**黙って落とさない。**"""


def to_table(columns: Mapping[str, pa.Array]) -> pa.Table:
    """列の辞書を表にする。**`SCHEMA` と過不足があれば例外にする。**"""
    return _to_table(columns, SCHEMA)


def to_serving_table(columns: Mapping[str, pa.Array]) -> pa.Table:
    """推論の列の辞書を表にする。**ラベルと抽出が混じっていれば例外にする。**"""
    return _to_table(columns, SERVING_SCHEMA)


def _to_table(columns: Mapping[str, pa.Array], schema: pa.Schema) -> pa.Table:
    missing = [name for name in schema.names if name not in columns]
    extra = [name for name in columns if name not in schema.names]
    if missing or extra:
        raise MissingColumnError(f"足りない列: {missing} / 余分な列: {extra}")
    return pa.table([columns[name] for name in schema.names], schema=schema)
