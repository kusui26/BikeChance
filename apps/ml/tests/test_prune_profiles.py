"""古い `profile.parquet` の掃除（`jobs/build_profiles.py` の `--prune`。D-28 の ③、W8 の PR D）。

**この 1 ファイルの主題は「消してよいものだけを、少しずつ消すこと」。** Storage の削除は
取り返せない（CLAUDE.md §6）。守るのは 7 つ。

  * **35 日を越えた日の `profile.parquet` だけ**（境目の日を含めて固定する）
  * **`daily.parquet` には触らない**（`profile` は `daily` から作り直せる派生物。源は消さない）
  * **形の違う名前・在り得ない日付は拾わない**
  * **1 回に消すのは上限まで、古い順**（日付を誤っても、1 回で失うのはそれだけ）
  * **成功も失敗も `prune_profiles` に記録する**（作る回の `build_profiles` と混ぜない）
  * **並べるだけの回（`--dry-run`）は、消さず記録もしない**
  * **消し終えた日で枠を使い切らない**（W8 の PR H）：日付の階層は `daily.parquet` のために
    残るので、2 回目からは消し終えた日も一覧に出る。10/28 の定時の回はそれで何も消さなかった
"""

from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from datetime import date, timedelta
from typing import Final

import pytest

from bikechance_ml.features.grid import profile_path
from bikechance_ml.io.supabase import PARQUET_BUCKET
from bikechance_ml.jobs import build_profiles as job

DAY: Final[date] = date(2026, 10, 26)
#: `DAY` を基準に消してよい最後の日（35 日前）。
CUTOFF: Final[date] = date(2026, 9, 21)


def _folder(day: date) -> str:
    return f"date={day:%Y-%m-%d}"


class FakeStore:
    """`PrunesProfiles` を満たす試験用の入出力。**Storage と同じく、一覧は指定した 1 階層だけを
    返し（`profiles/` なら日付の階層、`profiles/date=…/` ならその中のファイル）、削除は消えた
    ものだけを返す**（2026-10-27・28 に本番の Storage で確かめた形）。

    `profiles` を渡すと、`profile.parquet` はその日だけに置く（`daily.parquet` は全部の日）。
    """

    def __init__(
        self,
        days: Sequence[date],
        *,
        extra: Sequence[str] = (),
        profiles: Sequence[date] | None = None,
    ) -> None:
        kept = days if profiles is None else profiles
        self.objects: set[str] = {profile_path(one, "daily") for one in days} | {
            profile_path(one, "profile") for one in kept
        }
        self.extra_folders = tuple(extra)
        self.listed: list[tuple[str, str, int]] = []
        self.removals: list[tuple[str, tuple[str, ...]]] = []
        self.started: list[str] = []
        self.finished: list[tuple[int, str, Mapping[str, object]]] = []
        self.fail_remove = False

    def list_objects(self, bucket: str, prefix: str, limit: int) -> tuple[str, ...]:
        self.listed.append((bucket, prefix, limit))
        under = [path.removeprefix(prefix) for path in self.objects if path.startswith(prefix)]
        names = {one.split("/")[0] for one in under}
        extra = set(self.extra_folders) if prefix == job.PROFILES_PREFIX else set()
        return tuple(sorted(names | extra))[:limit]

    def remove_objects(self, bucket: str, paths: Sequence[str]) -> tuple[str, ...]:
        if self.fail_remove:
            raise RuntimeError("storage down")
        self.removals.append((bucket, tuple(paths)))
        gone = tuple(path for path in paths if path in self.objects)
        self.objects -= set(gone)
        return gone

    def job_started(self, job_name: str) -> int:
        self.started.append(job_name)
        return 7

    def job_finished(self, run_id: int, status: str, detail: Mapping[str, object]) -> None:
        self.finished.append((run_id, status, detail))


def _days(first: date, last: date) -> tuple[date, ...]:
    return tuple(first + timedelta(days=step) for step in range((last - first).days + 1))


# ── 消してよい日 ─────────────────────────────────────────────────
def test_the_cutoff_keeps_exactly_35_days() -> None:
    """**基準の日を含めて 35 日ぶんを残す**（10-26 なら 09-22〜10-26。09-21 から前を消す）。"""
    assert job.prune_cutoff(DAY) == CUTOFF
    kept = _days(CUTOFF + timedelta(days=1), DAY)
    assert len(kept) == job.PROFILE_KEEP_DAYS


def test_only_days_on_or_before_the_cutoff_are_due() -> None:
    folders = [
        _folder(one)
        for one in (CUTOFF - timedelta(days=1), CUTOFF, CUTOFF + timedelta(days=1), DAY)
    ]
    assert job.days_to_prune(folders, DAY) == (CUTOFF - timedelta(days=1), CUTOFF)


