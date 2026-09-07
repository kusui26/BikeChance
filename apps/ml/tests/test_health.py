"""`/ml/health` の契約（W2 プラン §5.5）。

ここで固定したいのは 3 つ。
  * パスが `/ml` から始まること（rewrite は URL を書き換えないので、接頭辞はアプリが持つ）
  * 依存を持たないこと（落ちたらランタイム自体の問題だと言い切れるようにする）
  * 公開ドキュメントを出さないこと（Cron と内部からしか叩かない）
"""

from fastapi.testclient import TestClient

from bikechance_ml.api import HEALTH_SCHEMA_VERSION, SERVICE_NAME, build_app


def test_health_returns_service_identity() -> None:
    with TestClient(build_app()) as client:
        response = client.get("/ml/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] == "true"
    assert body["service"] == SERVICE_NAME
    assert body["schema_version"] == HEALTH_SCHEMA_VERSION


def test_health_is_mounted_under_ml_prefix() -> None:
    """接頭辞を落とすと web 側の catch-all に食われて 404 になる。"""
    with TestClient(build_app()) as client:
        assert client.get("/health").status_code == 404


def test_openapi_is_not_exposed() -> None:
    with TestClient(build_app()) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
