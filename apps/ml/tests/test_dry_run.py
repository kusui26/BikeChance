"""本番の口での試し打ち（`jobs/dry_run.py`・`io/infer_client.py`。W8 の PR G）。

**この 1 ファイルの主題は「本番の周期に重ねないこと」と「合格を甘くしないこと」。**

  * 試し打ちは**周期の合間の窓**（HELLO 2 分 20 秒・ドコモ 3 分 00 秒から 15 秒）でだけ始める。
    窓を過ぎていたら次の周期まで待つ（前の回が長引いても、本番の 1 分・4 分に食い込まない）
  * **1 周期に系統ごと 1 回**、2 周期くり返す
  * 合格は HTTP 200・`status = dry_run`・頼んだ版・予測が 1 つ以上・`rss_mb` が 1.6 GB 未満
  * **URL も秘密も例外に出さない**
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Final

import httpx
import pytest

from bikechance_ml.io.infer_client import InferCallError, InferClient
from bikechance_ml.jobs import dry_run

VERSION: Final[str] = "composite-v1-20261101"
SECRET: Final[str] = "cron-secret-for-tests-0123456789"
ORIGIN: Final[str] = "https://example.invalid"


class FakeClock:
    """進むのは `sleep` だけ。**叩いた時刻**を記録に残す。"""

    def __init__(self, start: datetime) -> None:
        self.at = start
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.at += timedelta(seconds=seconds)


class FakeInfer:
    """叩くたびに 20 秒進める（本番の 1 回ぶん）。**返す要約は渡したもの。**"""

    def __init__(self, clock: FakeClock, body: Mapping[str, object], status: int = 200) -> None:
        self.clock = clock
        self.body = body
        self.status = status
        self.calls: list[tuple[str, str, datetime]] = []

    def call(self, system_id: str, model_version: str) -> tuple[int, Mapping[str, object]]:
        self.calls.append((system_id, model_version, self.clock.now()))
        self.clock.at += timedelta(seconds=20)
        return self.status, {**self.body, "model_version": model_version}


def _ok_body(**changed: object) -> dict[str, object]:
    body: dict[str, object] = {
        "status": "dry_run",
        "model_kind": "composite",
        "predicted": 15_331,
        "unknown_ports": 1,
        "duration_ms": 16_208,
        "cpu_ms": 11_632,
        "model_load_ms": 0,
        "rss_mb": 1_052,
        "profile_date": "2026-10-31",
        "sample": [{"station_id": "x"}],
    }
    return {**body, **changed}


def _at(minute: int, second: int) -> datetime:
    return datetime(2026, 11, 2, 1, minute, second, tzinfo=UTC)


# ── 窓 ────────────────────────────────────────────────────────
def test_the_slots_sit_between_the_production_cycles() -> None:
    """**本番はドコモが 60 秒・HELLO が 240 秒から**（`vercel.json` の `1-59/5`・`4-59/5`）。"""
    for offset in dry_run.SLOT_OFFSETS_S.values():
        assert offset >= 60 + 30
        assert offset + dry_run.SLOT_WINDOW_S + 30 <= 240


@pytest.mark.parametrize(
    ("now", "offset", "expected"),
    [
        (_at(30, 0), 140, 140.0),  # 周期の頭 → 2 分 20 秒まで待つ
        (_at(32, 20), 140, 0.0),  # 窓の始まり
        (_at(32, 34), 140, 0.0),  # 窓の終わりの手前
        (_at(32, 35), 140, 285.0),  # 窓を過ぎた → 次の周期
        (_at(33, 0), 180, 0.0),
        (_at(33, 15), 180, 285.0),
        (_at(34, 59), 180, 181.0),
    ],
)
def test_seconds_until_the_next_slot(now: datetime, offset: int, expected: float) -> None:
    assert dry_run.seconds_until(offset, now) == pytest.approx(expected)


def test_the_plan_calls_each_system_once_per_cycle() -> None:
    assert dry_run.plan(["docomo-cycle", "hellocycling"]) == [
        (1, "hellocycling"),
        (1, "docomo-cycle"),
        (2, "hellocycling"),
        (2, "docomo-cycle"),
    ]


def test_every_call_starts_inside_its_window() -> None:
    clock = FakeClock(_at(29, 50))
    infer = FakeInfer(clock, _ok_body())
    results = dry_run.run(infer, clock, VERSION, ["hellocycling", "docomo-cycle"])
    assert len(results) == 4
    for system_id, _, started in infer.calls:
        offset = dry_run.SLOT_OFFSETS_S[system_id]
        assert offset <= dry_run.position_s(started) < offset + dry_run.SLOT_WINDOW_S
    hello = [started for system_id, _, started in infer.calls if system_id == "hellocycling"]
    assert hello[1] - hello[0] == timedelta(minutes=5), "続けて叩かず、次の周期に入れる"


# ── 合否 ──────────────────────────────────────────────────────
def _results(body: Mapping[str, object], status: int = 200) -> tuple[dry_run.DryRun, ...]:
    clock = FakeClock(_at(32, 20))
    return dry_run.run(FakeInfer(clock, body, status), clock, VERSION, ["hellocycling"])


def test_a_clean_run_passes_and_keeps_only_the_summary() -> None:
    results = _results(_ok_body())
    assert dry_run.problems_of(results, VERSION) == []
    assert "sample" not in results[0].summary, "予測の値は持たない"
    assert dry_run.peak_rss_mb(results) == 1_052


@pytest.mark.parametrize(
    ("body", "status", "words"),
    [
        (_ok_body(), 500, "HTTP 500"),
        (_ok_body(status="ok"), 200, "status ok"),
        (_ok_body(predicted=0), 200, "予測 0"),
        (_ok_body(rss_mb=1_600), 200, "rss_mb 1600"),
        (_ok_body(rss_mb=None), 200, "rss_mb None"),
    ],
)
def test_failures_are_named(body: Mapping[str, object], status: int, words: str) -> None:
    problems = dry_run.problems_of(_results(body, status), VERSION)
    assert problems and all(words in one for one in problems)


def test_a_different_version_in_the_answer_fails() -> None:
    """**頼んだ版で答えたか**（別の版の要約を合格にしない）。"""
    clock = FakeClock(_at(32, 20))
    results = dry_run.run(FakeInfer(clock, _ok_body()), clock, VERSION, ["hellocycling"])
    assert dry_run.problems_of(results, "composite-v1-other") != []


# ── HTTP の口 ─────────────────────────────────────────────────
def _client(handler: httpx.MockTransport) -> InferClient:
    return InferClient(ORIGIN, SECRET, httpx.Client(transport=handler))


def test_the_client_sends_the_secret_only_in_the_header() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_ok_body(model_version=VERSION))

    status, body = _client(httpx.MockTransport(answer)).call("hellocycling", VERSION)
    assert (status, body["status"]) == (200, "dry_run")
    (request,) = seen
    assert request.url.path == "/ml/infer/hellocycling"
    assert request.url.params["model"] == VERSION
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    assert SECRET not in str(request.url)


def test_transport_errors_carry_neither_url_nor_secret() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed: {request.url}")

    with pytest.raises(InferCallError) as caught:
        _client(httpx.MockTransport(broken)).call("docomo-cycle", VERSION)
    text = str(caught.value)
    assert ORIGIN not in text and SECRET not in text
    assert (caught.value.system_id, caught.value.error_name) == ("docomo-cycle", "ConnectError")


def test_a_non_json_answer_is_an_error() -> None:
    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    with pytest.raises(InferCallError) as caught:
        _client(httpx.MockTransport(html)).call("hellocycling", VERSION)
    assert caught.value.http_status == 502