@pytest.mark.parametrize(
    "name",
    [
        "date=2026-02-30",
        "date=2026-9-07",
        "date=2026-09-07/profile.parquet",
        "README",
        "",
        "xdate=2026-09-07",
    ],
)
def test_strange_names_are_never_picked(name: str) -> None:
    """**形の違う名前・在り得ない日付は拾わない**（消す名前は日付から組み立てる）。"""
    assert job.folder_day(name) is None
    assert job.days_to_prune([name], DAY) == ()


def test_due_days_are_sorted_and_unique() -> None:
    folders = [_folder(date(2026, 9, 9)), _folder(date(2026, 9, 7)), _folder(date(2026, 9, 9))]
    assert job.days_to_prune(folders, DAY) == (date(2026, 9, 7), date(2026, 9, 9))


def test_the_prefix_matches_the_path_layout() -> None:
    """**一覧の頭と日付の階層の形は、置き場所の 1 か所（`profile_path`）とそろう。**"""
    path = profile_path(DAY, "profile")
    assert path.startswith(job.PROFILES_PREFIX)
    assert job.folder_day(path.removeprefix(job.PROFILES_PREFIX).split("/")[0]) == DAY


# ── 消す ──────────────────────────────────────────────────────
def test_only_profile_files_are_removed_never_dailies() -> None:
    store = FakeStore(_days(date(2026, 9, 20), DAY))
    pruned = job.prune_profiles(store, DAY)
    assert pruned.removed == (
        profile_path(date(2026, 9, 20), "profile"),
        profile_path(date(2026, 9, 21), "profile"),
    )
    assert all(path.endswith("/profile.parquet") for _, paths in store.removals for path in paths)
    assert profile_path(date(2026, 9, 20), "daily") in store.objects, "daily は残る"
    assert profile_path(date(2026, 9, 22), "profile") in store.objects, "35 日の内は残る"
    assert store.listed[0] == (PARQUET_BUCKET, job.PROFILES_PREFIX, job.PROFILES_LIST_LIMIT)


def test_removal_is_capped_and_starts_from_the_oldest() -> None:
    """**1 回に消すのは上限まで、古い順**。残りは `pending` に数え、翌日以降の回が消す。"""
    store = FakeStore(_days(date(2026, 9, 7), DAY))
    pruned = job.prune_profiles(store, DAY)
    assert pruned.targets == tuple(
        profile_path(date(2026, 9, 7) + timedelta(days=step), "profile")
        for step in range(job.PRUNE_MAX_PER_RUN)
    )
    summary = pruned.summary()
    assert summary["pending"] == len(pruned.found) - job.PRUNE_MAX_PER_RUN == 12
    assert summary["cutoff"] == CUTOFF.isoformat()


def test_a_dry_run_lists_without_removing() -> None:
    store = FakeStore(_days(date(2026, 9, 7), DAY))
    pruned = job.prune_profiles(store, DAY, dry_run=True)
    assert len(pruned.targets) == job.PRUNE_MAX_PER_RUN
    assert pruned.removed == ()
    assert store.removals == []


def test_nothing_due_means_no_removal_call() -> None:
    """**消すものが無ければ削除を呼ばない**（空の削除は Storage が 400 で断る）。"""
    store = FakeStore(_days(CUTOFF + timedelta(days=1), DAY))
    pruned = job.prune_profiles(store, DAY)
    assert pruned.found == ()
    assert pruned.checked == 0, "消してよい日が無ければ、1 つも確かめない"
    assert store.removals == []


def test_a_file_already_gone_is_not_counted_as_removed() -> None:
    """**消えた名前だけを返す**（無かったものは数えない）。確かめたあとに消えた形でも同じ。"""
    store = FakeStore(_days(date(2026, 9, 20), DAY))
    original = store.remove_objects

    def vanish_first(bucket: str, paths: Sequence[str]) -> tuple[str, ...]:
        store.objects.discard(paths[0])
        return original(bucket, paths)

    store.remove_objects = vanish_first  # type: ignore[method-assign]
    pruned = job.prune_profiles(store, DAY)
    assert pruned.targets == (
        profile_path(date(2026, 9, 20), "profile"),
        profile_path(date(2026, 9, 21), "profile"),
    )
    assert pruned.removed == (profile_path(date(2026, 9, 21), "profile"),)


# ── 消し終えた日（W8 の PR H）─────────────────────────────────────
def test_the_day_after_a_prune_removes_the_next_day() -> None:
    """**10/28 の不具合の再現**：09-07〜09-21 の `profile.parquet` を消した翌日（基準 10-27、
    消してよいのは 09-22 まで）。階層は `daily.parquet` のために 09-07 から残っている。
    **枠を消し終えた日に使わず、09-22 を消す。**"""
    day = date(2026, 10, 27)
    store = FakeStore(_days(date(2026, 9, 7), day), profiles=_days(date(2026, 9, 22), day))
    pruned = job.prune_profiles(store, day)
    assert pruned.removed == (profile_path(date(2026, 9, 22), "profile"),)
    assert pruned.summary()["pending"] == 0
    assert profile_path(date(2026, 9, 7), "daily") in store.objects, "daily は残る"


