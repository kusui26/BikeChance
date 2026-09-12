"""Supabase への読み書き（CLAUDE.md §3「副作用は `io/` に分離」）。

PostgREST と Storage の REST を httpx で直に叩く。**psycopg を入れない**理由は
`pyproject.toml` に書いたとおりで、1 時間ぶんは 60 行に満たず、経路を 2 本にしない。

秘密の扱い（CLAUDE.md §5）：
  * キーは**ヘッダにだけ**載せる。URL とクエリには決して入れない
  * 例外の文言は必ず `redact()` を通す。httpx の例外は要求 URL を抱えている
  * 失敗は `SupabaseError` に詰め替えて上げる。素の例外のままだと文脈が消える
"""

import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final

import httpx

from bikechance_ml.config import Config, StorageConfig
from bikechance_ml.features.reference import (
    NeighborRow,
    StationAttributeRow,
    StationGeoRow,
    StationStatusRow,
)
from bikechance_ml.features.weather import SERIES as WEATHER_SERIES
from bikechance_ml.features.weather import WeatherRow
from bikechance_ml.jobs.snapshot_table import Snapshot, StationRow
from bikechance_ml.jobs.weather_archive import SERIES as ARCHIVE_SERIES
from bikechance_ml.jobs.weather_archive import PendingIssue
from bikechance_ml.json_shape import (
    ShapeError,
    as_bool,
    as_dict,
    as_float,
    as_int,
    as_int_list,
    as_list,
    as_str,
    field,
)
from bikechance_ml.models.registry import Registered
from bikechance_ml.redact import redact

#: Parquet を置くバケット。0017 で作る。値の出どころはマイグレーションと
#: `packages/shared/src/constants.ts`（`PARQUET_BUCKET`）と一致させる。
PARQUET_BUCKET: Final[str] = "gbfs-parquet"

#: Parquet の Content-Type。IANA 登録済みの型で、バケットの `allowed_mime_types` と揃える。
PARQUET_CONTENT_TYPE: Final[str] = "application/vnd.apache.parquet"

#: 予測ログを置くバケット。0042 で作る。**`gbfs-parquet` に相乗りさせない**——あちらは
#: 無期限で、こちらは 12 か月保持である（D-24）。中身は同じ Parquet でも、
#: **寿命と作り直し方が違うものを混ぜると保持を別々に決められない**
#: （W3 プラン §12 の 106 と同じ判断）。形は `jobs/forecast_log.py`。
FORECAST_LOG_BUCKET: Final[str] = "forecast-log"

#: 台帳の 1 ページ。14,900 件なら 4 往復（5,000 が 3 回と、空のページ 1 回）で済む。
STATION_PAGE_SIZE: Final[int] = 5_000

#: 近傍の 1 ページ。1 行が小さいので大きく取る（HELLO は約 8.6 万行）。
NEIGHBOR_PAGE_SIZE: Final[int] = 10_000

#: スナップショットの 1 ページ。1 行が 5,800〜14,900 要素の配列 4 本なので小さく刻む。
SNAPSHOT_PAGE_SIZE: Final[int] = 24

#: 予報の 1 ページ。1 行は 8 要素の配列 3 本なので、台帳と同じくらいの大きさで刻める。
WEATHER_PAGE_SIZE: Final[int] = 5_000

#: ページ送りの上限。`offset` が効かない相手に当たっても無限に回らないための歯止め。
#: 台帳 14,900 件でも 4 往復で終わるので、100 は十分に余裕がある。
MAX_PAGES: Final[int] = 100

#: 応答本文をエラーに載せる上限。全部載せると 1 行が数 MB になり得る。
MAX_ERROR_CHARS: Final[int] = 200

#: 「無い」を正常系として扱うための状態コード。
HTTP_NOT_FOUND: Final[int] = 404

#: Storage が「無い」を表すときに本文へ入れる符号。**HTTP の状態コードは 400 で来る。**
STORAGE_NOT_FOUND_CODES: Final[frozenset[str]] = frozenset({"NoSuchKey", "NotFound"})

