"""本番の口での試し打ち（`jobs/dry_run.py`・`io/infer_client.py`。W8 の PR G）。

**この 1 ファイルの主題は「本番の周期に重ねないこと」と「合格を甘くしないこと」。**

  * 試し打ちは**周期の合間の窓**（HELLO 3 分 40 秒・ドコモ 4 分 15 秒から 15 秒）でだけ始める。
    窓を過ぎていたら次の周期まで待つ（前の回が長引いても、本番の 0 分・2 分に食い込まない）
  * **Parquet 化・天気の取り込み・参照の周期は飛ばす**（`vercel.json` の ML の仕事と突き合わせる）
  * **1 周期に系統ごと 1 回**、2 周期くり返す
  * 合格は HTTP 200・`status = dry_run`・頼んだ版・予測が 1 つ以上・`rss_mb` が 1.6 GB 未満
  * **URL も秘密も例外に出さない**
"""

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Final

import httpx
import pytest

from bikechance_ml.io.infer_client import InferCallError, InferClient
from bikechance_ml.jobs import dry_run

VERSION: Final[str] = "composite-v1-20261101"
SECRET: Final[str] = "cron-secret-for-tests-0123456789"
ORIGIN: Final[str] = "https://example.invalid"

VERCEL_JSON: Final[Path] = Path(__file__).resolve().parents[3] / "vercel.json"

#: 本番のドコモの推論の最長（秒）。直近 7 日の最長 35.7 秒（2026-11-14 に測った）。
DOCOMO_RUN_MAX_S: Final[int] = 36

#: ドコモの試し打ちの最長とみる秒（11/9 の実測 15〜18 秒に余裕を見て）。
DOCOMO_DRY_RUN_MAX_S: Final[int] = 30

#: 試し打ちが飛ばす ML のサービスの仕事（`vercel.json` の道）。推論のほかはこれだけ。
BUSY_PATHS: Final[tuple[str, ...]] = ("/ml/compact", "/ml/weather", "/ml/reference")


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


def _at(minute: int, second: int, hour: int = 1) -> datetime:
    return datetime(2026, 11, 2, hour, minute, second, tzinfo=UTC)


