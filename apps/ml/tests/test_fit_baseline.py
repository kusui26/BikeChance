"""当てはめる期間の決め方（`jobs/fit_baseline.py` の `window`）。

**主題は「期間を書かずに走らせても、狙った日が読まれること」**（W5 の PR M、W5-16）。
当てはめ直しを日課にするなら `--from` と `--to` を毎回書いていられないが、**既定が
静かにずれると、古い日で作った成果物を配ることになる**——それは名前（`-YYYYMMDD`）
にも出ないので、気づく手立てが無い。だから既定の境目をここで固定する。

**UTC と JST の境目を 2 点で挟む。** `now` は UTC で渡ってくるが、暦日は JST で切る。
`now.date()` と書いてしまうと **JST の朝 9 時より前に「一昨日まで」になる**——日次の
当てはめ直しは 08:30 JST 以降に走らせる想定なので、まさにその時間帯で外れる。

**もう 1 つの主題は上書きの防止**（W6 の PR B、W6-19、契約 38）。版の名前は最終学習日
なので、回し直すと同じ名前になる。**配信中の版と同じ名前なら、当てはめる前に止まり、
置く直前にもう 1 度確かめる。**
"""

from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from bikechance_ml.baselines.artifact import FORMAT_VERSION, artifact_path, from_bytes
from bikechance_ml.features.constants import FEATURE_SET
from bikechance_ml.jobs import fit_baseline
from bikechance_ml.jobs.fit_baseline import (
    CONTENT_TYPE,
    DEFAULT_TRAIN_DAYS,
    WindowError,
    model_version_for,
    run,
    window,
)
from bikechance_ml.models import registry
from tests import eval_fixture
from tests.test_window import write_samples

#: JST の 2026-09-14 08:30（＝当てはめ直しを走らせる時刻）。**UTC ではまだ 09-13。**
MORNING = datetime(2026, 9, 13, 23, 30, tzinfo=UTC)


def test_the_default_window_ends_yesterday_in_jst() -> None:
    """**既定は「昨日まで」**。当日の `features/` はまだ無い（翌朝に作られる）。"""
    first, last = window(start=None, end=None, days=None, now=MORNING)
    assert last == date(2026, 9, 13)
    assert first == date(2026, 9, 7)


def test_the_default_window_is_seven_days_inclusive() -> None:
    """**両端を含めて 7 日。** 9/7〜9/13 は 7 日（6 ではない）。

    **数え方を `DEFAULT_TRAIN_DAYS` から作らない。** 定数から期待値を作ると、定数を
    変えたときに試験も一緒に動いて何も守らなくなる（W5 §12 の 154 と同じ落とし穴）。
    """
    assert DEFAULT_TRAIN_DAYS == 7
    first, last = window(start=None, end=None, days=None, now=MORNING)
    assert (last - first).days + 1 == 7


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # JST 09-13 23:59 — **UTC ではもう 09-13 14:59**。UTC で切ると「09-12 まで」になる
        (datetime(2026, 9, 13, 14, 59, tzinfo=UTC), date(2026, 9, 12)),
        # JST 09-14 00:00 — 日が変わった最初の 1 分
        (datetime(2026, 9, 13, 15, 0, tzinfo=UTC), date(2026, 9, 13)),
    ],
)
def test_yesterday_is_measured_in_jst(now: datetime, expected: date) -> None:
    """**暦日は JST で切る。** 9 時間ぶんの取り違えは、まるまる 1 日ぶんの差になる。"""
    _, last = window(start=None, end=None, days=None, now=now)
    assert last == expected


def test_days_counts_back_from_the_end() -> None:
    """`--days` は **`--to` から遡る**。1 を渡したら 1 日（0 日でも 2 日でもない）。"""
    assert window(start=None, end="2026-09-12", days=1, now=MORNING) == (
        date(2026, 9, 12),
        date(2026, 9, 12),
    )
    assert window(start=None, end="2026-09-12", days=3, now=MORNING) == (
        date(2026, 9, 10),
        date(2026, 9, 12),
    )


def test_days_without_an_end_counts_back_from_yesterday() -> None:
    """`--days` だけを渡したら、**昨日から遡る**（`--to` の既定と噛み合う）。"""
    assert window(start=None, end=None, days=2, now=MORNING) == (
        date(2026, 9, 12),
        date(2026, 9, 13),
    )


def test_explicit_dates_are_used_as_given() -> None:
    """両端を書いたら、そのまま。**既定は一切混ざらない。**"""
    assert window(start="2026-09-07", end="2026-09-12", days=None, now=MORNING) == (
        date(2026, 9, 7),
        date(2026, 9, 12),
    )


def test_from_and_days_cannot_be_combined() -> None:
    """**どちらも「始まり」を決める。** 片方を黙って無視したら、読む日が変わる。"""
    with pytest.raises(WindowError):
        window(start="2026-09-07", end=None, days=3, now=MORNING)


def test_a_backwards_window_is_refused() -> None:
    """始まりが終わりより後なら止める（`read_window` は空の並びを黙って受ける）。"""
    with pytest.raises(WindowError):
        window(start="2026-09-13", end="2026-09-12", days=None, now=MORNING)


def test_a_single_day_window_is_allowed() -> None:
    """同じ日を両端に置くのは正しい指定（1 日で当てはめる）。"""
    assert window(start="2026-09-12", end="2026-09-12", days=None, now=MORNING) == (
        date(2026, 9, 12),
        date(2026, 9, 12),
    )


def test_zero_or_negative_days_is_refused() -> None:
    """**0 日の窓は作らない。** `--days 0` は「始まりが終わりの翌日」になって止まる。"""
    for bad in (0, -3):
        with pytest.raises(WindowError):
            window(start=None, end="2026-09-12", days=bad, now=MORNING)


