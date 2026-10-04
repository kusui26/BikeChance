"""モデルを置く・登録するジョブが共有する、出力先とモデルカードの決まり（W6 の PR F・H）。

`fit_lightgbm` と `build_composite` の 2 つが使う。**`lightgbm` を読み込まない**ので、
合成器の組み立てがここを使っても、当てはめの依存を引き込まない。
"""

import re
from pathlib import Path
from typing import Final

#: 出力先に書けるひな形。**版の名前は学習日と印で決まり、走らせる前には分からない**ので、
#: `--card ../../docs/model_cards/{version}.md` のように書く。
VERSION_FIELD: Final[str] = "{version}"

#: `--label`（版の名前に挟む印。予行演習は `rehearsal`）の書き方。**ハイフンを許さない**
#: ——許すと、版の名前から印と日付を切り分けられなくなる。
LABEL_PATTERN: Final[re.Pattern[str]] = re.compile(r"\A[a-z][a-z0-9]{0,19}\Z")


def expand(path: str | None, version: str) -> str | None:
    """出力先の `{version}` を版の名前に置き換える。"""
    return None if path is None else path.replace(VERSION_FIELD, version)


def card_reference(card_path: str | None) -> str | None:
    """登録簿に残すモデルカードの場所。**リポジトリからの相対に直す。**

    `--card` はシェルから見た**書き出し先**なので、`apps/ml` で走らせると
    `../../docs/model_cards/…` になる。それをそのまま登録簿に入れると、
    **読む人が「どこ起点の相対か」を復元できない**（2026-09-10 に 1 度そうなった）。

    `.git` のある場所を上へ辿って、そこからの相対にする。見つからなければ
    渡された文字列をそのまま残す（**勝手に別の場所を指さない**）。
    """
    if card_path is None:
        return None
    resolved = Path(card_path).resolve()
    for parent in resolved.parents:
        if (parent / ".git").exists():
            return str(resolved.relative_to(parent))
    return card_path
