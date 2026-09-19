"""測る道具そのものを留める（W5 プラン §12 の 168 の検算）。

**測る道具が壊れていると、間違った数で判断する。** 168 でいちばん効いたのは
「macOS の `ru_maxrss` は圧縮メモリぶん少なく出る」だった——**計器の読み違いは、
値の間違いより気づきにくい**。だからここでは、**単位**と**書かないこと**と
**山を間引きで落とさないこと**の 3 つを重く見る。
"""

import itertools
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from bikechance_ml.jobs import fit_lightgbm, measure_fit

WINDOW = measure_fit.Window(
    start=date(2026, 9, 8),
    end=date(2026, 9, 18),
    eval_days=2,
    purge_days=1,
    allow_mixed_weather=False,
)

#: `/proc/<pid>/status` の抜粋（実物と同じ字間で書く。タブと空白が混ざる）
STATUS = "Name:\tpython3\nVmPeak:\t 1048576 kB\nVmRSS:\t  786432 kB\nVmSwap:\t    1024 kB\n"


# ── 書かないこと ──────────────────────────────────────────────
@pytest.mark.parametrize("mixed", [False, True])
def test_the_child_is_never_told_to_write(mixed: bool) -> None:
    """**測るだけ。** `--upload` も `--register` も子に渡らない。

    渡ると**本番の成果物を上書きする**（`--upload` は版の名前＝最終学習日で
    同じパスに書く。§12 の 157）。水準を並べて何度も回す道具なので、
    ここが破れると気づかないうちに登録簿まで動く。
    """
    argv = measure_fit.child_argv(
        WINDOW.__class__(**{**vars(WINDOW), "allow_mixed_weather": mixed}), Path("/tmp/r.md")
    )
    for forbidden in measure_fit.FORBIDDEN_ARGUMENTS:
        assert forbidden not in argv, f"{forbidden} を子に渡しています"


def test_the_forbidden_arguments_are_the_real_ones() -> None:
    """**禁じている名前が、当てはめの側に実在すること。**

    `fit_lightgbm` が旗の名前を変えたら、ここの定数は**何も守らなくなる**——
    綴りの違う文字列を探し続けて、いつまでも緑になる。
    """
    parsed = fit_lightgbm._arguments(["--from", "2026-09-08", "--to", "2026-09-18"])
    for forbidden in measure_fit.FORBIDDEN_ARGUMENTS:
        assert hasattr(parsed, forbidden.removeprefix("--").replace("-", "_"))


# ── 窓をそのまま渡す ──────────────────────────────────────────
def test_the_window_reaches_the_child() -> None:
    """日と分割の指定が**そのまま**届く。"""
    argv = measure_fit.child_argv(WINDOW, Path("/tmp/report.md"))
    pairs = dict(itertools.pairwise(argv))
    assert pairs["--from"] == "2026-09-08"
    assert pairs["--to"] == "2026-09-18"
    assert pairs["--eval-days"] == "2"
    assert pairs["--purge-days"] == "1"
    assert pairs["--report"] == "/tmp/report.md"
    assert argv[:3] == (sys.executable, "-m", "bikechance_ml.jobs.fit_lightgbm")


def test_the_mixed_weather_flag_is_only_added_when_asked() -> None:
    """**逃げ道は要るが、既定では渡さない**（09-07 は天気が 0% である）。"""
    assert "--allow-mixed-weather" not in measure_fit.child_argv(WINDOW, Path("r.md"))
    loose = measure_fit.Window(**{**vars(WINDOW), "allow_mixed_weather": True})
    assert "--allow-mixed-weather" in measure_fit.child_argv(loose, Path("r.md"))


# ── 計器 ──────────────────────────────────────────────────────
def test_the_proc_fields_are_read() -> None:
    assert measure_fit.field_kib(STATUS, "VmRSS") == 786_432
    assert measure_fit.field_kib(STATUS, "VmSwap") == 1_024


def test_a_missing_proc_field_is_zero_not_an_error() -> None:
    """**`VmSwap` は環境によって無い**（退避を切った機械）。落とさず 0 にする。"""
    assert measure_fit.field_kib("Name:\tpython3\n", "VmSwap") == 0


