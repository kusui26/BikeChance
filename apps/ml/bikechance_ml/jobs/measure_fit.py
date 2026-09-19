"""当てはめの山（peak RSS）を、**当てはめる機械の上で**測る（W5 プラン §12 の 168 の検算）。

**なぜ別の道具が要るか。** 「28 日ぶんは 14.2 GiB」という数字は、**手元の macOS
（8 GB、スワップしながら）の 2 点から 5.6 倍に外挿**したものである。週次の再学習が
載る `ubuntu-latest`（16 GB）では**一度も測っていない**。

**OS が違えば計器も違う。** macOS の `ru_maxrss` は圧縮メモリぶん少なく出るので
（実測 0.73 GB 対 `peak memory footprint` 3.08 GB）、あちらでは footprint を見る。
**Linux の `ru_maxrss` は常駐の高水位そのもの**で、OOM キラーが見るのもこれである。
**2 つの数を並べるときは、計器が違うことを必ず添える。**

**書かない。** 子に渡す引数は `child_argv` が組み立てる 1 か所だけで、
`--upload` も `--register` も入らない（`tests/test_measure_fit.py` が留める）。

**山だけでなく曲線も残す。** 「どこが重いか」が分かれば次の梃子が決まる——
`load_days` が読んだ表を抱えたまま行列を作るのか、当てはめそのものが重いのか。
1 つの数では区別できない。

使い方（環境変数は `.env` から読み込んでから）:

    cd apps/ml
    ./.venv/bin/python -m bikechance_ml.jobs.measure_fit \\
        --from 2026-09-08 --to 2026-09-18 --report /tmp/fit.md

GitHub Actions では `.github/workflows/measure-fit.yml` が水準を並べて呼ぶ。
"""

import argparse
import json
import os
import re
import resource
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final

#: 常駐の大きさを見に行く間隔。**細かくしすぎると測る側が重くなる**（1 回で `/proc` を 1 つ読む）。
SAMPLE_INTERVAL_S: Final[float] = 1.0

#: 要約に載せる曲線の点数。1 時間の走りは 3,600 点になるので間引く。
CURVE_POINTS: Final[int] = 24

#: 走っている最中に 1 行出す間隔。**殺されると要約は書けない**ので、記録に残す。
PROGRESS_INTERVAL_S: Final[float] = 60.0

#: `ru_maxrss` の単位は OS で違う。**Linux は KiB、macOS はバイト**（取り違えると 1,024 倍ずれる）。
_RUSAGE_IN_BYTES: Final[bool] = sys.platform == "darwin"

#: 1 GiB は何 KiB か。**表に出す数はすべて GiB（1024³）**で、OOM の予算と単位を揃える。
KIB_PER_GIB: Final[int] = 1024 * 1024

#: 子に渡してはいけない引数。**この道具は測るだけで、成果物も登録簿も触らない。**
FORBIDDEN_ARGUMENTS: Final[tuple[str, ...]] = ("--upload", "--register")

_ROWS = re.compile(r"学習 ([\d,]+) 行 / 検証 ([\d,]+) 行")


@dataclass(frozen=True)
class Window:
    """測る窓。**`fit_lightgbm` にそのまま渡す形**で持つ。"""

    start: date
    end: date
    eval_days: int
    purge_days: int
    allow_mixed_weather: bool

    def describe(self) -> str:
        mixed = "（天気の混在を許す）" if self.allow_mixed_weather else ""
        return f"{self.start}〜{self.end}{mixed}"


@dataclass(frozen=True)
class Sample:
    """ある時点の常駐の大きさ。"""

    at_s: float
    rss_kib: int
    swap_kib: int


@dataclass(frozen=True)
class Measured:
    """1 回の当てはめの測定結果。"""

    window: Window
    peak_kib: int
    swap_peak_kib: int
    seconds: float
    exit_code: int
    samples: tuple[Sample, ...]
    #: 報告書から拾った `(学習, 検証)` の行数。**落ちていれば報告書が無いので `None`。**
    rows: tuple[int, int] | None


def child_argv(window: Window, report: Path) -> tuple[str, ...]:
    """当てはめを起こす引数。**`--upload` も `--register` も入れない。**

    **組み立てる場所をここ 1 か所にする。** 呼ぶ側が自由に足せる形にすると、
    「測るだけのはずが本番の成果物を上書きした」が起こり得る（§12 の 157）。
    """
    argv = [
        sys.executable,
        "-m",
        "bikechance_ml.jobs.fit_lightgbm",
        "--from",
        window.start.isoformat(),
        "--to",
        window.end.isoformat(),
        "--eval-days",
        str(window.eval_days),
        "--purge-days",
        str(window.purge_days),
        "--report",
        str(report),
    ]
    if window.allow_mixed_weather:
        argv.append("--allow-mixed-weather")
    return tuple(argv)


def field_kib(text: str, name: str) -> int:
    """`/proc` の `名前:\t   1234 kB` を数にする。**無ければ 0。**"""
    found = re.search(rf"^{name}:\s+(\d+) kB$", text, re.MULTILINE)
    return int(found.group(1)) if found else 0


