"""記録する文字列から秘密を落とす（CLAUDE.md §5）。

web 側の `apps/web/lib/jobs/errors.ts` の `redact()` と同じ役割。**例外の文言を
そのまま記録しない**ための最後の関門で、通す文字列は必ずここを経由させる。

httpx の例外は要求 URL を文言に含む。Supabase の URL はプロジェクトを特定する情報で、
サービスロールキーはヘッダにしか載せていないが、「載っていないはず」に頼らず機械で消す。
"""

from collections.abc import Iterable

#: 置き換え後の文字列。長さを保たないのは、長さから元の値を推測させないため。
MASK = "***"

#: 短すぎる値は誤爆する（`/` や `a` を全部消してしまう）。この長さ未満は対象にしない。
MIN_SECRET_LENGTH = 8


def redact(text: str, secrets: Iterable[str]) -> str:
    """`secrets` の各値を伏せ字にする。空や短すぎる値は無視する。"""
    masked = text
    for secret in secrets:
        if len(secret) >= MIN_SECRET_LENGTH:
            masked = masked.replace(secret, MASK)
    return masked
