"""B2 の材料（ポートプロファイル）を読む（W5 プラン §6.4 の PR D）。

**当てはめる 2 つのジョブ（`fit_baseline` と `evaluate_baselines`）が同じ読み方をする**
ための 1 か所。読むのは 2 種類だけ：

  * `profiles/date=D/profile.parquet` … **学習の最終日 D の版**（D 当日までの累計）
  * `profiles/date=E/daily.parquet` … 学習の各日 E ぶん（**自分の日を引く**ため）

**最終日の版を渡すのは呼ぶ側の責任である。** 検証期間まで含む版を渡すと、B2 が検証日の
観測を見た状態で測ることになる（`profile_climatology.FromProfile` の注記）。

**プロファイルが無ければ `None` を返す。** 従来どおり `features/` の行から気候値を
作る（`climatology.FromSamples`）——**初日と、収集を始めたばかりの日のため**である
（W5 プラン §6.4 の完了条件 7）。
"""

import io
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from bikechance_ml.baselines import profile_climatology
from bikechance_ml.features import profile
from bikechance_ml.features.grid import profile_path
from bikechance_ml.io.supabase import PARQUET_BUCKET, SupabaseIo


def read_bytes(source: SupabaseIo | None, path: str, local: Path | None) -> bytes | None:
    """Storage か、`--local` のミラーから 1 つ読む。**無ければ `None`。**

    **学習サンプルも同じ規約で読む**（`evaluate_baselines._one_day`）。`--local` の下は
    Storage のパスをそのまま並べたものである（`.cache/features/date=…`、
    `.cache/profiles/date=…`）。
    """
    if local is not None:
        whole = local / path
        return whole.read_bytes() if whole.exists() else None
    if source is None:
        raise ValueError("Storage か --local のどちらかが要ります")
    return source.download(PARQUET_BUCKET, path)


def load(
    source: SupabaseIo | None,
    days: Sequence[date],
    local: Path | None,
    ports: Sequence[str],
) -> profile_climatology.FromProfile | None:
    """**学習の最終日の版**と、学習の各日ぶんを読む。無ければ `None`。"""
    if not days:
        return None
    body = read_bytes(source, profile_path(days[-1], profile.PROFILE_NAME), local)
    if body is None:
        return None
    kept = tuple(str(one) for one in ports)
    return profile_climatology.FromProfile(
        day=days[-1],
        profile=_read(body, profile.PROFILE_SCHEMA),
        dailies=_dailies(source, days, local, kept),
        ports=kept,
    )


def _dailies(
    source: SupabaseIo | None,
    days: Sequence[date],
    local: Path | None,
    ports: Sequence[str],
) -> profile_climatology.Dailies:
    """引ける日ぶんだけ集める。**無い日は入れない。**

    入れなかった日の行は**混合の当てはめから外れる**（`FromProfile.blend_rows`）。
    黙って引かずに混ぜると、B2 がその日の答えを見たまま係数に効く。
    """
    built: dict[int, profile_climatology.DayCells] = {}
    for day in days:
        body = read_bytes(source, profile_path(day, profile.DAILY_NAME), local)
        if body is not None:
            table = _read(body, profile.DAILY_SCHEMA)
            built[day.toordinal()] = profile_climatology.day_cells(table, ports)
    return profile_climatology.Dailies(by_day=built)


def _read(body: bytes, schema: pa.Schema) -> pa.Table:
    table = pq.read_table(io.BytesIO(body))
    profile.require_schema(table, schema)
    return table