def _read_proc(path: str) -> str | None:
    """`/proc` の 1 つを読む。**Linux 以外や、もう居ない子は `None`。**"""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def read_status(pid: int) -> Sample | None:
    """走っている子の常駐と退避の大きさ。**もう居なければ `None`。**"""
    text = _read_proc(f"/proc/{pid}/status")
    if text is None:
        return None
    return Sample(at_s=0.0, rss_kib=field_kib(text, "VmRSS"), swap_kib=field_kib(text, "VmSwap"))


def meminfo_kib(name: str) -> int:
    """機械そのものの大きさ（`MemTotal` / `SwapTotal`）。**読めなければ 0。**"""
    text = _read_proc("/proc/meminfo")
    return field_kib(text, name) if text is not None else 0


def to_kib(raw: int, *, from_bytes: bool) -> int:
    """`ru_maxrss` を KiB に直す。**macOS はバイトで返す**（`man getrusage` の差）。"""
    return raw // 1024 if from_bytes else raw


def _gib(kib: int) -> str:
    return f"{kib / KIB_PER_GIB:.2f}"


def _both(kib: int) -> str:
    """**GiB とバイトを並べて出す。** 「GB」が 1024³ か 10⁹ かで **7.4% ずれる**——

    168 の記録はその区別を書いていないので、**後から読む人が換算し直せる**ように
    生の値を添える。単位の取り違えは、値の間違いより気づきにくい。
    """
    return f"{_gib(kib)} GiB（{kib * 1024:,} バイト）"


def child_peak_kib() -> int:
    """**待ち終えた子**の常駐の高水位。OOM キラーが見るのと同じ量である。"""
    return to_kib(
        resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss, from_bytes=_RUSAGE_IN_BYTES
    )


def tell(sample: Sample, told_at_s: float) -> float:
    """`PROGRESS_INTERVAL_S` ごとに 1 行だけ書く。**返すのは次に比べる時刻。**

    **OOM で殺されると要約は書けない。** 記録に残った最後の 1 行が、
    「どこまで登ったか」の唯一の証拠になる。
    """
    if sample.at_s - told_at_s < PROGRESS_INTERVAL_S:
        return told_at_s
    print(
        f"[measure] {sample.at_s / 60:5.1f} 分  常駐 {_gib(sample.rss_kib)} GiB"
        f"  退避 {_gib(sample.swap_kib)} GiB",
        file=sys.stderr,
        flush=True,
    )
    return sample.at_s


def watch(child: subprocess.Popen[bytes], interval_s: float) -> tuple[Sample, ...]:
    """子が終わるまで常駐の大きさを測り続ける。**曲線が次の梃子を決める。**"""
    started = time.monotonic()
    seen: list[Sample] = []
    told_at_s = 0.0
    while child.poll() is None:
        now = read_status(child.pid)
        if now is not None:
            seen.append(Sample(time.monotonic() - started, now.rss_kib, now.swap_kib))
            told_at_s = tell(seen[-1], told_at_s)
        time.sleep(interval_s)
    return tuple(seen)


def read_rows(report: Path) -> tuple[int, int] | None:
    """報告書から件数を拾う。**当てはめが落ちていれば報告書そのものが無い。**"""
    if not report.exists():
        return None
    found = _ROWS.search(report.read_text(encoding="utf-8"))
    if found is None:
        return None
    return int(found.group(1).replace(",", "")), int(found.group(2).replace(",", ""))


def measure(window: Window, report: Path, interval_s: float = SAMPLE_INTERVAL_S) -> Measured:
    """当てはめを 1 回起こして測る。**子の出力はそのまま親へ流す**（記録に残す）。"""
    started = time.monotonic()
    child = subprocess.Popen(child_argv(window, report))
    samples = watch(child, interval_s)
    code = child.wait()
    return Measured(
        window=window,
        peak_kib=child_peak_kib(),
        swap_peak_kib=max((one.swap_kib for one in samples), default=0),
        seconds=time.monotonic() - started,
        exit_code=code,
        samples=samples,
        rows=read_rows(report),
    )


def curve(samples: Sequence[Sample], points: int = CURVE_POINTS) -> tuple[Sample, ...]:
    """曲線を `points` 点に間引く。**山の点は必ず残す**（間引きで峰を落とさない）。"""
    if len(samples) <= points or points < 1:
        return tuple(samples)
    step = len(samples) / points
    picked = {int(index * step) for index in range(points)}
    picked.add(max(range(len(samples)), key=lambda index: samples[index].rss_kib))
    return tuple(samples[index] for index in sorted(picked))


def verdict(measured: Measured, total_kib: int) -> str:
    """一言でどうだったか。**落ちたことも結果である**ので、隠さず書く。"""
    if measured.exit_code == -9:
        return "**殺された（SIGKILL）。OOM の疑い**——山が機械の大きさに届いている"
    if measured.exit_code != 0:
        return f"**当てはめが失敗した**（終了コード {measured.exit_code}）。上の記録を読むこと"
    if total_kib and measured.peak_kib > total_kib * 0.9:
        return "**載ったが余裕が 10% を切っている。** 週次の自動ジョブとしては薄い"
    return "**載った。**"