def test_a_quiet_day_checks_only_a_few_folders() -> None:
    """**ふだんの回は「消す 1 日と無い 3 日」の確かめで終わる**（日付の階層は増え続けても）。"""
    day = date(2026, 10, 27)
    store = FakeStore(_days(date(2026, 9, 7), day), profiles=_days(date(2026, 9, 22), day))
    pruned = job.prune_profiles(store, day)
    assert pruned.checked == 1 + job.PRUNE_MISSING_STREAK
    assert len(store.listed) == 1 + pruned.checked, "階層の一覧 1 回と、確かめた日の数"


def test_a_short_gap_does_not_hide_older_profiles() -> None:
    """**作れなかった日（穴）が 2 日あっても、その先を見落とさない**（打ち切りは 3 日続いたら）。"""
    present = (date(2026, 9, 15), *_days(date(2026, 9, 18), DAY))
    store = FakeStore(_days(date(2026, 9, 10), DAY), profiles=present)
    pruned = job.prune_profiles(store, DAY, limit=10)
    assert pruned.found == (
        date(2026, 9, 15),
        date(2026, 9, 18),
        date(2026, 9, 19),
        date(2026, 9, 20),
        date(2026, 9, 21),
    )
    assert pruned.targets[0] == profile_path(date(2026, 9, 15), "profile"), "古い順"


def test_three_missing_days_end_the_search() -> None:
    """**無い日が 3 日続けば、その先は確かめない**（消すのは古い順なので、その先は消し終えて
    いるはず）。4 日の穴の先に取り残しがあっても見ない——それは手で消す形（`--limit`）。"""
    present = (date(2026, 9, 10), *_days(date(2026, 9, 15), DAY))
    store = FakeStore(_days(date(2026, 9, 7), DAY), profiles=present)
    pruned = job.prune_profiles(store, DAY, limit=20)
    assert date(2026, 9, 10) not in pruned.found
    assert pruned.found == _days(date(2026, 9, 15), CUTOFF)


# ── 記録 ──────────────────────────────────────────────────────
def test_a_prune_is_recorded_under_its_own_name() -> None:
    store = FakeStore(_days(date(2026, 9, 20), DAY))
    job.prune_and_record(store, DAY, job.PRUNE_MAX_PER_RUN)
    assert store.started == [job.PRUNE_JOB_NAME]
    _, status, detail = store.finished[0]
    assert status == "ok"
    assert detail["removed"] == [
        profile_path(date(2026, 9, 20), "profile"),
        profile_path(date(2026, 9, 21), "profile"),
    ]
    assert detail["pending"] == 0


def test_a_failed_prune_is_recorded_and_raised() -> None:
    store = FakeStore(_days(date(2026, 9, 20), DAY))
    store.fail_remove = True
    with pytest.raises(RuntimeError):
        job.prune_and_record(store, DAY, job.PRUNE_MAX_PER_RUN)
    _, status, detail = store.finished[0]
    assert (status, detail["error"]) == ("failed", "RuntimeError")


# ── CLI ─────────────────────────────────────────────────────────
def _run(argv: Sequence[str], store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(job, "read_storage_config", lambda: None)
    monkeypatch.setattr(job, "open_storage", lambda config: nullcontext(store))
    return job.run(list(argv))


def test_the_cli_dry_run_prints_and_records_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = FakeStore(_days(date(2026, 9, 7), DAY))
    assert _run(["--prune", "--dry-run", "--date", f"{DAY}"], store, monkeypatch) == 0
    assert store.removals == [] and store.started == []
    printed = capsys.readouterr().out
    assert '"dry_run": true' in printed and '"pending": 12' in printed


def test_the_cli_limit_widens_one_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """**初めて消す回だけ、上限を広げられる**（確認を取った一覧をまとめて消す）。"""
    store = FakeStore(_days(date(2026, 9, 7), DAY))
    assert _run(["--prune", "--limit", "20", "--date", f"{DAY}"], store, monkeypatch) == 0
    assert len(store.removals[0][1]) == 15
    assert store.finished[0][2]["pending"] == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["--prune", "--upload"],
        ["--prune", "--skip-if-exists"],
        ["--prune", "--from-dailies"],
        ["--prune", "--out", "/tmp/x"],
        ["--dry-run"],
        ["--limit", "3"],
        ["--prune", "--limit", "0"],
    ],
)
def test_the_cli_refuses_mixed_flags(argv: list[str]) -> None:
    """**掃除は作らない**——作る道の指定とは一緒に使えない（Storage を開く前に止める）。"""
    assert job.run(argv) == 2


def test_the_cli_refuses_a_day_after_yesterday(monkeypatch: pytest.MonkeyPatch) -> None:
    """**先の日を基準にしない**（残すべき新しい版を消さない）。"""
    store = FakeStore(_days(date(2026, 9, 7), DAY))
    assert _run(["--prune", "--date", "2099-01-01"], store, monkeypatch) == 2
    assert store.removals == [] and store.listed == []
