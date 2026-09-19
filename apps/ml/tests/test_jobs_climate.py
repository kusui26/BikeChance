"""B2 の材料を読む（`jobs/climate.py`、W5 プラン §6.4 の PR D）。

**主題は「どの日の版を読むか」。** 検証期間まで含む版を読むと、B2 がその日の観測を
見た状態で測ることになる——例外は出ず、**数字だけが良くなる**。

`--local` の下は Storage のパスをそのまま並べたものなので、**ここで固定するのは
「学習の最終日の `profile` と、学習の各日の `daily`」**という 1 点である。
"""

from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from bikechance_ml.baselines import climatology, profile_climatology
from bikechance_ml.features import profile
from bikechance_ml.features.grid import features_path, profile_path
from bikechance_ml.jobs import climate
from tests import eval_fixture as fixture
from tests.test_profile_climatology import cell_row, to_daily, to_profile

DAY0, DAY1, DAY2 = fixture.DAYS
PORTS = ("hellocycling/a",)


def put(root: Path, path: str, table: pa.Table) -> None:
    """Storage と同じ形で手元に置く。"""
    whole = root / path
    whole.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, whole)


def put_day(
    root: Path, day: date, *, n_days: int = 2, with_profile: bool = True, with_daily: bool = True
) -> None:
    """その日の版を置く。**`n_days` を日ごとに変える**と、どちらを読んだかが中身で分かる。"""
    if with_profile:
        put(root, profile_path(day, profile.PROFILE_NAME), to_profile([cell_row(n_days=n_days)]))
    if with_daily:
        put(root, profile_path(day, profile.DAILY_NAME), to_daily([cell_row(n=3, n_bike_ok=3)]))


def test_the_profile_read_is_the_last_day(tmp_path: Path) -> None:
    """**学習の最終日の版**を読む。1 日目の版を読むと、以降の日ぶんが入らない。

    **札ではなく中身で確かめる。** `FromProfile.day` は呼ぶ側が付けた札なので、
    読む日だけを取り違えても札は正しいままになる——**壊して初めて分かった**
    （W5 プラン §12 の 147）。
    """
    put_day(tmp_path, DAY0, n_days=2)
    put_day(tmp_path, DAY1, n_days=5)
    source = climate.load(None, (DAY0, DAY1), tmp_path, PORTS)
    assert source is not None
    assert source.day == DAY1
    assert source.profile.column("n_days")[0].as_py() == 5, "最終日の版の中身であること"


def test_every_training_day_can_be_subtracted(tmp_path: Path) -> None:
    """学習の各日ぶんの `daily` を読む。**引ける日が混合に使える行を決める。**"""
    put_day(tmp_path, DAY0)
    put_day(tmp_path, DAY1)
    source = climate.load(None, (DAY0, DAY1), tmp_path, PORTS)
    assert source is not None
    assert sorted(source.dailies.by_day) == [DAY0.toordinal(), DAY1.toordinal()]


def test_a_day_without_a_daily_is_simply_absent(tmp_path: Path) -> None:
    """**無い日を埋めない**（収集の欠損は補間しない。CLAUDE.md §6）。"""
    put_day(tmp_path, DAY0, with_daily=False)
    put_day(tmp_path, DAY1)
    source = climate.load(None, (DAY0, DAY1), tmp_path, PORTS)
    assert source is not None
    assert sorted(source.dailies.by_day) == [DAY1.toordinal()]


def test_no_profile_means_the_old_way(tmp_path: Path) -> None:
    """**初日と、収集を始めたばかりの日のため**（完了条件 7）。"""
    assert climate.load(None, (DAY0,), tmp_path, PORTS) is None


def test_no_days_means_the_old_way(tmp_path: Path) -> None:
    """1 日も読めなかったとき。**`days[-1]` を引きに行かない。**"""
    assert climate.load(None, (), tmp_path, PORTS) is None


# ── 作り方を決める 1 か所（§12 の 166）────────────────────────
def test_source_for_reads_the_profiles_when_asked(tmp_path: Path) -> None:
    """**`from_profiles` が真ならプロファイルから作る。**

    以前はこの決め方が `evaluate_baselines` の中にだけ在り、`harness.run` の既定値が
    もう 1 つの答えを持っていた。**`fit_lightgbm` は既定のまま**だったので、
    同じ日の同じ `v3` で**違う B2** が出ていた（W5 プラン §12 の 166）。
    """
    put_day(tmp_path, DAY0, n_days=2)
    made = climate.source_for(None, (DAY0,), tmp_path, PORTS, from_profiles=True)
    assert isinstance(made, profile_climatology.FromProfile)
    assert "プロファイル" in made.describe()


def test_source_for_uses_the_samples_when_told_not_to(tmp_path: Path) -> None:
    """**逃げ道。** 2026-09-19 より前の記録と比べるときに要る。

    プロファイルが**在っても**読まない（「無いから落ちた」と区別できること）。
    """
    put_day(tmp_path, DAY0, n_days=2)
    made = climate.source_for(None, (DAY0,), tmp_path, PORTS, from_profiles=False)
    assert isinstance(made, climatology.FromSamples)


def test_source_for_falls_back_when_there_is_no_profile(tmp_path: Path) -> None:
    """**プロファイルが無ければ落ちる**（完了条件 7）。落ちたことは `describe()` に出る。"""
    made = climate.source_for(None, (DAY0,), tmp_path, PORTS, from_profiles=True)
    assert isinstance(made, climatology.FromSamples)
    assert "学習サンプル" in made.describe()


def test_only_one_place_decides_how_b2_is_made() -> None:
    """**`FromSamples()` を作るのは `jobs/climate.py` だけ。**

    同じ条件が 3 か所（`fit_baseline`・`evaluate_baselines`・`harness.run` の既定値）に
    書かれていて、**そのうち 1 つが違う答えを出していた**のが §12 の 166 である。
    同じ挙動の重複は検査で区別できないので、**「決める場所が 1 つであること」そのもの**を
    留める。落ちたら、`climate.source_for` を呼ぶ形に直す。
    """
    root = Path(__file__).resolve().parents[1] / "bikechance_ml"
    guilty = sorted(
        one.relative_to(root).as_posix()
        for one in root.rglob("*.py")
        if "FromSamples()" in one.read_text(encoding="utf-8")
    )
    assert guilty == ["jobs/climate.py"], f"B2 の作り方を決めている場所が他にあります: {guilty}"


def test_a_file_with_the_wrong_columns_is_refused(tmp_path: Path) -> None:
    """**足りない列を捏造しない**（W3 プラン §12 の 97）。"""
    put(tmp_path, profile_path(DAY0, profile.PROFILE_NAME), to_daily([cell_row()]))
    with pytest.raises(profile.SchemaMismatchError):
        climate.load(None, (DAY0,), tmp_path, PORTS)


def test_the_samples_and_the_profile_are_read_the_same_way(tmp_path: Path) -> None:
    """**学習サンプルも同じ関数で読む**（`evaluate_baselines._one_day`）。"""
    put(tmp_path, features_path(DAY0), fixture.to_table([]))
    assert climate.read_bytes(None, features_path(DAY0), tmp_path) is not None
    assert climate.read_bytes(None, features_path(DAY1), tmp_path) is None


def test_without_storage_and_without_local_it_stops() -> None:
    """**どちらも無いまま黙って空を返さない。**"""
    with pytest.raises(ValueError, match="--local"):
        climate.read_bytes(None, features_path(DAY0), None)
