"""水平とグリッドの契約（`features/constants.py`）。

**この 1 ファイルの主題は「TypeScript と同じ値を持つこと」。** 水平は `/v1` の応答にも
モデルの入力にも出る。ここがずれると、配信している確率の意味が学習時と変わる。

暦の規則が `fixtures/calendar/day_type_golden.csv` で突き合わせてあるのと同じ理由で、
**定義そのものを機械で照合する**。
"""

import re
from pathlib import Path
from typing import Final

from bikechance_ml.features.constants import (
    DELTA_MINUTES,
    GRID_MINUTES,
    GRID_POINTS_PER_DAY,
    HORIZONS_MIN,
    LAG_MINUTES,
    ROLL_MINUTES,
    SAME_TIME_MINUTES,
    TIGHT_RATE,
    UNIFORM_RATE,
)

CONSTANTS_TS: Final[Path] = (
    Path(__file__).resolve().parents[3] / "packages" / "shared" / "src" / "constants.ts"
)


def typescript_horizons() -> tuple[int, ...]:
    """`packages/shared/src/constants.ts` の `HORIZONS_MIN` を読む。"""
    text = CONSTANTS_TS.read_text(encoding="utf-8")
    found = re.search(r"export const HORIZONS_MIN = \[([^\]]*)\]", text)
    assert found is not None, "HORIZONS_MIN が constants.ts に見つからない"
    return tuple(int(one) for one in found.group(1).replace(" ", "").split(","))


def test_horizons_match_typescript() -> None:
    assert typescript_horizons() == HORIZONS_MIN


def test_horizons_fit_the_grid() -> None:
    """**すべての水平がグリッドの倍数**でなければ、`t + h` が格子に乗らない。"""
    assert all(horizon % GRID_MINUTES == 0 for horizon in HORIZONS_MIN)
    assert tuple(sorted(HORIZONS_MIN)) == HORIZONS_MIN
    assert len(set(HORIZONS_MIN)) == len(HORIZONS_MIN)


def test_lookback_windows_fit_the_grid() -> None:
    """ラグ・差分・移動窓・同時刻履歴も格子から引く。"""
    windows = (*LAG_MINUTES, *DELTA_MINUTES, ROLL_MINUTES, SAME_TIME_MINUTES)
    assert all(minutes % GRID_MINUTES == 0 for minutes in windows)


def test_grid_covers_the_day() -> None:
    assert GRID_POINTS_PER_DAY * GRID_MINUTES == 24 * 60


def test_tight_rate_is_higher_than_uniform() -> None:
    """難所を厚く取る（開発プラン §6.2）。逆になっていたら重みの意味が反転する。"""
    assert TIGHT_RATE > UNIFORM_RATE > 0
    assert TIGHT_RATE < 1