def _vercel_schedules() -> dict[str, str]:
    """`vercel.json` の Cron（道 → スケジュール）。"""
    parsed: object = json.loads(VERCEL_JSON.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    crons = parsed.get("crons")
    assert isinstance(crons, list)
    return {str(one["path"]): str(one["schedule"]) for one in crons if isinstance(one, dict)}


def _first_minute(schedule: str) -> int:
    """分のフィールドの最初の分（`2-59/5` → 2、`9` → 9）。"""
    return int(schedule.split()[0].split("/")[0].split("-")[0])


# ── 窓 ────────────────────────────────────────────────────────
def test_the_slots_sit_after_docomo_and_end_within_the_cycle() -> None:
    """**本番は HELLO が 0 分・ドコモが 2 分に起動する**（`vercel.json`。D-48・W11-01）。

    ドコモの本番は、2 分の中のどの秒に呼ばれても 3 分 36 秒までに終わる（分の中の起動の秒は
    スケジュールを変えると変わる。W11 プランの所見 246）。試し打ちはその後に始め、ドコモの
    試し打ちが周期の終わり（＝次の周期の HELLO の起動の分）までに終わる。
    """
    schedules = _vercel_schedules()
    assert _first_minute(schedules["/ml/infer/hellocycling"]) == 0
    docomo_minute = _first_minute(schedules["/ml/infer/docomo-cycle"])
    docomo_done_s = (docomo_minute + 1) * 60 + DOCOMO_RUN_MAX_S
    assert min(dry_run.SLOT_OFFSETS_S.values()) >= docomo_done_s
    last_end_s = dry_run.SLOT_OFFSETS_S["docomo-cycle"] + dry_run.SLOT_WINDOW_S
    assert last_end_s + DOCOMO_DRY_RUN_MAX_S <= dry_run.CYCLE_S


def test_the_skipped_jobs_are_the_ml_jobs_in_vercel_json() -> None:
    """**飛ばす周期は `vercel.json` の ML のサービスの仕事と同じ**（片方だけ変えると落ちる）。"""
    schedules = _vercel_schedules()
    ml_paths = {path for path in schedules if path.startswith("/ml/")}
    infer_paths = {path for path in ml_paths if path.startswith("/ml/infer/")}
    assert ml_paths - infer_paths == set(BUSY_PATHS), "ML の Cron を足したら、ここも決める"
    hourly = {_first_minute(schedules[path]) for path in ("/ml/compact", "/ml/weather")}
    assert hourly == dry_run.HOURLY_BUSY_MINUTES
    minute, hour = schedules["/ml/reference"].split()[:2]
    assert {(int(hour), int(minute))} == dry_run.DAILY_BUSY_AT


@pytest.mark.parametrize(
    ("now", "offset", "expected"),
    [
        (_at(30, 0), 220, 220.0),  # 周期の頭 → 3 分 40 秒まで待つ
        (_at(33, 40), 220, 0.0),  # 窓の始まり
        (_at(33, 54), 220, 0.0),  # 窓の終わりの手前
        (_at(33, 55), 220, 285.0),  # 窓を過ぎた → 次の周期
        (_at(34, 15), 255, 0.0),
        (_at(34, 30), 255, 285.0),
        (_at(34, 59), 255, 256.0),
    ],
)
def test_seconds_until_the_next_slot(now: datetime, offset: int, expected: float) -> None:
    assert dry_run.seconds_until(offset, now) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("now", "offset", "expected", "why"),
    [
        (_at(5, 0), 220, 520.0, "9 分の Parquet 化の周期を飛ばす"),
        (_at(4, 0), 220, 580.0, "窓を過ぎた次の周期が Parquet 化 → もう 1 つ先"),
        (_at(8, 45), 220, 295.0, "窓の中でも Parquet 化の周期なら叩かない"),
        (_at(25, 0), 255, 555.0, "28 分の天気の取り込みの周期を飛ばす"),
        (_at(0, 0, hour=20), 220, 820.0, "20:03 UTC（05:03 JST）の参照と次の 9 分の周期を飛ばす"),
        (_at(0, 0, hour=1), 220, 220.0, "参照は 1 日 1 回。ほかの時の 3 分は飛ばさない"),
    ],
)
def test_the_slots_skip_cycles_with_other_ml_jobs(
    now: datetime, offset: int, expected: float, why: str
) -> None:
    assert dry_run.seconds_until(offset, now) == pytest.approx(expected), why


def test_from_any_moment_the_slot_is_in_its_window_and_a_free_cycle() -> None:
    """**どの時刻から待ち始めても**、叩くのは窓の中で、ほかの ML の仕事が無い周期。

    1 日を 7 秒刻みで確かめる。
    """
    start = datetime(2026, 11, 2, 0, 0, 0, tzinfo=UTC)
    for step in range(0, 24 * 3_600, 7):
        now = start + timedelta(seconds=step)
        for offset in dry_run.SLOT_OFFSETS_S.values():
            at = now + timedelta(seconds=dry_run.seconds_until(offset, now))
            assert offset <= dry_run.position_s(at) < offset + dry_run.SLOT_WINDOW_S, now
            assert not dry_run.is_busy_cycle(at), now


def test_busy_cycles_follow_utc_even_for_jst_times() -> None:
    """**JST の時刻で渡しても UTC で判定する**（参照は 05:03 JST ＝ 20:03 UTC）。"""
    jst = timezone(timedelta(hours=9))
    assert dry_run.is_busy_cycle(datetime(2026, 11, 3, 5, 3, 30, tzinfo=jst))
    assert not dry_run.is_busy_cycle(datetime(2026, 11, 3, 20, 3, 30, tzinfo=jst))


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


def test_a_run_started_before_the_parquet_cycle_waits_past_it() -> None:
    """01:04:50 に始めると、01:05〜01:10（9 分の Parquet 化）の周期を飛ばして 01:13:40 から叩く。"""
    clock = FakeClock(_at(4, 50))
    infer = FakeInfer(clock, _ok_body())
    dry_run.run(infer, clock, VERSION, ["hellocycling", "docomo-cycle"])
    starts = [started for _, _, started in infer.calls]
    assert starts[0] == _at(13, 40)
    assert not any(dry_run.is_busy_cycle(started) for started in starts)


# ── 合否 ──────────────────────────────────────────────────────
def _results(body: Mapping[str, object], status: int = 200) -> tuple[dry_run.DryRun, ...]:
    clock = FakeClock(_at(33, 40))
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
    clock = FakeClock(_at(33, 40))
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