def test_a_similar_name_does_not_match() -> None:
    """**前方一致で拾わない。** `VmRSS` を探して `VmRSSHuge` を読むと桁が変わる。"""
    assert measure_fit.field_kib("VmRSSHuge:\t  999 kB\n", "VmRSS") == 0


def test_the_rusage_unit_differs_by_platform() -> None:
    """**ここが 168 の教訓そのもの。** Linux は KiB、macOS はバイトで返る。

    取り違えると **1,024 倍**ずれる——「14 GiB」が「14 MiB」に見える。
    """
    assert measure_fit.to_kib(1_048_576, from_bytes=False) == 1_048_576
    assert measure_fit.to_kib(1_073_741_824, from_bytes=True) == 1_048_576


def test_the_machine_size_is_zero_when_proc_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    """**macOS には `/proc` が無い。** 落とさず 0 にして、表に「0.00」と出す。"""
    monkeypatch.setattr(measure_fit, "_read_proc", lambda path: None)
    assert measure_fit.meminfo_kib("MemTotal") == 0
    assert measure_fit.read_status(1) is None


def test_a_child_that_already_left_is_not_an_error() -> None:
    """**競争は必ず起きる。** `poll()` を見てから `/proc` を読むまでに子が消える。

    そこで例外を出すと、**測り終える直前に測定そのものが落ちる**——いちばん
    惜しい壊れ方である。`/proc` が無い機械（macOS）も同じ道を通る。
    """
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert measure_fit.read_status(child.pid) is None


# ── 曲線 ──────────────────────────────────────────────────────
def _samples(values: list[int]) -> list[measure_fit.Sample]:
    return [measure_fit.Sample(float(index), one, 0) for index, one in enumerate(values)]


def test_the_curve_keeps_the_peak() -> None:
    """**間引きで峰を落とさない。** 山の時刻こそが次の梃子を決める。

    **山は等間隔の点から外して置く**（添字 4。`points=3` が拾うのは 0・3・6）。
    最初この仕込みは添字 3 に山を置いていて、**山を残す処理を消しても緑のまま**
    だった——「壊す仕掛けも壊れる」（§12 の 159・161）。
    """
    rows = _samples([1, 2, 3, 4, 99, 5, 6, 7, 8, 9])
    picked = measure_fit.curve(rows, points=3)
    assert max(one.rss_kib for one in picked) == 99
    assert [one.at_s for one in picked] == sorted(one.at_s for one in picked)


def test_a_short_curve_is_left_alone() -> None:
    rows = _samples([1, 2, 3])
    assert measure_fit.curve(rows, points=24) == tuple(rows)


def test_the_curve_stays_within_budget() -> None:
    """点数は `points` に山の 1 点を足したところに収まる。要約が 3,600 行にならない。"""
    rows = _samples(list(range(1000)))
    assert len(measure_fit.curve(rows, points=24)) <= 25


