"""Supabase への読み書き（CLAUDE.md §3「副作用は `io/` に分離」）。

PostgREST と Storage の REST を httpx で直に叩く。**psycopg を入れない**理由は
`pyproject.toml` に書いたとおりで、1 時間ぶんは 60 行に満たず、経路を 2 本にしない。

秘密の扱い（CLAUDE.md §5）：
  * キーは**ヘッダにだけ**載せる。URL とクエリには決して入れない
  * 例外の文言は必ず `redact()` を通す。httpx の例外は要求 URL を抱えている
  * 失敗は `SupabaseError` に詰め替えて上げる。素の例外のままだと文脈が消える
"""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import httpx

from bikechance_ml.config import Config
from bikechance_ml.jobs.snapshot_table import Snapshot, StationRow
from bikechance_ml.json_shape import (
    ShapeError,
    as_dict,
    as_int,
    as_int_list,
    as_list,
    as_str,
    field,
)
from bikechance_ml.redact import redact

#: Parquet を置くバケット。0017 で作る。値の出どころはマイグレーションと
#: `packages/shared/src/constants.ts`（`PARQUET_BUCKET`）と一致させる。
PARQUET_BUCKET: Final[str] = "gbfs-parquet"

#: Parquet の Content-Type。IANA 登録済みの型で、バケットの `allowed_mime_types` と揃える。
PARQUET_CONTENT_TYPE: Final[str] = "application/vnd.apache.parquet"

#: 台帳の 1 ページ。14,900 件なら 4 往復（5,000 が 3 回と、空のページ 1 回）で済む。
STATION_PAGE_SIZE: Final[int] = 5_000

#: スナップショットの 1 ページ。1 行が 5,800〜14,900 要素の配列 4 本なので小さく刻む。
SNAPSHOT_PAGE_SIZE: Final[int] = 24

#: ページ送りの上限。`offset` が効かない相手に当たっても無限に回らないための歯止め。
#: 台帳 14,900 件でも 4 往復で終わるので、100 は十分に余裕がある。
MAX_PAGES: Final[int] = 100

#: 応答本文をエラーに載せる上限。全部載せると 1 行が数 MB になり得る。
MAX_ERROR_CHARS: Final[int] = 200

#: 1 要求のタイムアウト（秒）。maxDuration 120 秒の内側に収める。
REQUEST_TIMEOUT_S: Final[float] = 30.0
CONNECT_TIMEOUT_S: Final[float] = 10.0


@dataclass(frozen=True)
class SupabaseFailure:
    """失敗の文脈。**URL と秘密は入らない。**"""

    phase: str
    status: int | None
    error_name: str
    message: str

    def describe(self) -> str:
        return f"{self.phase}/{self.error_name}: {self.message}"


class SupabaseError(RuntimeError):
    def __init__(self, failure: SupabaseFailure) -> None:
        self.failure = failure
        super().__init__(failure.describe())


def _iso_z(at: datetime) -> str:
    """PostgREST に渡す時刻。**必ず UTC に直してから**、`+` を含まない形にする。

    `Z` を付けるだけだと、JST の値を渡されたときに 9 時間ずれた区間を読んでしまう。
    符号化の揺れ（`+` が空白になる）も避けられる。
    """
    return f"{at.astimezone(UTC):%Y-%m-%dT%H:%M:%S}Z"


