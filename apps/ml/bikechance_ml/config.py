"""環境変数の読み取り（CLAUDE.md §5）。

**値をエラーに出さない。** 足りないときに出すのは変数名だけ。web 側の
`apps/web/lib/jobs/env.ts` と同じ規律で、設定漏れを 500 として気づけるようにしつつ、
秘密がログや応答に混ざる経路を作らない。
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

#: Storage を読むだけなら、この 2 つで足りる。
STORAGE_REQUIRED: Final[tuple[str, ...]] = ("SUPABASE_URL", "SUPABASE_SECRET_KEY")

#: 圧縮ジョブが要る変数。1 つでも欠けたら動かさない。
REQUIRED: Final[tuple[str, ...]] = (*STORAGE_REQUIRED, "CRON_SECRET")


class MissingConfigError(RuntimeError):
    """設定が足りない。**メッセージには変数名しか入れない。**"""

    def __init__(self, names: tuple[str, ...]) -> None:
        self.names = names
        super().__init__(f"環境変数が足りません: {', '.join(names)}")


@dataclass(frozen=True)
class StorageConfig:
    """Supabase に届くために要る最小限。読み取りだけの用途はこれで足りる。"""

    supabase_url: str
    supabase_secret_key: str


@dataclass(frozen=True)
class Config:
    supabase_url: str
    supabase_secret_key: str
    cron_secret: str


def _require(source: Mapping[str, str], names: tuple[str, ...]) -> None:
    missing = tuple(name for name in names if not source.get(name))
    if missing:
        raise MissingConfigError(missing)


def read_storage_config(environ: Mapping[str, str] | None = None) -> StorageConfig:
    """**読み取りだけの経路が `CRON_SECRET` を要求しない**ようにする。

    分析（`analysis/`）は Storage を読むだけで、Cron の認証には関係が無い。
    要らない設定まで必須にすると、無関係な設定漏れで分析が動かなくなる。
    """
    source = os.environ if environ is None else environ
    _require(source, STORAGE_REQUIRED)
    return StorageConfig(
        supabase_url=source["SUPABASE_URL"].rstrip("/"),
        supabase_secret_key=source["SUPABASE_SECRET_KEY"],
    )


def read_config(environ: Mapping[str, str] | None = None) -> Config:
    """環境変数を読む。空文字は「未設定」として扱う。"""
    source = os.environ if environ is None else environ
    _require(source, REQUIRED)
    storage = read_storage_config(source)
    return Config(
        supabase_url=storage.supabase_url,
        supabase_secret_key=storage.supabase_secret_key,
        cron_secret=source["CRON_SECRET"],
    )
