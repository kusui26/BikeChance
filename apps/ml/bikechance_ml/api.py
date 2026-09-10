"""FastAPI アプリの組み立て（開発プラン §12.1 の `bikechance_ml/api.py`）。

**ルートは `/ml` から始める。** `vercel.json` の rewrite は `{ "service": "ml" }`
だけを指定しており、Vercel のスキーマにあるとおり「path はルート選択にのみ使われ、
ユーザーコードから見える URL を書き換えない」。つまりサービスには `/ml/health` が
そのまま届く。接頭辞はこのアプリが持つ。

状態コードの方針は web 側の `/api/jobs/*` と揃える（W1 プラン §11.6、W1-20）：
  200 成功    400 引数が不正    401 `CRON_SECRET` 不一致    500 失敗
エラーを 500 にするのは Vercel Observability のエラー率検知を効かせるため。
Cron はリダイレクトを追わないので 3xx を返さない（CLAUDE.md §3）。
"""

import os
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, date, datetime
from typing import Final

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from bikechance_ml import __version__
from bikechance_ml.auth import is_authorized
from bikechance_ml.config import MissingConfigError, read_config
from bikechance_ml.io.supabase import SupabaseIo, open_supabase
from bikechance_ml.jobs.build_reference import ReferencePort, build_and_upload, yesterday
from bikechance_ml.jobs.compact import CompactPort, compact_hour
from bikechance_ml.jobs.compact import to_detail as compact_detail
from bikechance_ml.jobs.infer import InferPort, run_inference
from bikechance_ml.jobs.infer import to_detail as infer_detail
from bikechance_ml.jobs.load_weather import WeatherPort, run_load

#: 応答の形式版。増やすときは iOS / web 側と揃える。
HEALTH_SCHEMA_VERSION: Final[str] = "1"

#: 収集（`/api/jobs/*`）と公開 API（`/v1/*`）を持つ web サービスとは別系統であることを、
#: 応答からも分かるようにしておく。障害調査でどちらを見ているか迷わないため。
SERVICE_NAME: Final[str] = "ml"

#: Cron の応答を CDN に載せない。
NO_STORE: Final[dict[str, str]] = {"Cache-Control": "no-store"}

#: 配信するモデルの版。**環境変数で切り替える**（成果物のパスがそのまま版になる）。
#: W4 で `model_versions` を作るまでは、ここが唯一の「どれを配るか」の指定である。
MODEL_VERSION_ENV: Final[str] = "BASELINE_MODEL_VERSION"

#: 推論を受け付けるシステム。**知らない名前は 400 で弾く**（DB に問い合わせない）。
KNOWN_SYSTEMS: Final[frozenset[str]] = frozenset({"hellocycling", "docomo-cycle"})


#: 入出力の差し替え点。**ルートごとに要る口が違う**ので別々に取る。テストは必要な
#: ほうだけを用意すればよく、本番はどちらにも同じ `SupabaseIo` を渡す。
CompactPortFactory = Callable[[], AbstractContextManager[CompactPort]]
InferPortFactory = Callable[[], AbstractContextManager[InferPort]]
ReferencePortFactory = Callable[[], AbstractContextManager[ReferencePort]]
WeatherPortFactory = Callable[[], AbstractContextManager[WeatherPort]]


@contextmanager
def _default_port() -> Iterator[SupabaseIo]:
    """本番の組み立て。環境変数は**ハンドラの中で**読む（起動時に読まない）。"""
    with open_supabase(read_config(os.environ)) as io:
        yield io


def _problem(status: int, title: str, detail: str) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "title": title, "detail": detail}, status_code=status, headers=NO_STORE
    )


def parse_hour(text: str | None) -> datetime | None:
    """`?hour=2026-09-08T04:00:00Z` を読む。**タイムゾーン必須**。

    素の日時を UTC と決めつけると、JST のつもりで書いた指定で 9 時間ずれた時間帯を
    黙って畳んでしまう。曖昧なものは受け取らない。
    """
    if not text:
        return None
    try:
        at = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("hour を ISO 8601 として読めません（例 2026-09-08T04:00:00Z）") from None
    if at.tzinfo is None:
        raise ValueError("hour にはタイムゾーンを付けてください（例 2026-09-08T04:00:00Z）")
    return at.astimezone(UTC)