#: 1 要求のタイムアウト（秒）。maxDuration 120 秒の内側に収める。
REQUEST_TIMEOUT_S: Final[float] = 30.0
CONNECT_TIMEOUT_S: Final[float] = 10.0

#: Parquet の取得は数 MB になるので長めに取る（分析用。Cron の経路では使わない）。
DOWNLOAD_TIMEOUT_S: Final[float] = 120.0


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


def is_missing_object(http_status: int, body: str) -> bool:
    """Storage の応答が「オブジェクトが無い」を表しているか。

    **Supabase Storage は 404 を HTTP 400 で返す**（実測 2026-09-08、本文は
    `{"statusCode":"404","error":"not_found","message":"Object not found",
    "code":"NoSuchKey"}`）。重複アップロードを 400 で返すのと同じ癖で、
    W3 プラン §12 の 84 と同じ形をしている。

    **状態コードだけで判断しない。** 400 をすべて「無い」と読むと、権限エラーや
    要求の誤りまで「無い」に化けて、欠測として静かに集計から抜ける。
    """
    if http_status == HTTP_NOT_FOUND:
        return True
    if http_status not in {400, 404}:
        return False
    try:
        document = json.loads(body)
    except ValueError:
        return False
    if not isinstance(document, dict):
        return False
    return str(document.get("statusCode")) == str(HTTP_NOT_FOUND) or (
        str(document.get("code")) in STORAGE_NOT_FOUND_CODES
    )


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
                "select": "observed_at,fetched_at,bikes,docks,flags,reported_age_s",
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

    # ── 参照データ（特徴量パイプライン。W3 プラン §9.5）──────────
    def list_station_geo(self, system_id: str) -> tuple[StationGeoRow, ...]:
        """台帳と行政区画コード。**`is_active` は読まない**（生存者バイアス）。"""
        rows = self._paged(
            "/rest/v1/stations",
            {
                "select": "station_id,first_seen_at,pref_code,muni_code",
                "system_id": f"eq.{system_id}",
                "order": "station_id.asc",
            },
            STATION_PAGE_SIZE,
            "stations",
        )
        return tuple(_to_station_geo(row) for row in rows)

    def list_station_attributes(self, system_id: str) -> tuple[StationAttributeRow, ...]:
        """現行の属性。`raw` から HELLO / ドコモ固有の項目を取り出す。"""
        rows = self._paged(
            "/rest/v1/station_attributes",
            {
                "select": "station_id,lat,lon,capacity,raw",
                "system_id": f"eq.{system_id}",
                "valid_to": "is.null",
                "order": "station_id.asc",
            },
            STATION_PAGE_SIZE,
            "station_attributes",
        )
        return tuple(_to_station_attribute(row) for row in rows)

    def list_neighbors(self, system_id: str) -> tuple[NeighborRow, ...]:
        """半径 500 m の近傍（起点がこのシステムのもの）。相手は別システムでもよい。"""
        rows = self._paged(
            "/rest/v1/station_neighbors",
            {
                "select": "station_id,nb_system_id,nb_station_id,distance_m,same_system",
                "system_id": f"eq.{system_id}",
                "order": "station_id.asc,nb_station_id.asc",
            },
            NEIGHBOR_PAGE_SIZE,
            "station_neighbors",
        )
        return tuple(_to_neighbor(row) for row in rows)

    def list_holidays(self) -> tuple[date, ...]:
        """内閣府 CSV の祝日。**年末年始とお盆は入っていない**（暦の規則）。"""
        rows = self._paged(
            "/rest/v1/jp_holidays",
            {"select": "holiday_date", "order": "holiday_date.asc"},
            STATION_PAGE_SIZE,
            "jp_holidays",
        )
        return tuple(
            date.fromisoformat(
                as_str(field(as_dict(row, "jp_holidays"), "holiday_date", "jp_holidays"), "date")
            )
            for row in rows
        )

    # ── 推論（W3 プラン §5.10）────────────────────────────────
    def read_base_observed_at(self, system_id: str) -> datetime | None:
        """そのシステムの最新の観測時刻。**予測の基準**になる。"""
        rows = self._rows(
            "/rest/v1/feed_state",
            {"select": "last_observed_at", "system_id": f"eq.{system_id}"},
            "feed_state",
        )
        if not rows:
            return None
        value = as_dict(rows[0], "feed_state").get("last_observed_at")
        return None if value is None else _to_datetime(as_str(value, "last_observed_at"))

    def list_station_status(self, system_id: str) -> tuple[StationStatusRow, ...]:
        """最新状態（1 ポート 1 行）。**`-1` は「一度も観測されていない」。**"""
        rows = self._paged(
            "/rest/v1/station_status_latest",
            {
                "select": "station_id,bikes,docks,flags,is_present",
                "system_id": f"eq.{system_id}",
                "order": "station_id.asc",
            },
            STATION_PAGE_SIZE,
            "station_status_latest",
        )
        return tuple(_to_station_status(row) for row in rows)

    def begin_inference(
        self, system_id: str, base_observed_at: datetime, model_version: str
    ) -> int | None:
        """推論を掴む。**既に同じ観測時刻があれば None**（二重推論を止める）。"""
        response = self._request(
            "POST",
            "/rest/v1/rpc/begin_inference",
            "rest",
            json={
                "p_system_id": system_id,
                "p_base_observed_at": _iso_z(base_observed_at),
                "p_model_version": model_version,
            },
        )
        fields = as_dict(response.json(), "begin_inference")
        if not as_bool(field(fields, "claimed", "begin_inference"), "claimed"):
            return None
        return as_int(field(fields, "id", "begin_inference"), "id")

    def finish_inference(
        self,
        run_id: int,
        status: str,
        n_rows: int,
        duration_ms: int,
        error: str | None = None,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        self._request(
            "POST",
            "/rest/v1/rpc/finish_inference",
            "rest",
            json={
                "p_id": run_id,
                "p_status": status,
                "p_n_rows": n_rows,
                "p_duration_ms": duration_ms,
                "p_error": error,
                "p_detail": None if detail is None else dict(detail),
            },
        )

    def upsert_forecasts(self, rows: Sequence[Mapping[str, object]]) -> int:
        """予測をまとめて書く。**1 回の往復で送る量は呼ぶ側が刻む。**"""
        if not rows:
            return 0
        response = self._request(
            "POST", "/rest/v1/rpc/upsert_forecasts", "rest", json={"p_rows": list(rows)}
        )
        return as_int(response.json(), "upsert_forecasts")

    # ── モデルの登録簿（W4 プラン §6.5）──────────────────────
    def active_model(self) -> Registered | None:
        """いま配る版。**登録が無ければ None**（呼ぶ側が止める）。"""
        return self._one_model({"status": "eq.active"})

    def find_model(self, model_version: str) -> Registered | None:
        """版を名指しで引く（候補を手で試すとき）。"""
        return self._one_model({"model_version": f"eq.{model_version}"})

    def _one_model(self, where: Mapping[str, str]) -> Registered | None:
        rows = self._rows(
            "/rest/v1/model_versions",
            {
                "select": "model_version,kind,status,feature_set,artifact_path",
                **where,
                "limit": "1",
            },
            "model_versions",
        )
        return None if not rows else _to_registered(rows[0])

    def register_model_version(self, row: Mapping[str, object]) -> str:
        """候補を登録する。**`active` にはできない**（0038 の RPC が弾く）。"""
        response = self._request(
            "POST", "/rest/v1/rpc/register_model_version", "rest", json={"p_row": dict(row)}
        )
        fields = as_dict(response.json(), "register_model_version")
        return as_str(field(fields, "model_version", "register"), "model_version")

    # ── 天気（W4 プラン §6.4）────────────────────────────────
    def list_weather_pending(self, since: datetime, limit: int) -> tuple[PendingIssue, ...]:
        """まだ取り込んでいない発行を、古い順に。**下限を必ず渡す。**

        `v_weather_pending` は時刻で切っていない（保守の保持期間と二重管理にしない
        ため。0037）ので、**保持期間より短い窓を呼ぶ側が添える**。添えないと、
        30 日で消えた発行が「未処理」として蘇り、取り込みと削除を繰り返す。
        """
        rows = self._rows(
            "/rest/v1/v_weather_pending",
            {
                "select": "hour_epoch_s,issued_hour,available_at,n_cells,n_loaded",
                "available_at": f"gte.{_iso_z(since)}",
                "order": "available_at.asc",
                "limit": str(limit),
            },
            "v_weather_pending",
        )
        return tuple(_to_pending_issue(row) for row in rows)

    def list_weather(self, start: datetime, end: datetime) -> tuple[WeatherRow, ...]:
        """`available_at` が半開区間 `[start, end)` に入る予報。

        **これは読み込み量の都合であって、リークを止める規律ではない。**
        どの発行を使ってよいかを決めるのは `features/weather.py` の 1 か所である。
        """
        rows = self._paged(
            "/rest/v1/weather_hourly",
            {
                "select": "cell_lat_idx,cell_lon_idx,issued_hour,available_at,"
                + ",".join(WEATHER_SERIES),
                "and": f"(available_at.gte.{_iso_z(start)},available_at.lt.{_iso_z(end)})",
                "order": "available_at.asc,cell_lat_idx.asc,cell_lon_idx.asc",
            },
            WEATHER_PAGE_SIZE,
            "weather_hourly",
        )
        return tuple(_to_weather_row(row) for row in rows)

    def oldest_weather_issue(self) -> datetime | None:
        """`weather_hourly` にいちばん古く残っている発行。**次に消えるもの。**

        保持は 30 日なので、これが「あと何日で生アーカイブから戻すしかなくなるか」を
        表す（W4 プラン §6.8 の PR L）。1 行も無ければ `None`。
        """
        rows = self._rows(
            "/rest/v1/weather_hourly",
            {"select": "issued_hour", "order": "issued_hour.asc", "limit": "1"},
            "weather_hourly",
        )
        if not rows:
            return None
        fields = as_dict(rows[0], "weather_hourly")
        return _to_datetime(as_str(field(fields, "issued_hour", "weather"), "issued_hour"))

    def list_weather_issue(self, issued_hour: datetime) -> tuple[WeatherRow, ...]:
        """1 発行ぶんを**全系列**で読む（`weather_code` も含む）。

        `list_weather` が 3 系列に絞るのは読み込み量の都合だが、**生アーカイブと
        突き合わせるときは絞ってはいけない**——絞った先だけを比べると、比べていない
        列がずれていても「一致した」と言ってしまう（PR L）。
        """
        rows = self._paged(
            "/rest/v1/weather_hourly",
            {
                "select": "cell_lat_idx,cell_lon_idx,issued_hour,available_at,"
                + ",".join(ARCHIVE_SERIES),
                "issued_hour": f"eq.{_iso_z(issued_hour)}",
                "order": "cell_lat_idx.asc,cell_lon_idx.asc",
            },
            WEATHER_PAGE_SIZE,
            "weather_hourly",
        )
        return tuple(_to_weather_row(row, ARCHIVE_SERIES) for row in rows)

    def upsert_weather_hourly(self, rows: Sequence[Mapping[str, object]]) -> int:
        """予報をまとめて書く。**同じ発行を入れ直しても結果は変わらない。**"""
        if not rows:
            return 0
        response = self._request(
            "POST", "/rest/v1/rpc/upsert_weather_hourly", "rest", json={"p_rows": list(rows)}
        )
        return as_int(response.json(), "upsert_weather_hourly")

    # ── 書き込み ────────────────────────────────────────────────
    def upload_parquet(self, path: str, body: bytes) -> None:
        """同じパスに上書きする。同じ時間帯を 2 回処理しても結果が変わらない。"""
        self.upload(PARQUET_BUCKET, path, body, PARQUET_CONTENT_TYPE)

    def upload_forecast_log(self, path: str, body: bytes) -> None:
        """予測ログの 1 サイクルぶんを置く（D-24）。

        **同じパスに上書きする。** パスは `(基準時刻, 版)` で決まるので、同じ観測に
        対する 2 度目は同じ中身になる。
        """
        self.upload(FORECAST_LOG_BUCKET, path, body, PARQUET_CONTENT_TYPE)

    def upload(self, bucket: str, path: str, body: bytes, content_type: str) -> None:
        """Storage の 1 オブジェクトを置く（上書き）。"""
        self._request(
            "POST",
            f"/storage/v1/object/{bucket}/{path}",
            "storage",
            content=body,
            headers={"Content-Type": content_type, "x-upsert": "true"},
        )

    def job_started(self, job_name: str) -> int:
        response = self._request(
            "POST", "/rest/v1/rpc/job_started", "rest", json={"p_job_name": job_name}
        )
        return as_int(response.json(), "job_started")

    def download(self, bucket: str, path: str) -> bytes | None:
        """Storage の 1 オブジェクトを取る。**無ければ None**（例外にしない）。

        分析（`analysis/`）が Parquet を読むために使う。畳んでいない時間帯は
        単に存在しないので、「無い」は正常系として扱えたほうがよい。
        """
        try:
            response = self._client.request(
                "GET",
                f"{self._config.supabase_url}/storage/v1/object/{bucket}/{path}",
                headers=self._headers(),
            )
        except httpx.HTTPError as cause:
            raise SupabaseError(
                SupabaseFailure("storage", None, type(cause).__name__, self._mask(str(cause)))
            ) from None
        if response.is_success:
            return response.content
        if is_missing_object(response.status_code, response.text):
            return None
        raise SupabaseError(
            SupabaseFailure(
                "storage", response.status_code, "HttpStatus", self._mask(response.text)
            )
        )

    def job_finished(self, run_id: int, status: str, detail: Mapping[str, object]) -> None:
        self._request(
            "POST",
            "/rest/v1/rpc/job_finished",
            "rest",
            json={"p_id": run_id, "p_status": status, "p_detail": dict(detail)},
        )


def _to_registered(row: object) -> Registered:
    fields = as_dict(row, "model_versions")
    return Registered(
        model_version=as_str(field(fields, "model_version", "model"), "model_version"),
        kind=as_str(field(fields, "kind", "model"), "kind"),
        feature_set=as_str(field(fields, "feature_set", "model"), "feature_set"),
        artifact_path=as_str(field(fields, "artifact_path", "model"), "artifact_path"),
        status=as_str(field(fields, "status", "model"), "status"),
    )


def _to_pending_issue(row: object) -> PendingIssue:
    fields = as_dict(row, "v_weather_pending")
    return PendingIssue(
        hour_epoch_s=as_int(field(fields, "hour_epoch_s", "pending"), "hour_epoch_s"),
        issued_hour=_to_datetime(as_str(field(fields, "issued_hour", "pending"), "issued_hour")),
        available_at=_to_datetime(as_str(field(fields, "available_at", "pending"), "available_at")),
        n_cells=as_int(field(fields, "n_cells", "pending"), "n_cells"),
        n_loaded=as_int(field(fields, "n_loaded", "pending"), "n_loaded"),
    )


def _to_weather_row(row: object, names: Iterable[str] = WEATHER_SERIES) -> WeatherRow:
    """`weather_hourly` の 1 行。**どの系列を読むかは呼ぶ側が決める。**"""
    fields = as_dict(row, "weather_hourly")
    return WeatherRow(
        cell_lat_idx=as_int(field(fields, "cell_lat_idx", "weather"), "cell_lat_idx"),
        cell_lon_idx=as_int(field(fields, "cell_lon_idx", "weather"), "cell_lon_idx"),
        issued_hour=_to_datetime(as_str(field(fields, "issued_hour", "weather"), "issued_hour")),
        available_at=_to_datetime(as_str(field(fields, "available_at", "weather"), "available_at")),
        values={name: _nullable_floats(fields, name) for name in names},
    )


def _nullable_floats(fields: Mapping[str, object], name: str) -> list[float | None]:
    """`real[]` の 1 列。**要素の null は null のまま**（0 で埋めない）。"""
    items = as_list(field(fields, name, "weather"), name)
    return [None if one is None else as_float(one, f"weather.{name}") for one in items]


def _to_station_status(row: object) -> StationStatusRow:
    fields = as_dict(row, "station_status_latest")
    return StationStatusRow(
        station_id=as_str(field(fields, "station_id", "status"), "station_id"),
        bikes=as_int(field(fields, "bikes", "status"), "bikes"),
        docks=as_int(field(fields, "docks", "status"), "docks"),
        flags=as_int(field(fields, "flags", "status"), "flags"),
        is_present=as_bool(field(fields, "is_present", "status"), "is_present"),
    )


def _to_station_geo(row: object) -> StationGeoRow:
    fields = as_dict(row, "stations")
    return StationGeoRow(
        station_id=as_str(field(fields, "station_id", "stations"), "station_id"),
        first_seen_at=_to_datetime(as_str(field(fields, "first_seen_at", "stations"), "first")),
        pref_code=_optional_int(fields, "pref_code", "stations"),
        muni_code=_optional_int(fields, "muni_code", "stations"),
    )


def _to_station_attribute(row: object) -> StationAttributeRow:
    fields = as_dict(row, "station_attributes")
    raw = as_dict(field(fields, "raw", "station_attributes"), "raw")
    return StationAttributeRow(
        station_id=as_str(field(fields, "station_id", "station_attributes"), "station_id"),
        lat=_optional_float(fields, "lat", "station_attributes"),
        lon=_optional_float(fields, "lon", "station_attributes"),
        capacity=_optional_int(fields, "capacity", "station_attributes"),
        is_charging_station=_optional_bool(raw, "is_charging_station", "raw"),
        region_id=_optional_numeric_id(raw, "region_id", "raw"),
    )


def _to_neighbor(row: object) -> NeighborRow:
    fields = as_dict(row, "station_neighbors")
    return NeighborRow(
        station_id=as_str(field(fields, "station_id", "station_neighbors"), "station_id"),
        nb_system_id=as_str(field(fields, "nb_system_id", "station_neighbors"), "nb_system"),
        nb_station_id=as_str(field(fields, "nb_station_id", "station_neighbors"), "nb"),
        distance_m=as_int(field(fields, "distance_m", "station_neighbors"), "distance_m"),
        same_system=as_bool(field(fields, "same_system", "station_neighbors"), "same_system"),
    )


def _optional_int(fields: Mapping[str, object], name: str, where: str) -> int | None:
    """**無い**と**null**を同じに扱う。`raw` はシステムごとにキーの集合が違う。"""
    value = fields.get(name)
    return None if value is None else as_int(value, f"{where}.{name}")


def _optional_numeric_id(fields: Mapping[str, object], name: str, where: str) -> int | None:
    """GBFS の ID は**文字列**である（実測：ドコモの `region_id` は "1"〜"18"）。

    W3-07a は「整数のカテゴリとして使う」と決めたので、ここで数に直す。
    **数字でない ID が来たら止める。** 静かに NULL にすると、地域の情報が
    丸ごと消えたことに気づけない（`region_id` はドコモの地理特徴量の主役）。
    """
    value = fields.get(name)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ShapeError(f"{where}.{name}: 真偽値は ID ではない")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise ShapeError(f"{where}.{name}: 数字の ID を期待した")


def _optional_float(fields: Mapping[str, object], name: str, where: str) -> float | None:
    value = fields.get(name)
    return None if value is None else as_float(value, f"{where}.{name}")


def _optional_bool(fields: Mapping[str, object], name: str, where: str) -> bool | None:
    value = fields.get(name)
    return None if value is None else as_bool(value, f"{where}.{name}")


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
        fetched_at=_to_datetime(as_str(field(fields, "fetched_at", "snapshot"), "fetched_at")),
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


@contextmanager
def open_storage(
    config: StorageConfig, timeout_s: float = DOWNLOAD_TIMEOUT_S
) -> Iterator[SupabaseIo]:
    """読み取りだけの組み立て。`CRON_SECRET` を要求しない（分析用）。

    `cron_secret` は伏せ字の対象に空文字を渡す。**空は `redact()` が無視する**ので、
    「短すぎる値で全部を伏せ字にする」事故は起きない。
    """
    full = Config(
        supabase_url=config.supabase_url,
        supabase_secret_key=config.supabase_secret_key,
        cron_secret="",
    )
    with httpx.Client(timeout=httpx.Timeout(timeout_s, connect=CONNECT_TIMEOUT_S)) as client:
        yield SupabaseIo(full, client)
