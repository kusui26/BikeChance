"""Supabase への読み書き（`bikechance_ml/io/supabase.py`）。

`httpx.MockTransport` で本物の Supabase 無しに要求と応答を検査する。守りたいのは 3 つ。
  * **キーは URL に載らない**（ヘッダにだけ載る。CLAUDE.md §5）
  * **例外の文言に秘密と URL が残らない**（`redact()` を必ず通す）
  * ページ送りと半開区間の指定が正しい（1 ページ目だけ読んで終わる事故を防ぐ）
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest

from bikechance_ml.config import Config
from bikechance_ml.io.supabase import (
    PARQUET_BUCKET,
    PARQUET_CONTENT_TYPE,
    STATION_PAGE_SIZE,
    SupabaseError,
    SupabaseIo,
    is_missing_object,
)
from bikechance_ml.json_shape import ShapeError

URL = "https://project-ref.supabase.co"
KEY = "sb-secret-key-that-must-never-leak"
CONFIG = Config(supabase_url=URL, supabase_secret_key=KEY, cron_secret="cron-secret-value")
START = datetime(2026, 9, 8, 4, 0, tzinfo=UTC)
END = datetime(2026, 9, 8, 5, 0, tzinfo=UTC)


Handler = Callable[[httpx.Request], httpx.Response]


def io_with(handler: Handler) -> tuple[SupabaseIo, list[httpx.Request]]:
    """記録付きのクライアントを組み立てる。送った要求は `seen` に貯まる。"""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(record))
    return SupabaseIo(CONFIG, client), seen


def json_response(payload: object) -> httpx.Response:
    return httpx.Response(200, json=payload)


def server_with(rows: list[object], max_rows: int | None = None) -> Handler:
    """`limit` / `offset` を本物と同じように解釈する応答役。

    `max_rows` は PostgREST のサーバ側上限を模す。要求した `limit` より小さいページが
    返る状況を作り、**そこで打ち切らない**ことを確かめるために使う。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        limit = int(request.url.params["limit"])
        if max_rows is not None:
            limit = min(limit, max_rows)
        return json_response(rows[offset : offset + limit])

    return handler


# ── 秘密の扱い ────────────────────────────────────────────────
def test_key_travels_in_headers_only() -> None:
    io, seen = io_with(lambda request: json_response([{"system_id": "hellocycling"}]))
    io.list_active_systems()
    request = seen[0]
    assert request.headers["apikey"] == KEY
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in str(request.url)


def test_http_error_hides_the_key_and_keeps_the_status() -> None:
    body = f"permission denied (key={KEY})"
    io, _ = io_with(lambda request: httpx.Response(403, text=body))
    with pytest.raises(SupabaseError) as caught:
        io.list_active_systems()
    failure = caught.value.failure
    assert failure.status == 403
    assert failure.phase == "rest"
    assert KEY not in failure.message
    assert "***" in failure.message


def test_transport_error_hides_the_host() -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("failed to connect", request=request)

    io, _ = io_with(explode)
    with pytest.raises(SupabaseError) as caught:
        io.list_active_systems()
    assert URL not in caught.value.failure.message
    assert caught.value.failure.error_name == "ConnectError"


def test_error_message_is_truncated() -> None:
    io, _ = io_with(lambda request: httpx.Response(500, text="x" * 5_000))
    with pytest.raises(SupabaseError) as caught:
        io.list_active_systems()
    assert len(caught.value.failure.message) <= 200


# ── 読み出し ──────────────────────────────────────────────────
def test_active_systems_filters_on_is_active() -> None:
    io, seen = io_with(lambda request: json_response([{"system_id": "docomo-cycle"}]))
    assert io.list_active_systems() == ("docomo-cycle",)
    assert seen[0].url.params["is_active"] == "is.true"


def ledger(count: int) -> list[object]:
    return [{"station_id": f"s{i}", "idx": i} for i in range(count)]


def test_stations_read_every_page() -> None:
    """1 ページ目が満杯なら次を読む。**ここを間違えると台帳が黙って欠ける。**"""
    io, seen = io_with(server_with(ledger(STATION_PAGE_SIZE + 1)))
    stations = io.list_stations("hellocycling")
    assert len(stations) == STATION_PAGE_SIZE + 1
    assert stations[-1].station_id == f"s{STATION_PAGE_SIZE}"
    # 満杯 → 1 件 → 空、の 3 往復。空を見るまで止まらない
    assert len(seen) == 3


def test_stations_survive_a_server_side_row_cap() -> None:
    """PostgREST の `max-rows` が要求より小さくても、最後まで読む。

    「ページ長未満なら終わり」と書くと、ここで**黙って 10 件しか読まない**。
    """
    io, seen = io_with(server_with(ledger(25), max_rows=10))
    assert len(io.list_stations("hellocycling")) == 25
    assert len(seen) == 4


def test_stations_of_an_empty_ledger() -> None:
    io, seen = io_with(server_with([]))
    assert io.list_stations("hellocycling") == ()
    assert len(seen) == 1


def test_snapshots_use_a_half_open_range() -> None:
    io, seen = io_with(lambda request: json_response([]))
    io.list_snapshots("hellocycling", START, END)
    condition = seen[0].url.params["and"]
    assert condition == "(observed_at.gte.2026-09-08T04:00:00Z,observed_at.lt.2026-09-08T05:00:00Z)"


