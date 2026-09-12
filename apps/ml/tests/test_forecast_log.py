"""予測ログの形（`jobs/forecast_log.py`）。

**主題は「1 年後に読んで、意味が変わっていないこと」。** 予測ログは 12 か月残り、
実運用 Brier と昇格の判定がここから出る（開発プラン §8.4・§8.5）。読むのはずっと
後なので、**書いた本人が居ないところで壊れないか**を機械で固定する。

固定するのは 4 つ。

  * **定数は列に入っている**——1 日ぶんを `concat_tables` した瞬間に覚え書きは消える
  * **`station_id` の昇順**——並びが圧縮に効く（実測 +24.7%）
  * **パスは UTC で、基準時刻と版から決まる**
  * **ずれたものは書かない**（水平の数・パスに使えない名前）
"""

import gzip
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from bikechance_ml.features.constants import HORIZONS_MIN
from bikechance_ml.features.grid import JST
from bikechance_ml.jobs import forecast_log

BASE: Final[datetime] = datetime(2026, 9, 13, 4, 49, 34, tzinfo=UTC)
GENERATED: Final[datetime] = datetime(2026, 9, 13, 4, 50, tzinfo=UTC)
MODEL: Final[str] = "baseline-b3-v0-20260908"


@dataclass(frozen=True)
class Row:
    """`jobs/infer.py` の `Forecast` と同じ形（`Predicted` を満たす）。"""

    station_id: str
    p_bike_x1000: tuple[int, ...]
    p_dock_x1000: tuple[int, ...]
    confidence: int


def rows(*station_ids: str) -> tuple[Row, ...]:
    """水平の数だけ確率を持つ行を作る。**値はポートごとに変える。**"""
    return tuple(
        Row(
            station_id=one,
            p_bike_x1000=tuple(index * 10 + step for step in range(len(HORIZONS_MIN))),
            p_dock_x1000=tuple(index * 10 + step * 2 for step in range(len(HORIZONS_MIN))),
            confidence=2 + index % 2,
        )
        for index, one in enumerate(station_ids)
    )


def built(*station_ids: str) -> forecast_log.LogFile:
    return forecast_log.build(
        rows(*station_ids),
        system_id="hellocycling",
        base_observed_at=BASE,
        generated_at=GENERATED,
        model_version=MODEL,
        feature_set="v3",
    )


def read(file: forecast_log.LogFile) -> pa.Table:
    return pq.read_table(pa.BufferReader(file.body))


# ── パス ──────────────────────────────────────────────────────
def test_the_path_carries_the_base_time_and_the_version() -> None:
    """**1 サイクル 1 ファイル。** 基準時刻と版が違えば別のファイルになる。"""
    assert forecast_log.log_path("hellocycling", BASE, MODEL) == (
        f"hellocycling/date=2026-09-13/hour=04/{int(BASE.timestamp())}_{MODEL}.parquet"
    )


def test_the_path_is_utc_even_when_given_jst() -> None:
    """**日付と時は UTC**（`features/` の JST とは違う）。結合する相手が UTC だから。"""
    same_moment = BASE.astimezone(JST)
    assert forecast_log.log_path("hellocycling", same_moment, MODEL) == forecast_log.log_path(
        "hellocycling", BASE, MODEL
    )


def test_a_naive_time_is_refused() -> None:
    """**素の日時を受け取らない。** UTC と決めつけると 9 時間ずれる。"""
    with pytest.raises(ValueError, match="タイムゾーン"):
        forecast_log.log_path("hellocycling", BASE.replace(tzinfo=None), MODEL)


@pytest.mark.parametrize(
    "name",
    ["", "a/b", "../etc", ".hidden", "a" * 65, "版", "a b"],
)
def test_a_name_that_cannot_be_a_path_segment_is_refused(name: str) -> None:
    """**黙って別の場所に置かない。** `/` を含む版が来たら階層が増えてしまう。"""
    with pytest.raises(forecast_log.UnsafeNameError):
        forecast_log.log_path(name, BASE, MODEL)
    with pytest.raises(forecast_log.UnsafeNameError):
        forecast_log.log_path("hellocycling", BASE, name)


def test_the_hour_folder_keeps_one_listing_small() -> None:
    """`hour=` を挟むので、**1 階層は 1 つの版につき 12 ファイル**（5 分 × 12）に収まる。

    1 日ぶん（288 サイクル）を数えて確かめる。**一覧が 288 件の階層を作らない**
    のがねらいで、あとから 1 日ぶんを読むときに効く。
    """
    day_start = BASE.replace(hour=0, minute=0, second=0)
    folders: dict[str, int] = {}
    for step in range(288):
        path = forecast_log.log_path("hellocycling", day_start + timedelta(minutes=5 * step), MODEL)
        folders[path.rsplit("/", 1)[0]] = folders.get(path.rsplit("/", 1)[0], 0) + 1
    assert len(folders) == 24
    assert set(folders.values()) == {12}


# ── 列 ────────────────────────────────────────────────────────
def test_the_schema_is_the_contract() -> None:
    """**列と型は W4 プラン §6.8 の表のまま。** 変えたら `FORMAT_VERSION` を上げる。"""
    assert read(built("a", "b")).schema.remove_metadata() == forecast_log.SCHEMA


def test_the_constants_are_columns_not_metadata() -> None:
    """**1 日ぶんを `concat_tables` した瞬間に覚え書きは消える。**

    どの行がどの基準時刻のものかが分からなければ、実測と結合できない（PR N）。
    """
    table = read(built("a", "b"))
    assert table.column("base_observed_at").to_pylist() == [BASE.isoformat()] * 2
    assert table.column("model_version").to_pylist() == [MODEL] * 2
    assert table.column("horizons_min").to_pylist() == [list(HORIZONS_MIN)] * 2