def test_the_progress_line_is_written_at_most_once_a_minute(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**間隔を守る。** 1 秒ごとに書くと 1 時間の走りが 3,600 行になる。"""
    told_at_s = measure_fit.tell(measure_fit.Sample(0.5, 1024, 0), 0.0)
    assert told_at_s == 0.0, "間隔より前に書いています"
    told_at_s = measure_fit.tell(measure_fit.Sample(61.0, 8 * measure_fit.KIB_PER_GIB, 0), 0.0)
    assert told_at_s == 61.0
    assert "8.00 GiB" in capsys.readouterr().err


def test_the_progress_line_is_the_only_evidence_when_killed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**殺されると要約は書けない。** 経過は標準エラーへ出す（記録に残る側）。"""
    measure_fit.tell(measure_fit.Sample(120.0, 15 * measure_fit.KIB_PER_GIB, 0), 0.0)
    caught = capsys.readouterr()
    assert caught.out == "", "経過を標準出力に混ぜると要約と紛れます"
    assert "15.00 GiB" in caught.err


# ── 報告書から件数を拾う ──────────────────────────────────────
def test_the_row_counts_come_from_the_report(tmp_path: Path) -> None:
    report = tmp_path / "fit.md"
    report.write_text("- **件数**：学習 16,607,174 行 / 検証 1,205,726 行\n", encoding="utf-8")
    assert measure_fit.read_rows(report) == (16_607_174, 1_205_726)


def test_no_report_means_no_rows(tmp_path: Path) -> None:
    """**落ちた当てはめは報告書を書かない。** 0 を捏造せず `None` を返す。"""
    assert measure_fit.read_rows(tmp_path / "missing.md") is None
    empty = tmp_path / "empty.md"
    empty.write_text("# 何も書いていない\n", encoding="utf-8")
    assert measure_fit.read_rows(empty) is None


# ── 判定 ──────────────────────────────────────────────────────
def _measured(peak_kib: int, exit_code: int = 0) -> measure_fit.Measured:
    return measure_fit.Measured(
        window=WINDOW,
        peak_kib=peak_kib,
        swap_peak_kib=0,
        seconds=60.0,
        exit_code=exit_code,
        samples=tuple(_samples([peak_kib // 2, peak_kib])),
        rows=(16_607_174, 1_205_726),
    )


def test_a_killed_child_is_reported_as_a_possible_oom() -> None:
    """**落ちたことも結果である。** 「失敗」で終わらせず、OOM を名指しする。"""
    assert "OOM" in measure_fit.verdict(_measured(15 * measure_fit.KIB_PER_GIB, -9), 16_000_000)


def test_a_thin_margin_is_not_called_a_pass() -> None:
    """**載っただけでは合格にしない。** 週次の自動ジョブに 10% 未満の余裕は薄い。"""
    total = 16 * measure_fit.KIB_PER_GIB
    assert "薄い" in measure_fit.verdict(_measured(int(total * 0.95)), total)
    assert measure_fit.verdict(_measured(int(total * 0.5)), total) == "**載った。**"


def test_a_failed_fit_shows_its_exit_code() -> None:
    assert "3" in measure_fit.verdict(_measured(1000, 3), 16_000_000)


# ── 出力 ──────────────────────────────────────────────────────
def test_the_summary_shows_the_peak_and_the_caveat() -> None:
    """**計器が違うことを必ず添える**（macOS の数と並べられない）。"""
    text = measure_fit.to_markdown(
        _measured(8 * measure_fit.KIB_PER_GIB), 16 * measure_fit.KIB_PER_GIB
    )
    assert "8.00 GiB" in text
    assert "8,589,934,592 バイト" in text, "**単位を取り違えられる形で出している**"
    assert "**←山**" in text
    assert "peak memory footprint" in text
    assert "16,607,174" in text


def test_the_json_keeps_every_sample() -> None:
    """**要約は間引くが、機械可読な側は間引かない。** 後から曲線を引き直せる。"""
    measured = _measured(4 * measure_fit.KIB_PER_GIB)
    body = json.loads(measure_fit.to_json(measured, 16_000_000))
    assert body["fit_rows"] == 16_607_174
    assert len(body["samples"]) == len(measured.samples)
    assert body["samples"][0] == [0.0, measured.samples[0].rss_kib, 0]


def test_the_summary_is_appended_only_when_actions_asks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    measure_fit.append_summary("捨てられる\n")
    where = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(where))
    measure_fit.append_summary("一度目\n")
    measure_fit.append_summary("二度目\n")
    assert where.read_text(encoding="utf-8") == "一度目\n二度目\n"


# ── 殻 ────────────────────────────────────────────────────────
def test_the_exit_code_follows_the_fit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**当てはめが落ちたらこちらも落ちる。** 緑のまま通すと気づけない。"""
    monkeypatch.setattr(measure_fit, "measure", lambda window, report, interval: _measured(10, -9))
    base = ["--from", "2026-09-08", "--to", "2026-09-18", "--report", str(tmp_path / "r.md")]
    assert measure_fit.run(base) == 1
    monkeypatch.setattr(measure_fit, "measure", lambda window, report, interval: _measured(10))
    where = tmp_path / "out.json"
    assert measure_fit.run([*base, "--json", str(where)]) == 0
    assert json.loads(where.read_text(encoding="utf-8"))["exit_code"] == 0


def test_the_module_runs_as_a_script() -> None:
    """**`python -m` で起きること。** `__main__` の番人を途中に置くと名前が足りない
    まま走り、検査は `import` するだけなので**全部緑のまま**になる（§12 の 170）。
    """
    done = subprocess.run(
        [sys.executable, "-m", "bikechance_ml.jobs.measure_fit", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "--report" in done.stdout
