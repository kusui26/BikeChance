"""生アーカイブから `weather_hourly` を組み直せることを確かめる（W4 プラン §6.8 の PR L）。

**書き込まない。** 読んで、組み立てて、入っている行と突き合わせるだけである。

**なぜ要るか。** 学習サンプルは `status_snapshots`（60 日）と `weather_hourly`（30 日）から
作り直せる——**30 日以内なら**。30 日を過ぎたら生アーカイブ（`weather-raw`、無期限）から
戻すしかないが、**その経路を一度も動かしていなかった**（§8.5.2）。「たぶん戻せる」で
持っている保険は、**要るときに初めて壊れていることが分かる**。

**期限がある。** `weather_hourly` にいちばん古く残っている発行は 2026-09-07 15:00 UTC で、
30 日保持なら **2026-10-07** に消える。**確かめるのはそれより前でなければ意味が無い**
（消えたあとでは、突き合わせる相手が無い）。

**行を消して試す必要は無い。** 取り込みの経路は 3 つに分かれる。

    ①読む（Storage → バイト列）   ②解く（バイト列 → 行）   ③入れる（行 → DB）

この検査は①②を通し、③の**結果**（いま入っている行）と突き合わせる。**③そのものは
毎時動いている**ので、①②が通れば経路は全部生きている。

使い方（環境変数は `.env` から読み込んでから）:

    set -a; . ./.env; set +a
    cd apps/ml
    ./.venv/bin/python -m bikechance_ml.jobs.verify_weather_archive

`--issued-hour` を省くと、**いちばん古い発行**（次に消えるもの）を見る。
手順は `docs/260912_runbook_weather_restore.md`。
"""

import argparse
import json
import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol

from bikechance_ml.config import read_storage_config
from bikechance_ml.features.weather import WeatherRow
from bikechance_ml.io.supabase import open_storage
from bikechance_ml.jobs.weather_archive import (
    WEATHER_BUCKET,
    CellForecast,
    read_batch,
    weather_object_path,
)

#: 分割を探しにいく上限。1 発行は 595 格子 ÷ 100 = 6 分割なので、**十分に余裕がある**。
#: 上限を置くのは、Storage が 404 以外を返し続けたときに無限に読まないため。
MAX_BATCHES: Final[int] = 32

#: 報告に並べる食い違いの上限。**全部は出さない**（595 格子 × 4 系列 × 8 時間ある）。
MAX_REPORTED: Final[int] = 20

#: 格子の鍵。
type Cell = tuple[int, int]


class NoIssueError(RuntimeError):
    """突き合わせる発行が無い。**無いものを「一致した」と言わない。**"""


class VerifyPort(Protocol):
    """この検査が使う入出力だけ。**読む口しか持たない**（書けない形にしてある）。"""

    def oldest_weather_issue(self) -> datetime | None: ...

    def list_weather_issue(self, issued_hour: datetime) -> tuple[WeatherRow, ...]: ...

    def download(self, bucket: str, path: str) -> bytes | None: ...


@dataclass(frozen=True)
class Mismatch:
    """1 つの値の食い違い。**どこがどう違うかを名指しする。**"""

    cell: Cell
    series: str
    lead: int
    in_archive: float | None
    in_table: float | None

    def as_dict(self) -> dict[str, object]:
        return {
            "cell": list(self.cell),
            "series": self.series,
            "lead": self.lead,
            "archive": self.in_archive,
            "table": self.in_table,
        }


@dataclass(frozen=True)
class Verified:
    """1 発行ぶんの突き合わせ。"""

    issued_hour: str
    n_batches: int
    n_cells_archive: int
    n_cells_table: int
    n_values: int
    mismatches: tuple[Mismatch, ...]
    only_in_archive: tuple[Cell, ...]
    only_in_table: tuple[Cell, ...]

    @property
    def ok(self) -> bool:
        """**格子の集合が同じで、値が 1 つも違わない。** 片方でも欠ければ偽。"""
        return not (self.mismatches or self.only_in_archive or self.only_in_table)

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "issued_hour": self.issued_hour,
            "batches": self.n_batches,
            "cells_archive": self.n_cells_archive,
            "cells_table": self.n_cells_table,
            "values_compared": self.n_values,
            "n_mismatches": len(self.mismatches),
            "only_in_archive": [list(one) for one in self.only_in_archive],
            "only_in_table": [list(one) for one in self.only_in_table],
            "mismatches": [one.as_dict() for one in self.mismatches[:MAX_REPORTED]],
        }