def test_a_days_worth_can_be_joined_and_split_again() -> None:
    """**288 サイクルを 1 つの表にして、基準時刻で割り直せる。** 日次評価がこれをする。"""
    parts = [
        read(
            forecast_log.build(
                rows("a", "b"),
                system_id="hellocycling",
                base_observed_at=BASE + timedelta(minutes=5 * step),
                generated_at=GENERATED + timedelta(minutes=5 * step),
                model_version=MODEL,
                feature_set="v3",
            )
        )
        for step in range(3)
    ]
    joined = pa.concat_tables(parts)
    assert joined.num_rows == 6
    assert len(set(joined.column("base_observed_at").to_pylist())) == 3


def test_the_system_is_not_a_column() -> None:
    """1 ファイルは 1 システムぶん。**パスと覚え書きに入っているので列にしない。**"""
    assert "system_id" not in read(built("a")).column_names


# ── 並び ──────────────────────────────────────────────────────
def test_the_rows_come_out_sorted_by_station_id() -> None:
    """**並びが圧縮に効く**（実測 +24.7%）。入力がばらばらでも昇順で書く。"""
    file = forecast_log.build(
        rows("c", "a", "b"),
        system_id="hellocycling",
        base_observed_at=BASE,
        generated_at=GENERATED,
        model_version=MODEL,
        feature_set="v3",
    )
    assert read(file).column("station_id").to_pylist() == ["a", "b", "c"]


def test_sorting_moves_the_probabilities_with_the_port() -> None:
    """**並べ替えで値が別のポートに付いてはいけない。**"""
    shuffled = rows("c", "a", "b")
    table = read(
        forecast_log.build(
            shuffled,
            system_id="hellocycling",
            base_observed_at=BASE,
            generated_at=GENERATED,
            model_version=MODEL,
            feature_set="v3",
        )
    )
    by_id = {one.station_id: one for one in shuffled}
    for index, station_id in enumerate(table.column("station_id").to_pylist()):
        assert table.column("p_bike_x1000")[index].as_py() == list(by_id[station_id].p_bike_x1000)
        assert table.column("p_dock_x1000")[index].as_py() == list(by_id[station_id].p_dock_x1000)
        assert table.column("confidence")[index].as_py() == by_id[station_id].confidence


# ── 覚え書き ──────────────────────────────────────────────────
def test_the_metadata_says_when_and_with_what() -> None:
    """**結合に使わないものだけ**を覚え書きに入れる（PR N）。"""
    assert read(built("a")).schema.metadata == {
        b"format_version": b"1",
        b"system_id": b"hellocycling",
        b"generated_at": GENERATED.isoformat().encode(),
        b"feature_set": b"v3",
    }


def test_the_format_version_says_how_to_read_it() -> None:
    assert forecast_log.FORMAT_VERSION == 1


# ── 拒む ──────────────────────────────────────────────────────
def test_a_short_probability_array_is_refused() -> None:
    """**ずれたログは読むときに気づけない。** 形は正しく、意味だけが狂う。"""
    short = Row(station_id="a", p_bike_x1000=(1, 2), p_dock_x1000=(1, 2), confidence=2)
    with pytest.raises(forecast_log.HorizonMismatchError, match="a: 確率が 2 個"):
        forecast_log.to_table([short], base_observed_at=BASE, model_version=MODEL)


def test_a_long_probability_array_is_refused() -> None:
    values = tuple(range(len(HORIZONS_MIN) + 1))
    long = Row(station_id="a", p_bike_x1000=values, p_dock_x1000=values, confidence=2)
    with pytest.raises(forecast_log.HorizonMismatchError):
        forecast_log.to_table([long], base_observed_at=BASE, model_version=MODEL)


def test_the_dock_array_is_checked_too() -> None:
    """**片側だけ見ない。** 返却側がずれても同じように静かに狂う。"""
    good = tuple(range(len(HORIZONS_MIN)))
    lopsided = Row(station_id="a", p_bike_x1000=good, p_dock_x1000=(1,), confidence=2)
    with pytest.raises(forecast_log.HorizonMismatchError):
        forecast_log.to_table([lopsided], base_observed_at=BASE, model_version=MODEL)


# ── 中身 ──────────────────────────────────────────────────────
def test_an_empty_cycle_still_writes_a_file() -> None:
    """**1 サイクル 1 ファイル。** 何も出せなかったことも記録である。

    ファイルが無いことは「置けなかった」を意味するようにしておく——**欠落 0 を
    数えられる**のはこの約束のおかげである（§8.2 の合格判定）。
    """
    file = built()
    assert file.rows == 0
    assert read(file).num_rows == 0
    assert read(file).schema.remove_metadata() == forecast_log.SCHEMA


def test_the_same_input_gives_the_same_bytes() -> None:
    """**再現性。** 同じ観測に対する 2 度目は同じパスに同じ中身を上書きする。"""
    assert built("a", "b").body == built("a", "b").body


def test_the_file_is_zstd_parquet() -> None:
    """**書き方は `to_parquet_bytes` の 1 つだけ**（学習用 Parquet と同じ圧縮）。"""
    metadata = pq.ParquetFile(pa.BufferReader(built("a").body)).metadata
    compressions = {
        metadata.row_group(0).column(index).compression for index in range(metadata.num_columns)
    }
    assert compressions == {"ZSTD"}
    with pytest.raises(gzip.BadGzipFile):
        gzip.decompress(built("a").body)


def test_the_row_count_matches_the_ports() -> None:
    """**1 ポート 1 行**（水平は配列）。行数で取りこぼしを数えられる。"""
    file = built("a", "b", "c")
    assert file.rows == 3
    assert read(file).num_rows == 3
