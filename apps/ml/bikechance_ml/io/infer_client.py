"""本番の推論の口を、試し打ちのために叩く（W8 の PR G。`jobs/dry_run.py` の `CallsInfer`）。

**副作用の置き場所はここだけ。** 叩くのは `GET {origin}/ml/infer/{系統}?model=<版>` で、
active でない版なら本番は何も書かない（`jobs/infer.py` の `_dry_run`）。

**URL と秘密を外に出さない**（CLAUDE.md §5）。`CRON_SECRET` はヘッダにだけ載せ、httpx の
例外（文言に要求 URL を含む）は再送出せず、系統と例外の種類だけの `InferCallError` に
詰め替える。**応答が JSON の辞書でなければ止める**（要約を読めない回を合格にしない）。
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Final

import httpx

from bikechance_ml.json_shape import ShapeError, as_dict

#: 1 回の上限（秒）。本番の関数の `maxDuration`（240 秒。`vercel.json`）より少し長く待つ。
REQUEST_TIMEOUT_S: Final[float] = 280.0

#: 接続の上限（秒）。
CONNECT_TIMEOUT_S: Final[float] = 10.0


class InferCallError(RuntimeError):
    """試し打ちが応答を返さなかった。**URL も秘密も持たない。**"""

    def __init__(self, system_id: str, http_status: int | None, error_name: str) -> None:
        self.system_id = system_id
        self.http_status = http_status
        self.error_name = error_name
        super().__init__(f"試し打ちに失敗しました: {system_id} / {http_status} / {error_name}")


class InferClient:
    """`jobs/dry_run.py` の `CallsInfer` を満たす。"""

    def __init__(self, origin: str, cron_secret: str, client: httpx.Client) -> None:
        self._origin = origin.rstrip("/")
        self._secret = cron_secret
        self._client = client

    def call(self, system_id: str, model_version: str) -> tuple[int, Mapping[str, object]]:
        try:
            response = self._client.get(
                f"{self._origin}/ml/infer/{system_id}",
                params={"model": model_version},
                headers={"Authorization": f"Bearer {self._secret}"},
            )
        except httpx.HTTPError as cause:
            raise InferCallError(system_id, None, type(cause).__name__) from None
        try:
            return response.status_code, as_dict(response.json(), "infer")
        except (ShapeError, ValueError) as cause:
            raise InferCallError(system_id, response.status_code, type(cause).__name__) from None


@contextmanager
def open_infer_client(origin: str, cron_secret: str) -> Iterator[InferClient]:
    """本番で使う組み立て。接続を確実に閉じる。**転送先は追わない**（Cron の作法と同じ）。"""
    timeout = httpx.Timeout(REQUEST_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        yield InferClient(origin, cron_secret, client)
