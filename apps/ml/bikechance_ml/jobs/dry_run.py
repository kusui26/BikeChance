"""候補の版を本番の口で試し打ちする（W8 の PR G、W8-02・W8-06）。

#4・#5 で手で回した `dryrun.sh` をパッケージに移したもの。`/ml/infer/{系統}?model=<版>` は、
**active でない版なら掴まず、どこにも書かずに要約だけ返す**（`jobs/infer.py` の `_dry_run`）。
確かめるのは「本番の関数のメモリと時間で、この成果物を開いて予測まで作れるか」である。

**本番の周期と重ねない。** 推論はドコモが毎 5 分の 1 分、HELLO が 4 分に始まる（`vercel.json`）。
同じインスタンスで重なると、成果物を 2 つ開いた山でメモリが足りなくなりうる。試し打ちは
その合間に入れる：**HELLO は 2 分 20 秒、ドコモは 3 分 00 秒**から。窓（`SLOT_WINDOW_S`）を
過ぎたら次の周期まで待ち、**系統ごとに 1 周期 1 回**にする。#4・#5 は HELLO を続けて 2 回
叩いたのでドコモを 3 分 30 秒に置いたが、1 回ずつなら前に寄せられ、HELLO の本番（4 分）まで
30 秒の余裕が残る（ドコモの試し打ちは 13〜14 秒。10/26 の実測）。

**URL も秘密も記録に出さない**（CLAUDE.md §5）。HTTP の口は `io/infer_client.py` にあり、
失敗は系統・HTTP の状態・例外の種類に詰め替えてある。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

from bikechance_ml.jobs.clock import Clock

#: 推論の周期（秒）。`vercel.json` の `*/5` と同じ。
CYCLE_S: Final[int] = 300

#: 周期の中の、試し打ちを始める位置（秒）。**本番の推論（ドコモ 60 秒・HELLO 240 秒）の間。**
SLOT_OFFSETS_S: Final[Mapping[str, int]] = {"hellocycling": 140, "docomo-cycle": 180}

#: 窓の幅（秒）。**この中で始められなければ、次の周期まで待つ。**
SLOT_WINDOW_S: Final[int] = 15

#: 系統ごとに叩く回数（2 回目は温まった成果物での所要を見る）。
CALLS_PER_SYSTEM: Final[int] = 2

#: 推論の合格線（W8 プラン §1.2：`rss_mb` の最大 < 1.6 GB）。
RSS_LIMIT_MB: Final[int] = 1_600

#: 記録に残す応答の欄（`jobs/infer.py` の要約）。
SUMMARY_KEYS: Final[tuple[str, ...]] = (
    "status",
    "model_version",
    "model_kind",
    "predicted",
    "unknown_ports",
    "duration_ms",
    "cpu_ms",
    "features_ms",
    "model_load_ms",
    "rss_mb",
    "profile_date",
    "error",
)

#: 試し打ちの応答の `status`（`jobs/infer.py` が active でない版で返す値）。
DRY_RUN_STATUS: Final[str] = "dry_run"

HTTP_OK: Final[int] = 200


class CallsInfer(Protocol):
    """本番の `/ml/infer/{系統}?model=<版>` を 1 回叩く口（`io/infer_client.py`）。"""

    def call(self, system_id: str, model_version: str) -> tuple[int, Mapping[str, object]]: ...


@dataclass(frozen=True)
class DryRun:
    """1 回の試し打ち。**応答の要約だけ**を持つ（予測の値は持たない）。"""

    system_id: str
    attempt: int
    started_at: datetime
    http_status: int
    summary: Mapping[str, object]

    def problems(self, model_version: str) -> list[str]:
        """合格でない理由。**HTTP・状態・版・予測の数・メモリ**の 5 つを見る。"""
        found = [f"HTTP {self.http_status}"] if self.http_status != HTTP_OK else []
        if self.summary.get("status") != DRY_RUN_STATUS:
            found.append(f"status {self.summary.get('status')}")
        if self.summary.get("model_version") != model_version:
            found.append(f"版 {self.summary.get('model_version')}")
        found += _count_problems(self.summary)
        return [f"{self.system_id} の {self.attempt} 回目：{one}" for one in found]


def _count_problems(summary: Mapping[str, object]) -> list[str]:
    predicted, rss_mb = summary.get("predicted"), summary.get("rss_mb")
    found = [] if isinstance(predicted, int) and predicted > 0 else [f"予測 {predicted}"]
    if not isinstance(rss_mb, int) or rss_mb >= RSS_LIMIT_MB:
        found.append(f"rss_mb {rss_mb}（{RSS_LIMIT_MB} 未満が合格）")
    return found


def position_s(now: datetime) -> float:
    """周期の中の位置（秒）。**周期は毎時 0 分から 5 分ごと**（本番の Cron と同じ）。"""
    return (now.minute % (CYCLE_S // 60)) * 60 + now.second + now.microsecond / 1_000_000


def seconds_until(offset_s: int, now: datetime) -> float:
    """次に枠が開くまでの秒。**いま窓の中なら 0。**"""
    position = position_s(now)
    if offset_s <= position < offset_s + SLOT_WINDOW_S:
        return 0.0
    return (offset_s - position) % CYCLE_S


def plan(systems: Sequence[str]) -> list[tuple[int, str]]:
    """叩く順：**1 周期に系統ごと 1 回**、それを `CALLS_PER_SYSTEM` 周期くり返す。"""
    ordered = sorted(systems, key=lambda system_id: SLOT_OFFSETS_S[system_id])
    return [(attempt, one) for attempt in range(1, CALLS_PER_SYSTEM + 1) for one in ordered]


def run(
    client: CallsInfer, clock: Clock, model_version: str, systems: Sequence[str]
) -> tuple[DryRun, ...]:
    """枠を待って叩く。**合否は呼ぶ側が `problems` で決める**（全部の結果を記録に残すため）。"""
    results: list[DryRun] = []
    for attempt, system_id in plan(systems):
        clock.sleep(seconds_until(SLOT_OFFSETS_S[system_id], clock.now()))
        started = clock.now()
        status, body = client.call(system_id, model_version)
        results.append(DryRun(system_id, attempt, started, status, _summary_of(body)))
    return tuple(results)


def _summary_of(body: Mapping[str, object]) -> dict[str, object]:
    return {key: body[key] for key in SUMMARY_KEYS if key in body}


def problems_of(results: Sequence[DryRun], model_version: str) -> list[str]:
    """全部の試し打ちの、合格でない理由。**空なら合格。**"""
    return [problem for one in results for problem in one.problems(model_version)]


def peak_rss_mb(results: Sequence[DryRun]) -> int | None:
    """試し打ちで見えたメモリの山（MB）。"""
    values = [one.summary.get("rss_mb") for one in results]
    numbers = [value for value in values if isinstance(value, int)]
    return max(numbers, default=None)