def read_archive(source: VerifyPort, issued_hour: datetime) -> tuple[tuple[CellForecast, ...], int]:
    """分割を 0 番から、無くなるまで読む。**何分割あったかも返す。**

    **分割数を DB から貰わない。** `v_weather_pending` は取り込み済みの発行を返さないし、
    行数から割り戻すと「取り込めなかった格子」を勘定に入れ損ねる。**在るものを数える。**
    """
    hour_epoch_s = int(issued_hour.timestamp())
    forecasts: list[CellForecast] = []
    batches = 0
    for batch in range(MAX_BATCHES):
        body = source.download(WEATHER_BUCKET, weather_object_path(hour_epoch_s, batch))
        if body is None:
            break
        forecasts.extend(read_batch(body, issued_hour))
        batches = batch + 1
    return tuple(forecasts), batches


def same_value(in_archive: float, in_table: float | None) -> bool:
    """**NaN（アーカイブ）と NULL（表）は同じ「無い」。** 数どうしは完全一致を求める。

    `real[]` で往復しても値は変わらない：取り込みは `float` をそのまま JSON で送り、
    Postgres が `real`（単精度）に落とす。アーカイブ側も Open-Meteo が単精度で返した
    値なので、**丸めが入る余地が無い**。許容を置くと、**本当にずれたときに気づけない。**
    """
    if math.isnan(in_archive):
        return in_table is None
    return in_table is not None and in_archive == in_table


def _compare_cell(cell: Cell, archive: CellForecast, table: WeatherRow) -> list[Mismatch]:
    """1 格子ぶん。**系列も時間帯も、片方にしか無ければ食い違いとして出す。**"""
    found: list[Mismatch] = []
    for series, values in sorted(archive.values.items()):
        in_table = list(table.values.get(series, ()))
        for lead, one in enumerate(values):
            other = in_table[lead] if lead < len(in_table) else None
            if not same_value(one, other):
                found.append(Mismatch(cell, series, lead, _as_json(one), other))
    return found


def _as_json(value: float) -> float | None:
    """NaN を `None` に直す（報告は JSON にする）。"""
    return None if math.isnan(value) else value


def _n_values(forecasts: Sequence[CellForecast]) -> int:
    return sum(len(values) for one in forecasts for values in one.values.values())


def compare(
    issued_hour: datetime,
    forecasts: Sequence[CellForecast],
    rows: Sequence[WeatherRow],
    batches: int,
) -> Verified:
    """アーカイブから組み立てた格子と、表に入っている格子を突き合わせる。"""
    archive = {(one.cell_lat_idx, one.cell_lon_idx): one for one in forecasts}
    table = {(one.cell_lat_idx, one.cell_lon_idx): one for one in rows}
    shared = sorted(set(archive) & set(table))
    return Verified(
        issued_hour=issued_hour.isoformat(),
        n_batches=batches,
        n_cells_archive=len(archive),
        n_cells_table=len(table),
        n_values=_n_values(forecasts),
        mismatches=tuple(
            one for cell in shared for one in _compare_cell(cell, archive[cell], table[cell])
        ),
        only_in_archive=tuple(sorted(set(archive) - set(table))),
        only_in_table=tuple(sorted(set(table) - set(archive))),
    )


def verify(source: VerifyPort, issued_hour: datetime | None = None) -> Verified:
    """1 発行ぶんを確かめる。**発行を指定しなければ、次に消えるものを見る。**"""
    target = issued_hour if issued_hour is not None else source.oldest_weather_issue()
    if target is None:
        raise NoIssueError("weather_hourly に発行が 1 つもありません")
    rows = source.list_weather_issue(target)
    if not rows:
        raise NoIssueError(f"その発行が weather_hourly にありません: {target.isoformat()}")
    forecasts, batches = read_archive(source, target)
    return compare(target, forecasts, rows, batches)


# ── 実行 ──────────────────────────────────────────────────────
def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生アーカイブから weather_hourly を組み直せるか")
    parser.add_argument(
        "--issued-hour", default=None, help="発行の時刻（既定はいちばん古い＝次に消えるもの）"
    )
    return parser.parse_args(argv)


def run(argv: Sequence[str] | None = None) -> int:
    options = _arguments(argv)
    issued = (
        datetime.fromisoformat(options.issued_hour).astimezone(UTC) if options.issued_hour else None
    )
    with open_storage(read_storage_config()) as source:
        outcome = verify(source, issued)
    print(json.dumps(outcome.as_dict(), ensure_ascii=False, indent=2))
    if not outcome.ok:
        print("**一致しませんでした。** 生アーカイブから戻せない状態です", file=sys.stderr)
    return 0 if outcome.ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
