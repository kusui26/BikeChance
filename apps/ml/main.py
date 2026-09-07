"""Vercel の FastAPI プリセットが読む入口。

**中身は置かない。** アプリの組み立ては `bikechance_ml/api.py` にあり、ここは
デプロイの都合で必要な 1 行だけを持つ。フレームワークのプリセットは慣例として
リポジトリ直下の `main.py` の `app` を探すため、その約束にだけ従う。
"""

from bikechance_ml.api import build_app

app = build_app()