class SupabaseIo:
    """PostgREST と Storage の入り口。呼ぶ側は `httpx` を知らなくてよい。"""

    def __init__(self, config: Config, client: httpx.Client) -> None:
        self._config = config
        self._client = client
        self._secrets = (config.supabase_secret_key, config.cron_secret, config.supabase_url)

    # ── 低レベル ────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        key = self._config.supabase_secret_key
        return {"apikey": key, "Authorization": f"Bearer {key}"}

    def _mask(self, text: str) -> str:
        return redact(text, self._secrets)[:MAX_ERROR_CHARS]

    def _request(
        self,
        method: str,
        path: str,
        phase: str,
        *,
        params: Mapping[str, str] | None = None,
        json: object = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        merged = {**self._headers(), **(headers or {})}
        try:
            response = self._client.request(
                method,
                f"{self._config.supabase_url}{path}",
                params=params,
                json=json,
                content=content,
                headers=merged,
            )
        except httpx.HTTPError as cause:
            # 例外の文言は要求 URL を含む。再送出せず、伏せ字にしてから詰め替える
            raise SupabaseError(
                SupabaseFailure(phase, None, type(cause).__name__, self._mask(str(cause)))
            ) from None
        if response.is_success:
            return response
        raise SupabaseError(
            SupabaseFailure(phase, response.status_code, "HttpStatus", self._mask(response.text))
        )

    def _rows(self, path: str, params: Mapping[str, str], where: str) -> list[object]:
        response = self._request("GET", path, "rest", params=params)
        try:
            return as_list(response.json(), where)
        except (ShapeError, ValueError) as cause:
            raise SupabaseError(
                SupabaseFailure("parse", None, type(cause).__name__, self._mask(str(cause)))
            ) from None

    def _paged(self, path: str, params: Mapping[str, str], page: int, where: str) -> list[object]:
        """`limit` / `offset` で全ページを読む。**空のページが来るまで続ける。**

        「ページ長未満なら終わり」にしない。PostgREST には `max-rows` という
        サーバ側の上限があり、これが要求した `limit` より小さいと**短いページが返り、
        そこで打ち切ると台帳が黙って欠ける**。1 往復増えるだけなので、0 件を見るまで回す。

        並び順は一意な列（`idx` / `observed_at`）で固定してあるので、`offset` で
        ずらしても行が飛んだり重複したりしない。
        """
        rows: list[object] = []
        for _ in range(MAX_PAGES):
            page_params = {**params, "limit": str(page), "offset": str(len(rows))}
            received = self._rows(path, page_params, where)
            if not received:
                return rows
            rows.extend(received)
        raise SupabaseError(
            SupabaseFailure("rest", None, "TooManyPages", f"{where}: ページが {MAX_PAGES} を超えた")
        )

    # ── 読み出し ────────────────────────────────────────────────
    def list_active_systems(self) -> tuple[str, ...]:
        """収集中のシステム。**Python 側に一覧を二重化しない**ため DB から読む。"""
        rows = self._rows(
            "/rest/v1/systems",
            {"select": "system_id", "is_active": "is.true", "order": "system_id.asc"},
            "systems",
        )
        return tuple(
            as_str(field(as_dict(row, "systems"), "system_id", "systems"), "system_id")
            for row in rows
        )

    def list_stations(self, system_id: str) -> tuple[StationRow, ...]:
        rows = self._paged(
            "/rest/v1/stations",
            {"select": "station_id,idx", "system_id": f"eq.{system_id}", "order": "idx.asc"},
            STATION_PAGE_SIZE,
            "stations",
        )
        return tuple(_to_station_row(row) for row in rows)

    def list_snapshots(
        self, system_id: str, start: datetime, end: datetime
    ) -> tuple[Snapshot, ...]:
        """半開区間 `[start, end)` のスナップショット。`observed_at` の昇順。"""
        rows = self._paged(
            "/rest/v1/status_snapshots",
            {
                "select": "observed_at,bikes,docks,flags,reported_age_s",
                "system_id": f"eq.{system_id}",
                # 同じ列に 2 つの条件を掛けるので `and` にまとめる。
                # `observed_at` を 2 回書くとクエリで衝突する
                "and": f"(observed_at.gte.{_iso_z(start)},observed_at.lt.{_iso_z(end)})",
                "order": "observed_at.asc",
            },
            SNAPSHOT_PAGE_SIZE,
            "status_snapshots",
        )
        return tuple(_to_snapshot(row) for row in rows)

    # ── 書き込み ────────────────────────────────────────────────
    def upload_parquet(self, path: str, body: bytes) -> None:
        """同じパスに上書きする。同じ時間帯を 2 回処理しても結果が変わらない。"""
        self._request(
            "POST",
            f"/storage/v1/object/{PARQUET_BUCKET}/{path}",
            "storage",
            content=body,
            headers={"Content-Type": PARQUET_CONTENT_TYPE, "x-upsert": "true"},
        )

    def job_started(self, job_name: str) -> int:
        response = self._request(
            "POST", "/rest/v1/rpc/job_started", "rest", json={"p_job_name": job_name}
        )
        return as_int(response.json(), "job_started")

    def job_finished(self, run_id: int, status: str, detail: Mapping[str, object]) -> None:
        self._request(
            "POST",
            "/rest/v1/rpc/job_finished",
            "rest",
            json={"p_id": run_id, "p_status": status, "p_detail": dict(detail)},
        )


def _to_station_row(row: object) -> StationRow:
    fields = as_dict(row, "stations")
    return StationRow(
        station_id=as_str(field(fields, "station_id", "stations"), "station_id"),
        idx=as_int(field(fields, "idx", "stations"), "idx"),
    )


def _to_snapshot(row: object) -> Snapshot:
    fields = as_dict(row, "status_snapshots")
    return Snapshot(
        observed_at=_to_datetime(as_str(field(fields, "observed_at", "snapshot"), "observed_at")),
        bikes=_ints(fields, "bikes"),
        docks=_ints(fields, "docks"),
        flags=_ints(fields, "flags"),
        reported_age_s=_ints(fields, "reported_age_s"),
    )


def _ints(fields: Mapping[str, object], name: str) -> Sequence[int]:
    return as_int_list(field(fields, name, "snapshot"), name)


def _to_datetime(text: str) -> datetime:
    """PostgREST の timestamptz。`+00:00` でも `Z` でも読めるようにする。"""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


@contextmanager
def open_supabase(config: Config) -> Iterator[SupabaseIo]:
    """本番で使う組み立て。接続を確実に閉じる。"""
    timeout = httpx.Timeout(REQUEST_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
    with httpx.Client(timeout=timeout) as client:
        yield SupabaseIo(config, client)
