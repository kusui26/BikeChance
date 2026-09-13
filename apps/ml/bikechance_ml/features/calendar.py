"""暦の特徴量（開発プラン §6.3・§6.4、W3 プラン §5.6・§9.5）。

**`packages/shared/src/calendar.ts` と同じ規則を持つ。** 学習（ここ）と配信（TS）で
答えがずれると、モデルが見た世界と本番の世界が食い違う。同じであることは
`fixtures/calendar/day_type_golden.csv` で機械的に突き合わせる（`tests/test_calendar.py`）。

**規則の正は TypeScript 側**（ゴールデンを生成するのがあちら）。ここを変えるときは
向こうも変え、ゴールデンを作り直して差分をレビューする。

**年末年始（12/29〜1/3）とお盆（8/13〜16）は暦の規則で決まる**ので `jp_holidays` には
入れない。表に入れるのは内閣府 CSV の中身だけ（W3 プラン §12 の 87）。
"""

from collections.abc import Iterable, Set
from datetime import date, timedelta
from typing import Final, Literal

#: 日の種別（細かいほう）。開発プラン §6.4。
DayType = Literal["newyear", "obon", "holiday", "sun", "sat", "bridge", "weekday"]

#: 曜日の種別（粗いほう）。開発プラン §6.3 の `dow_type`。
#: 履歴プロファイルと気候値（B2）のセルはこの 3 値で切る。7 値にするとセルが埋まらない。
DowType = Literal["weekday", "sat", "sun_holiday"]

DAY_TYPES: Final[tuple[DayType, ...]] = (
    "newyear",
    "obon",
    "holiday",
    "sun",
    "sat",
    "bridge",
    "weekday",
)
DOW_TYPES: Final[tuple[DowType, ...]] = ("weekday", "sat", "sun_holiday")

#: 曜日種別を**番号で持つときの並び**。**`DOW_TYPES` の並びではない。**
#:
#: | 名前 | 番号 |
#: |---|---:|
#: | `sat` | 0 |
#: | `sun_holiday` | 1 |
#: | **`weekday`** | **2** |
#:
#: 気候値の成果物（`baselines/artifact.py`）も学習サンプルの読み込み（`eval/dataset.py`）も
#: 配信（`models/predictor.py`）も、**歴史的に `sorted()` を通した番号**で書かれている。
#: **`DOW_TYPES` の順に「直す」と、Storage に在る成果物が別のセルを指す**——例外は出ず、
#: 確率だけが静かに変わる（W5 プラン §2.3f、§12 の 132）。
#:
#: **直すべきなのは値ではなく、規約が 2 か所に無名で書かれていたこと**だった。番号を
#: 使う側はここを見る。名前に戻すのは `dow_type_name`。
DOW_TYPE_ORDER: Final[tuple[DowType, ...]] = tuple(sorted(DOW_TYPES))


def dow_type_name(index: int) -> DowType:
    """番号 → 名前。**報告書と診断はここを通す。**

    `DOW_TYPES[index]` と書くと 1 つずれた名前が出る（`weekday` が「日祝」になる）。
    実際に W5 プランを書くとき、成果物のセルを数えて最初に出した表がそうなっていた。
    """
    return DOW_TYPE_ORDER[index]


#: `dow_type` で日曜と同じ扱いにする種別。
_SUN_LIKE: Final[frozenset[str]] = frozenset({"sun", "holiday", "newyear", "obon"})

_SATURDAY: Final[int] = 5
_SUNDAY: Final[int] = 6


def is_new_year(day: date) -> bool:
    """年末年始（12/29〜1/3）。**暦の規則なので表に持たない。**"""
    return (day.month == 12 and day.day >= 29) or (day.month == 1 and day.day <= 3)


def is_obon(day: date) -> bool:
    """お盆（8/13〜16）。**暦の規則なので表に持たない。**"""
    return day.month == 8 and 13 <= day.day <= 16


def is_off(day: date, holidays: Set[date]) -> bool:
    """休みか（土日・祝日・年末年始・お盆）。飛び石と休前日の判定に使う。"""
    if is_new_year(day) or is_obon(day) or day in holidays:
        return True
    return day.weekday() in (_SATURDAY, _SUNDAY)


def _is_bridge(day: date, holidays: Set[date]) -> bool:
    """飛び石：**前日も翌日も休みの平日**。挟まれた 1 日は人の動きが休日に寄る。"""
    if is_off(day, holidays):
        return False
    return is_off(day - timedelta(days=1), holidays) and is_off(day + timedelta(days=1), holidays)


def day_type(day: date, holidays: Set[date]) -> DayType:
    """日の種別。**優先順は「期間 → 祝日 → 曜日 → 飛び石」。**

    年末年始とお盆を祝日より先に見るのは、期間全体が休業として振る舞うためで、
    「1/1 は元日でもあり年末年始でもある」を年末年始に寄せる。
    """
    if is_new_year(day):
        return "newyear"
    if is_obon(day):
        return "obon"
    if day in holidays:
        return "holiday"
    if day.weekday() == _SUNDAY:
        return "sun"
    if day.weekday() == _SATURDAY:
        return "sat"
    return "bridge" if _is_bridge(day, holidays) else "weekday"


def dow_type(day: DayType) -> DowType:
    """粗いほうの種別。プロファイルと気候値のセルはこれで切る。"""
    if day == "sat":
        return "sat"
    return "sun_holiday" if day in _SUN_LIKE else "weekday"


def is_day_before_holiday(day: date, holidays: Set[date]) -> bool:
    """翌日が休みか。夜の需要が伸びる（§6.3）。"""
    return is_off(day + timedelta(days=1), holidays)


def is_last_business_day(day: date, holidays: Set[date]) -> bool:
    """その月の**最後の営業日**か。給与日・締め日の人の動きを拾う（§6.3）。

    月末から遡って最初に見つかる非休日。月がまるごと休みということは無いので必ず 1 日ある。
    """
    if is_off(day, holidays):
        return False
    cursor = _last_day_of_month(day)
    while cursor >= day:
        if not is_off(cursor, holidays):
            return cursor == day
        cursor -= timedelta(days=1)
    return False


def _last_day_of_month(day: date) -> date:
    """その月の末日。翌月の 1 日から 1 日戻す（うるう年も 12 月もこれで足りる）。"""
    first_of_next = (
        date(day.year + 1, 1, 1) if day.month == 12 else date(day.year, day.month + 1, 1)
    )
    return first_of_next - timedelta(days=1)


def dates_between(start: date, end: date) -> Iterable[date]:
    """両端を含む日の並び。"""
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=1)
