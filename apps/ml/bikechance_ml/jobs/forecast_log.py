"""予測ログ（開発プラン §8.2 の手順 7・D-24、W5 プラン §6.1）。

**配った確率を、あとから測れる形で残す。** `station_forecasts` は 1 ポート 1 行で
**5 分毎に上書きされる**ので、記録しなかったサイクルは「作り直す」しかない——そして
作り直しには `status_snapshots`（60 日）と `weather_hourly`（**30 日**）が要る。
**30 日の導火線がある**（W5-10）。

**形は W4 の PR N で決めた**（W4 プラン §6.8）。本番の予測で測ってから決めたもので、
ここは実装だけである。決めたことのうち、コードで守るのは 4 つ。

  * **1 サイクル 1 ファイル。** パスに基準時刻と版が入る
  * **定数は列に持つ**（覚え書きではなく）。**1 日ぶんを `concat_tables` した瞬間に
    覚え書きは消える**ので、どの行がどの基準時刻かが分からなくなる
  * **`station_id` の昇順で書く。** 並びが圧縮に効く（実測 **+24.7%**）
  * **`active` と `shadow` の両方が書く。** 昇格の判定は同じ基準時刻で並んでいないと
    できない（開発プラン §8.4）

**ここは純粋な部分だけを持つ。** Storage に置くのは `jobs/infer.py` の仕事で、
**置けなくても推論は落とさない**（W3-18。配ることのほうが大事で、ログは作り直せる）。
置き場（`FORECAST_LOG_BUCKET`）とその理由は `io/supabase.py` にある。
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol

import pyarrow as pa

from bikechance_ml.features.constants import HORIZONS_MIN
from bikechance_ml.jobs.build_features import to_parquet_bytes

#: 書式の版。**読み方を変えたら上げる。** 覚え書きに入る。
FORMAT_VERSION: Final[int] = 1

#: パスに使ってよい名前。**`/` や `..` を含む名前を黙って別の場所に置かない。**
#: `system_id` は 2 つの定数、`model_version` は `model_versions` の主キーで、
#: どちらも実際にはここに触れない。**触れたときに止まることが大事である。**
_SAFE_NAME: Final[re.Pattern[str]] = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

#: 列と型の契約（W4 プラン §6.8 の表）。
#:
#: **`base_observed_at` と `model_version` は素の `string`。** Arrow の辞書型にしても
#: 大きさは変わらず（実測 129,251 B 対 129,507 B。**素のほうが小さい**）、Parquet の
#: 書き手がどのみち辞書符号化する。**型を 1 つ単純にしておく。**
#:
#: **`system_id` は列に入れない。** 1 ファイルは 1 システムぶんで、パスと覚え書きに
#: 入っている。
SCHEMA: Final[pa.Schema] = pa.schema(
    [
        pa.field("station_id", pa.string(), nullable=False),
        # **結合の鍵。** ISO 8601・UTC。1 ファイルの中では 1 つの値しか取らない
        pa.field("base_observed_at", pa.string(), nullable=False),
        # `active` と `shadow` が同じ日に並ぶので、行から版が引けなければならない
        pa.field("model_version", pa.string(), nullable=False),
        # **配列の何番目が何分先かを、ファイル自身が言える**ようにする（+528 B）
        pa.field("horizons_min", pa.list_(pa.int16()), nullable=False),
        pa.field("p_bike_x1000", pa.list_(pa.int16()), nullable=False),
        pa.field("p_dock_x1000", pa.list_(pa.int16()), nullable=False),
        # **信頼度で切った指標**を出すために要る（0026 と同じ 0〜3）
        pa.field("confidence", pa.int8(), nullable=False),
    ]
)


class UnsafeNameError(ValueError):
    """パスに使えない名前が来た。**黙って別の場所に置かない。**"""


class HorizonMismatchError(ValueError):
    """確率の配列の長さが水平の数と違う。**ずれたまま記録しない。**

    ずれたログは**読むときに気づけない**（配列の何番目が何分先かが狂うだけで、
    形は正しい）。書く前に止める。
    """


class Predicted(Protocol):
    """1 ポートぶんの予測。`jobs/infer.py` の `Forecast` がこれを満たす。

    **`Forecast` を import しない。** すると `infer` → `forecast_log` → `infer` の
    輪ができる。要るのは 4 つの値だけなので、**形で受け取る**。
    """

    @property
    def station_id(self) -> str: ...
    @property
    def p_bike_x1000(self) -> tuple[int, ...]: ...
    @property
    def p_dock_x1000(self) -> tuple[int, ...]: ...
    @property
    def confidence(self) -> int: ...


@dataclass(frozen=True)
class LogFile:
    """置く 1 ファイル。**パスと中身を一緒に作る。**

    別々に作れる形にすると、パスの `model_version` と中身の `model_version` が
    ずれる道ができる（契約 4 と同じ「同じものを 2 か所に置かない」）。
    """

    path: str
    body: bytes
    rows: int


def log_path(system_id: str, base_observed_at: datetime, model_version: str) -> str:
    """バケット内のパス。日時は **UTC**（`features/` の JST とは違う）。

    **UTC なのは、結合する相手が `gbfs-parquet` の実測だから**である（W4 プラン §6.8）。
    `hour=` を挟むのは 1 階層の一覧を 12 ファイル（× モデル数）に保つため。
    """
    at = _safe(base_observed_at)
    return (
        f"{_checked(system_id)}/date={at:%Y-%m-%d}/hour={at:%H}/"
        f"{int(at.timestamp())}_{_checked(model_version)}.parquet"
    )


def file_metadata(*, system_id: str, generated_at: datetime, feature_set: str) -> dict[str, str]:
    """ファイルの覚え書き。**結合に使わないものだけ**を入れる（+765 B）。

    結合に使うもの（基準時刻・版・水平）は**列**に持つ。1 日ぶんを `concat_tables`
    した瞬間に覚え書きは消えるためで、そこは PR N が測って決めた。
    """
    return {
        "format_version": str(FORMAT_VERSION),
        "system_id": system_id,
        "generated_at": _safe(generated_at).isoformat(),
        "feature_set": feature_set,
    }


def to_table(
    forecasts: Sequence[Predicted], *, base_observed_at: datetime, model_version: str
) -> pa.Table:
    """予測を表にする。**`station_id` の昇順に並べ直す。**

    `build_now` は既に `station_id` 昇順で返す（`features/build.py`）ので、ここは
    **並びを保証する見張り**である。ばらばらのまま書くと **+24.7%** 太る（実測）。
    """
    stamp = _safe(base_observed_at).isoformat()
    ordered = sorted(forecasts, key=lambda one: one.station_id)
    horizons = list(HORIZONS_MIN)
    return pa.table(
        {
            "station_id": [one.station_id for one in ordered],
            "base_observed_at": [stamp] * len(ordered),
            "model_version": [model_version] * len(ordered),
            "horizons_min": [horizons] * len(ordered),
            "p_bike_x1000": [_aligned(one.p_bike_x1000, one.station_id) for one in ordered],
            "p_dock_x1000": [_aligned(one.p_dock_x1000, one.station_id) for one in ordered],
            "confidence": [one.confidence for one in ordered],
        },
        schema=SCHEMA,
    )


def to_bytes(table: pa.Table, metadata: Mapping[str, str]) -> bytes:
    """覚え書きを付けて Parquet のバイト列にする。

    **書き方は `to_parquet_bytes` の 1 つだけ**（zstd）。ここで別の書き方を作ると、
    圧縮や版が学習用 Parquet とずれても誰も気づかない。
    """
    return to_parquet_bytes(table.replace_schema_metadata(dict(metadata)))


def build(
    forecasts: Sequence[Predicted],
    *,
    system_id: str,
    base_observed_at: datetime,
    generated_at: datetime,
    model_version: str,
    feature_set: str,
) -> LogFile:
    """置く 1 ファイルを作る。**パスと中身を同じ引数から作る。**"""
    table = to_table(forecasts, base_observed_at=base_observed_at, model_version=model_version)
    metadata = file_metadata(
        system_id=system_id, generated_at=generated_at, feature_set=feature_set
    )
    return LogFile(
        path=log_path(system_id, base_observed_at, model_version),
        body=to_bytes(table, metadata),
        rows=table.num_rows,
    )


def _checked(name: str) -> str:
    """パスの 1 区画として安全な名前か。"""
    if _SAFE_NAME.fullmatch(name) is None:
        raise UnsafeNameError(f"パスに使えない名前です（長さ {len(name)}）")
    return name


def _safe(at: datetime) -> datetime:
    """**素の日時を受け取らない。** UTC に直してから使う。"""
    if at.tzinfo is None:
        raise ValueError("タイムゾーンの無い日時は受け取りません")
    return at.astimezone(UTC)


def _aligned(values: tuple[int, ...], station_id: str) -> tuple[int, ...]:
    """確率の配列。**水平の数と合わなければ止める。**

    **写さずにそのまま返す。** pyarrow は任意の並びを受け取るので、`list()` に
    直す意味が無い（実測：14,851 行で 1.7 ms × 2 列ぶんの無駄になる）。
    """
    if len(values) != len(HORIZONS_MIN):
        raise HorizonMismatchError(
            f"{station_id}: 確率が {len(values)} 個、水平は {len(HORIZONS_MIN)} 個"
        )
    return values
