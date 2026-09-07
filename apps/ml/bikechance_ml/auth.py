"""Cron の認証（CLAUDE.md §3 の「Cron ハンドラの定型」）。

web 側の `apps/web/lib/jobs/auth.ts` と同じ方針。**先にハッシュを取ってから比べる**
ことで、長さの違いから秘密の長さが漏れないようにし、比較そのものは定数時間で行う。
"""

import hashlib
import hmac


def is_authorized(header: str | None, secret: str) -> bool:
    """`Authorization: Bearer <secret>` が一致するか。

    設定が空のときは常に false。「秘密が未設定なら誰でも通る」を作らない。
    """
    if not secret or header is None:
        return False
    prefix = "Bearer "
    if not header.startswith(prefix):
        return False
    given = header[len(prefix) :]
    # 長さが違っても比較時間が変わらないよう、固定長のダイジェストにしてから比べる
    return hmac.compare_digest(
        hashlib.sha256(given.encode("utf-8")).digest(),
        hashlib.sha256(secret.encode("utf-8")).digest(),
    )