def _headline(measured: Measured, total_kib: int) -> list[str]:
    """要約の見出し（表の前）。"""
    fit, evaluate = measured.rows if measured.rows is not None else (0, 0)
    swap = f"{_gib(measured.swap_peak_kib)} / {_gib(meminfo_kib('SwapTotal'))}"
    return [
        f"## 当てはめの山：学習 {measured.window.describe()}",
        "",
        f"- **山（peak RSS）**：**{_both(measured.peak_kib)}**",
        f"- **機械**：{_gib(total_kib)} GiB（`MemTotal`）、退避は {swap} GiB",
        f"- **行**：学習 {fit:,} / 検証 {evaluate:,}",
        f"- **所要**：{measured.seconds / 60:.1f} 分（`{sys.platform}`）",
        "",
        verdict(measured, total_kib),
        "",
    ]


def _curve_rows(measured: Measured) -> list[str]:
    """曲線の表。**山がどこで立つか**が分かれば、次に外すものが決まる。"""
    points = curve(measured.samples)
    if not points:
        return ["（曲線を測れなかった。`/proc` が読めない環境である）", ""]
    top = max(points, key=lambda one: one.rss_kib)
    rows = ["| 経過（分） | 常駐（GiB） | 退避（GiB） |", "|---:|---:|---:|"]
    rows += [
        f"| {one.at_s / 60:.1f}{' **←山**' if one is top else ''} "
        f"| {_gib(one.rss_kib)} | {_gib(one.swap_kib)} |"
        for one in points
    ]
    return [*rows, ""]


def to_markdown(measured: Measured, total_kib: int) -> str:
    """人が読む要約。**Actions の要約欄にそのまま貼る。**"""
    lines = _headline(measured, total_kib)
    lines += ["### 常駐の曲線", ""]
    lines += _curve_rows(measured)
    lines += [
        "> **macOS の数と直接は比べられない。** あちらの `ru_maxrss` は圧縮メモリぶん",
        "> 少なく出るので `peak memory footprint` を見る（§12 の 168）。**計器が違う。**",
        "",
    ]
    return "\n".join(lines)


def to_json(measured: Measured, total_kib: int) -> str:
    """後から並べるための機械可読な形。**曲線は間引かずに全部入れる。**"""
    return json.dumps(
        {
            "start": measured.window.start.isoformat(),
            "end": measured.window.end.isoformat(),
            "platform": sys.platform,
            "peak_kib": measured.peak_kib,
            "swap_peak_kib": measured.swap_peak_kib,
            "mem_total_kib": total_kib,
            "seconds": round(measured.seconds, 1),
            "exit_code": measured.exit_code,
            "fit_rows": measured.rows[0] if measured.rows else None,
            "evaluate_rows": measured.rows[1] if measured.rows else None,
            "samples": [
                [round(one.at_s, 1), one.rss_kib, one.swap_kib] for one in measured.samples
            ],
        },
        ensure_ascii=False,
    )


def append_summary(text: str) -> None:
    """Actions の要約欄へ足す。**無い環境では何もしない。**"""
    where = os.environ.get("GITHUB_STEP_SUMMARY")
    if not where:
        return
    with Path(where).open("a", encoding="utf-8") as handle:
        handle.write(text)


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="当てはめの山を測る（W5 §12 の 168）")
    parser.add_argument("--from", dest="start", required=True, help="窓の最初の日（JST）")
    parser.add_argument("--to", dest="end", required=True, help="窓の最後の日（JST）")
    parser.add_argument("--eval-days", type=int, default=2, help="検証に使う日数")
    parser.add_argument("--purge-days", type=int, default=1, help="パージの日数")
    parser.add_argument("--allow-mixed-weather", action="store_true", help="天気の混在を許す")
    parser.add_argument("--report", required=True, help="当てはめの報告書の出力先")
    parser.add_argument("--json", dest="json_path", default=None, help="測定結果の JSON の出力先")
    parser.add_argument(
        "--interval-s", type=float, default=SAMPLE_INTERVAL_S, help="常駐を見に行く間隔（秒）"
    )
    return parser.parse_args(argv)


def _window(options: argparse.Namespace) -> Window:
    return Window(
        start=date.fromisoformat(options.start),
        end=date.fromisoformat(options.end),
        eval_days=options.eval_days,
        purge_days=options.purge_days,
        allow_mixed_weather=options.allow_mixed_weather,
    )


def run(argv: Sequence[str] | None = None) -> int:
    """測って、書き出す。**当てはめが落ちたらこちらも落ちる**（赤いほうが気づく）。"""
    options = _arguments(argv)
    measured = measure(_window(options), Path(options.report), options.interval_s)
    total_kib = meminfo_kib("MemTotal")
    text = to_markdown(measured, total_kib)
    print(text)
    append_summary(text)
    if options.json_path:
        Path(options.json_path).write_text(to_json(measured, total_kib), encoding="utf-8")
    return 0 if measured.exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run())
