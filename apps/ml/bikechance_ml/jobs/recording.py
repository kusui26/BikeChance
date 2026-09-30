"""`job_runs` への記録（W3 プラン §5.3、W4 プラン §12 の 116）。

**記録の不調でジョブを落とさない。** 畳めたこと・置けたこと・作れたことのほうが大事で、
記録は「あとから見張るため」の付け足しである。だから始まりも終わりも例外を飲む。

**飲むときに残すのは例外の種類だけ。** 文言には接続先が混じりうる（CLAUDE.md §5）。
`io/supabase.py` が投げる `SupabaseError` の文言は伏せ字を通っているが、**ここは
他の例外も通る場所**なので種類だけに切り詰める。**学習サンプルの日次生成は GitHub
Actions で走り、公開リポジトリの実行ログは誰でも読める**（PR J）ので、ここを緩めない。

**同じ 2 つの関数が 3 か所に散っていた**（`compact` / `build_reference`、そして PR J で
`build_features` が 3 つめになるところだった）。うち 1 つだけが例外の文言まで出しており、
**「どれが正しい形か」がコードから読めなかった**。1 か所に集める。
"""

import sys
from collections.abc import Mapping
from typing import Final, Protocol

#: `job_runs.status` が受ける**終わりの値**（migration 0053 の `job_runs_status_valid` から
#: `running` を除いたもの。`running` は `job_started` が DB の中で書く）。
#: **表の制約と検査で突き合わせる**（`tests/test_recording.py`）——偽の口は値を何でも受ける
#: ので、突き合わせないと本番でだけ弾かれる（W6 プランの所見 207）。
FINISHED_STATUSES: Final[frozenset[str]] = frozenset({"ok", "failed", "skipped"})

#: 表が受けない値を渡されたときに、代わりに閉じる値。
FALLBACK_STATUS: Final[str] = "failed"


class RecordsJobs(Protocol):
    """`job_runs` に書く口だけ。各ジョブの Port が構造的に満たす。"""

    def job_started(self, job_name: str) -> int: ...

    def job_finished(self, run_id: int, status: str, detail: Mapping[str, object]) -> None: ...


def started_quietly(port: RecordsJobs, job_name: str) -> int | None:
    """始まりを記録する。**書けなくても仕事はする**（`None` なら終わりも書かない）。"""
    try:
        return port.job_started(job_name)
    except Exception as cause:
        _log(f"job_started に失敗した: {type(cause).__name__}")
        return None


def record_quietly(
    port: RecordsJobs, run_id: int | None, status: str, detail: Mapping[str, object]
) -> None:
    """終わりを記録する。**始まりを書けていなければ何もしない。**

    **表が受けない値は `failed` で閉じる**（W6 プランの所見 207）。そのまま送ると DB が弾き、
    ここはその失敗を飲むので、行が `running` のまま残る——見張りからは「殺された回」に
    見える。`failed` なら `check_jobs_failed` が拾い、渡された値は `detail` に残る。
    """
    if run_id is None:
        return
    closing, closing_detail = _closing(status, detail)
    try:
        port.job_finished(run_id, closing, closing_detail)
    except Exception as cause:
        _log(f"job_finished に失敗した: {type(cause).__name__}")


def _closing(status: str, detail: Mapping[str, object]) -> tuple[str, Mapping[str, object]]:
    """表が受ける値ならそのまま。**受けない値は `failed` にし、渡された値を `detail` に残す。**"""
    if status in FINISHED_STATUSES:
        return status, detail
    _log(f"job_runs が受けない status を渡された: {status}")
    return FALLBACK_STATUS, {**detail, "unknown_status": status}


def _log(message: str) -> None:
    """実行ログに残す。**要約には載せない**（呼び出し元の失敗ではない）。"""
    print(message, file=sys.stderr)