def test_the_command_refuses_before_it_opens_storage() -> None:
    """**期間を見るのは Storage を開く前**（`run` の並び順）。

    環境変数が無くても 2 で返ることが、その並びの証拠になる——`open_storage` に
    届いていたら `read_storage_config` が別の失敗を出す。
    """
    assert run(["--from", "2026-09-07", "--days", "3"]) == 2


# ── 上書きの防止（W6-19、契約 38）──────────────────────────────
DAYS = eval_fixture.DAYS


def _row(model_version: str, status: str) -> registry.Registered:
    return registry.Registered(
        model_version=model_version,
        kind=registry.BASELINE_KIND,
        feature_set=FEATURE_SET,
        artifact_path=artifact_path(model_version),
        status=status,
    )


@dataclass
class Shelf:
    """登録簿と `models` バケットの代役。**引いた名前と、置いたものを覚える。**

    `promoted_from` 回目に引かれたときから、その名前を active と答える——**当てはめの
    数分のあいだに昇格された**を作る。
    """

    rows: dict[str, registry.Registered] = field(default_factory=dict)
    lookups: list[str] = field(default_factory=list)
    uploads: list[tuple[str, str, bytes, str]] = field(default_factory=list)
    promoted_from: int | None = None

    def find_model(self, model_version: str) -> registry.Registered | None:
        self.lookups.append(model_version)
        if self.promoted_from is not None and len(self.lookups) >= self.promoted_from:
            return _row(model_version, "active")
        return self.rows.get(model_version)

    def upload(self, bucket: str, path: str, body: bytes, content_type: str) -> None:
        self.uploads.append((bucket, path, body, content_type))


def _run(shelf: Shelf, root: Path, monkeypatch: pytest.MonkeyPatch, *extra: str) -> int:
    """**入り口から**走らせる（`--local` の学習サンプル、Storage の代わりに `shelf`）。"""
    monkeypatch.setattr(fit_baseline, "read_storage_config", lambda: None)
    monkeypatch.setattr(fit_baseline, "open_storage", lambda config: nullcontext(shelf))
    return run(["--from", f"{DAYS[0]}", "--to", f"{DAYS[-1]}", "--local", str(root), *extra])


def _must_not_fit(*args: object) -> None:
    raise AssertionError("当てはめに入った（止まるのは当てはめの前のはず）")


@pytest.mark.parametrize("status", ["active", "shadow"])
def test_a_serving_name_stops_before_fitting(
    status: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**7 分かけてから捨てない。** 読めた日で名前が決まった直後に止まり、何も置かない。"""
    root = write_samples(tmp_path, dict.fromkeys(DAYS, True))
    name = model_version_for(DAYS)
    shelf = Shelf(rows={name: _row(name, status)})
    monkeypatch.setattr(fit_baseline, "build_artifact", _must_not_fit)
    with pytest.raises(registry.ServingVersionError, match=status):
        _run(shelf, root, monkeypatch, "--upload")
    assert shelf.lookups == [name]
    assert shelf.uploads == []


def test_the_name_comes_from_the_days_that_were_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**最終日が読めなければ、1 日前の名前になる**——それが active なら止まる。

    頼んだ期間（〜09-09）で名前を作って引くと、読めた期間（〜09-08）の名前で
    **配信中の成果物を上書きする**。朝の日次ジョブが遅れた日に起きる（W6 プラン §8.1）。
    """
    root = write_samples(tmp_path, dict.fromkeys(DAYS[:-1], True))
    served = model_version_for(DAYS[:-1])
    shelf = Shelf(rows={served: _row(served, "active")})
    monkeypatch.setattr(fit_baseline, "build_artifact", _must_not_fit)
    with pytest.raises(registry.ServingVersionError):
        _run(shelf, root, monkeypatch, "--upload")
    assert shelf.lookups == [served]
    assert shelf.uploads == []


def test_a_candidate_name_is_put_in_format_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**candidate は上書きする。** 置いたものは版 2 で、登録簿は 2 度引く（前と直前）。"""
    root = write_samples(tmp_path, dict.fromkeys(DAYS, True))
    name = model_version_for(DAYS)
    shelf = Shelf(rows={name: _row(name, "candidate")})
    assert _run(shelf, root, monkeypatch, "--upload") == 0
    assert shelf.lookups == [name, name]
    [(bucket, path, body, content_type)] = shelf.uploads
    assert (bucket, path, content_type) == (
        registry.MODEL_BUCKET,
        artifact_path(name),
        CONTENT_TYPE,
    )
    put = from_bytes(body)
    assert (put.format_version, put.model_version) == (FORMAT_VERSION, name)


def test_a_name_promoted_during_the_fit_is_not_put(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**当てはめの間に昇格されたら、置く直前に止まる**（前に 1 度見ただけでは足りない）。"""
    root = write_samples(tmp_path, dict.fromkeys(DAYS, True))
    shelf = Shelf(promoted_from=2)
    with pytest.raises(registry.ServingVersionError):
        _run(shelf, root, monkeypatch, "--upload")
    assert len(shelf.lookups) == 2
    assert shelf.uploads == []


def test_writing_to_a_file_does_not_ask_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**`--out` だけなら登録簿を引かない**（確かめるための道を、配信の状態に縛らない）。"""
    root = write_samples(tmp_path / "samples", dict.fromkeys(DAYS, True))
    name = model_version_for(DAYS)
    shelf = Shelf(rows={name: _row(name, "active")})
    out = tmp_path / "artifact.json.gz"
    assert _run(shelf, root, monkeypatch, "--out", str(out)) == 0
    assert shelf.lookups == []
    assert shelf.uploads == []
    assert from_bytes(out.read_bytes()).model_version == name