def test_snapshot_range_is_converted_to_utc() -> None:
    """JST のまま渡されても 9 時間ずれない。"""
    jst = timezone(timedelta(hours=9))
    io, seen = io_with(lambda request: json_response([]))
    io.list_snapshots(
        "hellocycling",
        datetime(2026, 9, 8, 13, 0, tzinfo=jst),
        datetime(2026, 9, 8, 14, 0, tzinfo=jst),
    )
    condition = seen[0].url.params["and"]
    assert condition == "(observed_at.gte.2026-09-08T04:00:00Z,observed_at.lt.2026-09-08T05:00:00Z)"


def test_snapshots_are_parsed_into_arrays() -> None:
    row = {
        "observed_at": "2026-09-08T04:00:00+00:00",
        "fetched_at": "2026-09-08T04:01:10+00:00",
        "bikes": [1, -1],
        "docks": [2, 3],
        "flags": [7, 7],
        "reported_age_s": [30, 0],
    }
    io, _ = io_with(server_with([row]))
    snapshots = io.list_snapshots("hellocycling", START, END)
    assert snapshots[0].observed_at == START
    assert snapshots[0].fetched_at == START + timedelta(seconds=70)
    assert list(snapshots[0].bikes) == [1, -1]


def test_snapshot_select_asks_for_fetched_at() -> None:
    """**as-of はこの列で切る。** 取り忘れると Parquet に入らず、静かに古い規約に戻る。"""
    io, seen = io_with(server_with([]))
    io.list_snapshots("hellocycling", START, END)
    assert "fetched_at" in seen[0].url.params["select"]


def test_snapshot_without_fetched_at_is_a_parse_failure() -> None:
    """列が返ってこなければ**止まる**。null で埋めて先に進まない。

    `ShapeError` は `compact_system` の外で拾われ、そのシステムだけ `ok=false` に
    なって `job_runs` に残る（`_run_all`）。API が 400 を返す経路には乗らない。
    """
    row = {
        "observed_at": "2026-09-08T04:00:00+00:00",
        "bikes": [1],
        "docks": [2],
        "flags": [7],
        "reported_age_s": [30],
    }
    io, _ = io_with(server_with([row]))
    with pytest.raises(ShapeError):
        io.list_snapshots("hellocycling", START, END)


def test_unexpected_shape_becomes_a_parse_failure() -> None:
    io, _ = io_with(lambda request: json_response({"message": "not a list"}))
    with pytest.raises(SupabaseError) as caught:
        io.list_active_systems()
    assert caught.value.failure.phase == "parse"


# ── 書き込み ──────────────────────────────────────────────────
def test_upload_overwrites_the_same_path() -> None:
    io, seen = io_with(lambda request: httpx.Response(200, json={"Key": "ok"}))
    io.upload_parquet("hellocycling/date=2026-09-08/hour=04/part.parquet", b"PAR1")
    request = seen[0]
    assert request.url.path == (
        f"/storage/v1/object/{PARQUET_BUCKET}/hellocycling/date=2026-09-08/hour=04/part.parquet"
    )
    assert request.headers["x-upsert"] == "true"
    assert request.headers["content-type"] == PARQUET_CONTENT_TYPE
    assert request.content == b"PAR1"


def test_upload_failure_is_labelled_storage() -> None:
    io, _ = io_with(lambda request: httpx.Response(413, text="too large"))
    with pytest.raises(SupabaseError) as caught:
        io.upload_parquet("a/part.parquet", b"PAR1")
    assert caught.value.failure.phase == "storage"


def test_job_started_returns_the_row_id() -> None:
    io, seen = io_with(lambda request: json_response(4321))
    assert io.job_started("compact_parquet") == 4321
    assert seen[0].url.path == "/rest/v1/rpc/job_started"


def test_job_finished_sends_the_detail() -> None:
    io, seen = io_with(lambda request: httpx.Response(204))
    io.job_finished(7, "ok", {"n_rows": 6})
    assert b'"p_status":"ok"' in seen[0].content.replace(b" ", b"")


# ── Storage の「無い」の読み方（W3 プラン §12 の 84 と同じ癖）──
def test_missing_object_is_reported_as_http_400() -> None:
    """**Supabase Storage は 404 を HTTP 400 で返す**（実測 2026-09-08）。"""
    body = (
        '{"statusCode":"404","error":"not_found","message":"Object not found","code":"NoSuchKey"}'
    )
    assert is_missing_object(400, body)
    assert is_missing_object(404, body)


def test_plain_404_is_still_missing() -> None:
    """相手が素直に 404 を返すようになっても壊れない。"""
    assert is_missing_object(404, "")


def test_other_failures_are_not_read_as_missing() -> None:
    """**400 をすべて「無い」と読むと、権限エラーが欠測に化ける。**"""
    assert not is_missing_object(403, '{"statusCode":"403","code":"AccessDenied"}')
    assert not is_missing_object(500, "boom")
    assert not is_missing_object(400, '{"statusCode":"409","code":"KeyAlreadyExists"}')
    assert not is_missing_object(400, "これは JSON ではない")
    assert not is_missing_object(400, "[]")
