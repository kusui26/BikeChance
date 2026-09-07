"""FastAPI アプリの組み立て（開発プラン §12.1 の `bikechance_ml/api.py`）。

**ルートは `/ml` から始める。** `vercel.json` の rewrite は
`{ "service": "ml" }` だけを指定しており、Vercel のスキーマにあるとおり
「path はルート選択にのみ使われ、ユーザーコードから見える URL を書き換えない」。
つまりサービスには `/ml/health` がそのまま届く。接頭辞はこのアプリが持つ。

W2 の PR C ではエンドポイントは `/ml/health` の 1 本だけにする。DB にも Storage にも
触らない。ここで確かめたいのは「Vercel Services に Python を足しても収集が
止まらないか」の一点で、依存や副作用を混ぜると切り分けができなくなる。
"""

from typing import Final

from fastapi import FastAPI

from bikechance_ml import __version__

#: 応答の形式版。増やすときは iOS / web 側と揃える。
HEALTH_SCHEMA_VERSION: Final[str] = "1"

#: 収集（`/api/jobs/*`）と公開 API（`/v1/*`）を持つ web サービスとは別系統であることを、
#: 応答からも分かるようにしておく。障害調査でどちらを見ているか迷わないため。
SERVICE_NAME: Final[str] = "ml"


def build_app() -> FastAPI:
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

    return app
