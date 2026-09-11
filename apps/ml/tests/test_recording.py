"""`job_runs` への記録（`jobs/recording.py`）。

**主題は「記録の都合でジョブを落とさない」と「飲んだ例外から文言を出さない」。**

後者は公開リポジトリで効く：学習サンプルの日次生成は GitHub Actions で走り、
**実行ログは誰でも読める**（W4 プラン §6.8 の PR J）。`io/supabase.py` が投げる
`SupabaseError` の文言は伏せ字を通っているが、**ここは他の例外も通る場所**なので、
種類だけに切り詰めてあることを機械で固定する。
"""

from collections.abc import Mapping
from typing import Final

import pytest

from bikechance_ml.jobs import recording

#: 鍵の見立て。**繋いで作る。** 本物の形をそのままソースに書くと、
#: CI の「機密情報の混入チェック」が**この検査そのもの**を秘密の混入として検出する。
FAKE_KEY: Final[str] = "sb" + "_secret_" + "0123456789abcdef"

#: 接続先と鍵が混じった文言の見立て。**これがログに出てはいけない。**
LEAKY: Final[str] = f"https://example.supabase.co/rest/v1/rpc/job_started?apikey={FAKE_KEY}"


class FakePort:
    def __init__(self, *, fail_started: bool = False, fail_finished: bool = False) -> None:
        self.fail_started = fail_started
        self.fail_finished = fail_finished
        self.started: list[str] = []
        self.finished: list[tuple[int, str, Mapping[str, object]]] = []

    def job_started(self, job_name: str) -> int:
        if self.fail_started:
            raise RuntimeError(LEAKY)
        self.started.append(job_name)
        return 7

    def job_finished(self, run_id: int, status: str, detail: Mapping[str, object]) -> None:
        if self.fail_finished:
            raise RuntimeError(LEAKY)
        self.finished.append((run_id, status, detail))


# ── 通常の経路 ────────────────────────────────────────────────
def test_the_run_id_comes_back() -> None:
    port = FakePort()
    assert recording.started_quietly(port, "build_features") == 7
    assert port.started == ["build_features"]


def test_the_end_is_recorded_with_the_detail() -> None:
    port = FakePort()
    recording.record_quietly(port, 7, "ok", {"date": "2026-09-10"})
    assert port.finished == [(7, "ok", {"date": "2026-09-10"})]


# ── 記録が落ちても仕事は落とさない ────────────────────────────
def test_a_failing_start_gives_none_instead_of_raising() -> None:
    assert recording.started_quietly(FakePort(fail_started=True), "build_features") is None


def test_a_failing_end_is_swallowed() -> None:
    recording.record_quietly(FakePort(fail_finished=True), 7, "ok", {})


def test_without_a_run_id_nothing_is_written() -> None:
    """**始まりを記録できなければ終わりも書かない**（宙ぶらりんの run_id を作らない）。"""
    port = FakePort()
    recording.record_quietly(port, None, "ok", {"date": "2026-09-10"})
    assert port.finished == []


# ── 飲んだ例外から文言を出さない ──────────────────────────────
def test_the_start_log_carries_only_the_exception_type(capsys: pytest.CaptureFixture[str]) -> None:
    """**GitHub Actions のログは公開リポジトリでは誰でも読める。**"""
    recording.started_quietly(FakePort(fail_started=True), "build_features")
    printed = capsys.readouterr().err
    assert "RuntimeError" in printed
    assert LEAKY not in printed
    assert "supabase.co" not in printed
    assert FAKE_KEY not in printed


def test_the_end_log_carries_only_the_exception_type(capsys: pytest.CaptureFixture[str]) -> None:
    recording.record_quietly(FakePort(fail_finished=True), 7, "failed", {})
    printed = capsys.readouterr().err
    assert "RuntimeError" in printed
    assert LEAKY not in printed
    assert FAKE_KEY not in printed


def test_the_three_jobs_share_this_one_implementation() -> None:
    """**同じ 2 つの関数が 3 か所に散っていた。** 戻さないために、使う側を数え上げる。

    片方だけが例外の文言まで出しており、**どれが正しい形かがコードから読めなかった**。
    """
    from bikechance_ml.jobs import build_features, build_reference, compact

    for module in (build_features, build_reference, compact):
        assert module.recording is recording, f"{module.__name__} が別の実装を持っている"
