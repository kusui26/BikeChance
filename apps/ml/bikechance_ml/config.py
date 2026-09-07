"""環境変数の読み取り（CLAUDE.md §5）。

**値をエラーに出さない。** 足りないときに出すのは変数名だけ。web 側の
`apps/web/lib/jobs/env.ts` と同じ規律で、設定漏れを 500 として気づけるようにしつつ、
秘密がログや応答に混ざる経路を作らない。
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

#: 圧縮ジョブが要る変数。1 つでも欠けたら動かさない。
REQUIRED: Final[tuple[str, ...]] = (
    "SUPABASE_URL",
    "SUPABASE_SECRET_KEY",
    "CRON_SECRET",
)


class MissingConfigError(RuntimeError):
    """設定が足りない。**メッセージには変数名しか入れない。**"""

    def __init__(self, names: tuple[str, ...]) -> None:
        self.names = names
        super().__init__(f"環境変数が足りません: {', '.join(names)}")


@dataclass(frozen=True)
class Config:
    supabase_url: str
    supabase_secret_key: str
    cron_secret: str


def read_config(environ: Mapping[str, str] | None = None) -> Config:
    """環境変数を読む。空文字は「未設定」として扱う。"""
    source = os.environ if environ is None else environ
    missing = tuple(name for name in REQUIRED if not source.get(name))
    if missing:
        raise MissingConfigError(missing)
    return Config(
        supabase_url=source["SUPABASE_URL"].rstrip("/"),
        supabase_secret_key=source["SUPABASE_SECRET_KEY"],
        cron_secret=source["CRON_SECRET"],
    )