def build_app(
    make_port: CompactPortFactory = _default_port,
    make_infer_port: InferPortFactory = _default_port,
    make_reference_port: ReferencePortFactory = _default_port,
    make_weather_port: WeatherPortFactory = _default_port,
) -> FastAPI:
    """アプリを組み立てて返す。

    生成を関数にしておくと、テストが本番と同じ手順でインスタンスを作れる。
    モジュール読み込みの副作用でアプリが出来上がる形にしない。
    """
    app = FastAPI(
        title="BikeChance ML",
        version=__version__,
        # 公開ドキュメントは出さない。ここは Cron と内部からしか叩かない
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/ml/health")
    def health() -> dict[str, str]:
        """疎通確認。**依存を持たない**ので、これが落ちたらランタイム自体の問題。"""
        return {
            "ok": "true",
            "service": SERVICE_NAME,
            "version": __version__,
            "schema_version": HEALTH_SCHEMA_VERSION,
        }

    @app.get("/ml/compact")
    def compact(request: Request, hour: str | None = None) -> JSONResponse:
        """前 1 時間（既定）のスナップショットを Parquet に畳む（W2 プラン §5.6）。

        `hour` を渡すと、その時間帯を畳み直す。再実行と取りこぼしの埋め戻し用で、
        同じパスに上書きするので何度実行しても結果は変わらない。
        """
        # 1. 認証。DB にもログにも何も書かずに弾く
        secret = os.environ.get("CRON_SECRET", "")
        if not is_authorized(request.headers.get("authorization"), secret):
            return _problem(401, "unauthorized", "CRON_SECRET が一致しません。")

        try:
            at = parse_hour(hour)
        except ValueError as cause:
            return _problem(400, "invalid_hour", str(cause))

        # 2. 設定 → 入出力の組み立て → 処理 → 記録
        try:
            with make_port() as port:
                summary = compact_hour(port, datetime.now(UTC), at)
        except MissingConfigError as cause:
            return _problem(500, "misconfigured", str(cause))
        except ValueError as cause:
            # 畳めない時間帯の指定（未来・正時でない）。呼び出し側の誤り
            return _problem(400, "invalid_hour", str(cause))
        except Exception as cause:
            # 例外の**種別だけ**を返す。文言は要求 URL を抱えていることがある
            print(f"未処理の例外: {type(cause).__name__}", file=sys.stderr)
            return _problem(500, "unhandled", type(cause).__name__)

        return JSONResponse(
            compact_detail(summary), status_code=200 if summary.ok else 500, headers=NO_STORE
        )

    @app.get("/ml/reference")
    def reference(request: Request, date_text: str | None = None) -> JSONResponse:
        """日次の参照スナップショットを書く（W3 プラン §14.3）。

        既定は**前日（JST）ぶん**。`rebuild_geo`（04:30 JST）の後に走らせるので、
        その日の近傍と行政区画が入ったものが固まる。**同じパスに上書きする**ので、
        何度実行しても結果は変わらない。
        """
        secret = os.environ.get("CRON_SECRET", "")
        if not is_authorized(request.headers.get("authorization"), secret):
            return _problem(401, "unauthorized", "CRON_SECRET が一致しません。")

        now = datetime.now(UTC)
        try:
            day = date.fromisoformat(date_text) if date_text else yesterday(now)
        except ValueError:
            return _problem(400, "invalid_date", "date は YYYY-MM-DD（JST の暦日）です。")

        try:
            with make_reference_port() as port:
                summary = build_and_upload(port, day, now)
        except MissingConfigError as cause:
            return _problem(500, "misconfigured", str(cause))
        except Exception as cause:
            print(f"未処理の例外: {type(cause).__name__}", file=sys.stderr)
            return _problem(500, "unhandled", type(cause).__name__)

        return JSONResponse(summary, status_code=200, headers=NO_STORE)

    @app.get("/ml/weather")
    def weather(request: Request) -> JSONResponse:
        """天気の予報を `weather_hourly` に取り込む（W4 プラン §6.4）。

        **何を取り込むかは DB が決める**（`v_weather_pending`）。取りこぼした発行は
        次の実行が拾い、**同じ発行を入れ直しても結果は変わらない**。取得（毎時 :17）の
        8 分あとに走らせる。
        """
        secret = os.environ.get("CRON_SECRET", "")
        if not is_authorized(request.headers.get("authorization"), secret):
            return _problem(401, "unauthorized", "CRON_SECRET が一致しません。")

        try:
            with make_weather_port() as port:
                summary = run_load(port, datetime.now(UTC))
        except MissingConfigError as cause:
            return _problem(500, "misconfigured", str(cause))
        except Exception as cause:
            print(f"未処理の例外: {type(cause).__name__}", file=sys.stderr)
            return _problem(500, "unhandled", type(cause).__name__)

        return JSONResponse(
            summary.as_dict(), status_code=200 if summary.ok else 500, headers=NO_STORE
        )

    @app.get("/ml/infer/{system}")
    def infer(request: Request, system: str) -> JSONResponse:
        """5 分毎の先回り推論（W3 プラン §5.10）。

        **同じ観測時刻に対する 2 度目は何もせずに 200 を返す**（`inference_log` の
        一意制約で掴む）。Vercel Cron の二重起動は無害になる。
        """
        secret = os.environ.get("CRON_SECRET", "")
        if not is_authorized(request.headers.get("authorization"), secret):
            return _problem(401, "unauthorized", "CRON_SECRET が一致しません。")

        if system not in KNOWN_SYSTEMS:
            return _problem(400, "unknown_system", f"知らないシステムです: {system}")

        model_version = os.environ.get(MODEL_VERSION_ENV, "")
        if not model_version:
            return _problem(500, "misconfigured", f"{MODEL_VERSION_ENV} が未設定です。")

        try:
            with make_infer_port() as port:
                summary = run_inference(port, system, model_version, datetime.now(UTC))
        except MissingConfigError as cause:
            return _problem(500, "misconfigured", str(cause))
        except Exception as cause:
            # ここに来るのは掴む前に落ちたときだけ。掴んだ後の失敗は `run_inference` が
            # `inference_log` に記録し、`status = "failed"` の要約として返る
            print(f"未処理の例外: {type(cause).__name__}", file=sys.stderr)
            return _problem(500, "unhandled", type(cause).__name__)

        return JSONResponse(
            infer_detail(summary), status_code=200 if summary.ok else 500, headers=NO_STORE
        )

    return app
