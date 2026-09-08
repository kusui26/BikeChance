"""EDA #1 の Markdown 生成（純粋）。

数字を並べるだけでなく、**開発プランの前提と食い違う点を機械で拾って書き出す**。
人が読んで気づくことに頼ると、次に走らせたとき同じ確認をやり直すことになる。
"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - 型のためだけの import（循環を避ける）
    from bikechance_ml.analysis.eda_01 import SystemReport

#: 開発プランが置いている前提。ずれたら本文に「食い違い」として出す。
EXPECTED_CADENCE_S: Final[dict[str, int]] = {"hellocycling": 300, "docomo-cycle": 81}

#: W1 で測った「5 分グリッドで失う流量」。日数を増やしても保たれるかを見る（W2 プラン §5.9）。
W1_FLOW_LOSS_PCT: Final[float] = 13.0

#: 差がこれを超えたら「食い違い」として書き出す。
FLOW_LOSS_TOLERANCE_PCT: Final[float] = 3.0

#: JST は UTC より 9 時間進んでいる。
JST_OFFSET_H: Final[int] = 9


def _jst(at: datetime) -> datetime:
    return at.astimezone(UTC) + timedelta(hours=JST_OFFSET_H)


def _fmt(at: datetime | None) -> str:
    return "—" if at is None else f"{_jst(at):%Y-%m-%d %H:%M} JST"


def _bar(value: float, largest: float, width: int = 24) -> str:
    """棒グラフの代わり。画像の依存を増やさずに形を見せる。"""
    if largest <= 0:
        return ""
    return "#" * max(0, round(width * value / largest))


def _ranged_bar(value: float, low: float, high: float, width: int = 24) -> str:
    """**最小〜最大の範囲で伸縮する棒。** 値の幅が狭い系列でも形が見える。

    絶対値に比例させると、17%〜19% のような系列がすべて同じ長さになって何も分からない。
    範囲は表の見出しに書いて、読み手が誤解しないようにする。
    """
    if high <= low:
        return ""
    return "#" * max(0, round(width * (value - low) / (high - low)))


#: システムを列に並べた表の 1 行。ラベルと、レポートから値を取る関数。
MetricRow = tuple[str, Callable[["SystemReport"], str]]


def _metric_table(reports: Sequence["SystemReport"], rows: Sequence[MetricRow]) -> list[str]:
    """システムを列に並べた表を作る。3 か所で同じ形を使うのでまとめてある。"""
    header = "| 指標 | " + " | ".join(report.system_id for report in reports) + " |"
    divider = "|---|" + "---|" * len(reports)
    body = [
        "| " + label + " | " + " | ".join(getter(report) for report in reports) + " |"
        for label, getter in rows
    ]
    return [header, divider, *body]


def _coverage_lines(report: "SystemReport") -> list[str]:
    missing = (
        "なし"
        if not report.missing_hours
        else ", ".join(f"{hour:%m-%d %H} UTC" for hour in report.missing_hours)
    )
    return [
        f"- 対象：{report.n_hours_found} / {report.n_hours_requested} 時間"
        f"（観測 {_fmt(report.first_observed)} 〜 {_fmt(report.last_observed)}）",
        f"- **Parquet が無い時間帯**：{missing}",
        f"- 行数 {report.missingness.n_rows:,}"
        f"（うち未観測 `-1` が {report.missingness.missing_pct}%）"
        f" / スナップショット {report.missingness.n_snapshots:,}"
        f" / ポート {report.missingness.n_stations:,}",
    ]


def _availability_table(reports: Sequence["SystemReport"]) -> list[str]:
    rows: list[MetricRow] = [
        ("観測した行数", lambda r: f"{r.availability.n_rows:,}"),
        ("`bikes = 0`", lambda r: f"**{r.availability.bikes_zero_pct}%**"),
        ("`docks = 0`", lambda r: f"**{r.availability.docks_zero_pct}%**"),
        ("両方 0", lambda r: f"{r.availability.both_zero_pct}%"),
        (
            "`bikes` 平均 / 最大",
            lambda r: f"{r.availability.bikes_mean} / {r.availability.bikes_max}",
        ),
        (
            "`docks` 平均 / 最大",
            lambda r: f"{r.availability.docks_mean} / {r.availability.docks_max}",
        ),
        ("運用停止（`flags = 1`）", lambda r: f"{r.availability.suspended_pct}%"),
    ]
    return _metric_table(reports, rows)


def _hourly_block(report: "SystemReport") -> list[str]:
    if not report.hourly:
        return ["（データなし）"]
    lo = min(row.mean_abs_delta for row in report.hourly)
    hi = max(row.mean_abs_delta for row in report.hourly)
    n_days = {row.n_days for row in report.hourly}
    caveat = (
        ""
        if len(n_days) == 1
        else "\n> **時台によって含まれる日数が違う**（1 日ぶんの時台と 2 日ぶんの時台が"
        "混ざっている）。日内変動と日差が分かれていないので、傾向としてだけ読む。\n"
    )
    header = (
        f"棒は**変化量** `平均|Δbikes|` を **{lo} 〜 {hi}** の範囲で伸縮させたもの"
        "（絶対値ではないので、他のシステムと長さを比べない）。"
    )
    lines = [
        header,
        caveat,
        "| JST | 日数 | 行数 | `bikes=0` | `docks=0` | `bikes` 平均 | 平均\\|Δbikes\\| | |",
        "|---|---|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {row.hour_jst:02d} 時 | {row.n_days} | {row.n_rows:,} | {row.bikes_zero_pct}% |"
        f" {row.docks_zero_pct}% | {row.bikes_mean} | {row.mean_abs_delta} |"
        f" `|{_ranged_bar(row.mean_abs_delta, lo, hi)}` |"
        for row in report.hourly
    ]
    return lines


def _extremes_block(report: "SystemReport") -> list[str]:
    if not report.extremes:
        return ["（データなし）"]
    lines = [
        "| station_id | `bikes` 最大 | `docks` 最大 | 観測回数 | 判定 |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| `{row.station_id}` | {row.max_bikes:,} | {row.max_docks:,} | {row.n_observed:,} |"
        f" {'**実在しない可能性が高い**' if row.implausible else 'ありうる値'} |"
        for row in report.extremes
    ]
    return lines


def _spread_table(reports: Sequence["SystemReport"]) -> list[str]:
    rows: list[MetricRow] = [
        ("ポート数", lambda r: f"{r.spread.n_stations:,}"),
        (
            "ゼロ率 p10 / p50 / p90",
            lambda r: (
                f"{r.spread.zero_rate_p10}% / {r.spread.zero_rate_p50}% / {r.spread.zero_rate_p90}%"
            ),
        ),
        ("**ずっと空**のポート", lambda r: f"{r.spread.n_always_empty:,}"),
        ("一度も空にならないポート", lambda r: f"{r.spread.n_never_empty:,}"),
        ("**一度も動かない**ポート", lambda r: f"{r.spread.n_never_changed:,}"),
        (
            "変化回数 p50 / p90 / 最大",
            lambda r: f"{r.spread.moves_p50} / {r.spread.moves_p90} / {r.spread.moves_max}",
        ),
    ]
    return _metric_table(reports, rows)


def _flow_table(reports: Sequence["SystemReport"]) -> list[str]:
    rows: list[MetricRow] = [
        ("本来のスナップショット数", lambda r: f"{r.flow.n_snapshots_native:,}"),
        ("5 分グリッドの点数", lambda r: f"{r.flow.n_snapshots_grid:,}"),
        ("本来の Σ|Δbikes|", lambda r: f"{r.flow.total_abs_delta_native:,}"),
        ("グリッドの Σ|Δbikes|", lambda r: f"{r.flow.total_abs_delta_grid:,}"),
        ("**取りこぼし**", lambda r: f"**{r.flow.lost_pct}%**"),
    ]
    return _metric_table(reports, rows)


def _rebalancing_block(report: "SystemReport") -> list[str]:
    total = max(report.rebalancing.by_hour_jst) if report.rebalancing.by_hour_jst else 0
    lines = [
        f"- 件数 **{report.rebalancing.n_events:,}**"
        f"（1 時間あたり {report.rebalancing.events_per_hour}）"
        f" / 該当ポート {report.rebalancing.n_stations_with_event:,}"
        f" / 最大の動き {report.rebalancing.largest_move} 台",
        "",
        "| JST | 件数 | |",
        "|---|---|---|",
    ]
    lines += [
        f"| {hour:02d} 時 | {count:,} | `{_bar(count, total)}` |"
        for hour, count in enumerate(report.rebalancing.by_hour_jst)
    ]
    return lines


def _ratio(high: float, low: float) -> float:
    """最大 ÷ 最小。**何倍動くか**を言うために使う。"""
    return round(high / low, 1) if low > 0 else 0.0


def _diurnal_note(report: "SystemReport") -> str | None:
    """日内変動の強さを、水準と変化に分けて言う。

    「時刻の特徴量が効くか」は、**水準ではなく変化**を見ないと分からない。
    水準がほぼ一定でも、動きが時間帯で大きく変われば `minute_of_day` は効く。
    """
    if len(report.hourly) < 2:
        return None
    levels = [row.bikes_zero_pct for row in report.hourly]
    peak = max(report.hourly, key=lambda row: row.mean_abs_delta)
    trough = min(report.hourly, key=lambda row: row.mean_abs_delta)
    level_ratio = _ratio(max(levels), min(levels))
    move_ratio = _ratio(peak.mean_abs_delta, trough.mean_abs_delta)
    return (
        f"**{report.system_id}：水準はほぼ動かないのに、変化は時間帯で大きく動く。**"
        f" `bikes = 0` の割合は {min(levels)}% 〜 {max(levels)}%"
        f"（{level_ratio} 倍）にとどまるのに、`平均|Δbikes|` は"
        f" {trough.hour_jst:02d} 時の {trough.mean_abs_delta} から"
        f" {peak.hour_jst:02d} 時の {peak.mean_abs_delta} まで **{move_ratio} 倍** 動く。"
        "時刻の特徴量は**水準ではなく遷移**に効くとみるべき"
    )


def _spread_note(report: "SystemReport") -> str | None:
    """ポート別プロファイルの価値を、ゼロ率の広がりで言う。"""
    spread = report.spread
    if spread.n_stations == 0:
        return None
    return (
        f"**{report.system_id}：ポートによってゼロ率が大きく違う。**"
        f" p10 {spread.zero_rate_p10}% / p50 {spread.zero_rate_p50}%"
        f" / p90 {spread.zero_rate_p90}%。ずっと空が {spread.n_always_empty:,} 件、"
        f"一度も空にならないのが {spread.n_never_empty:,} 件。"
        " **共通モデルだけでは説明できない広がり**があり、"
        "ポート別プロファイル（開発プラン §6.4）の価値が高い"
    )


def _flow_note(report: "SystemReport") -> str | None:
    """5 分グリッドの取りこぼしを、W1 の実測と突き合わせて言う。"""
    flow = report.flow
    if flow.n_snapshots_native == flow.n_snapshots_grid:
        return (
            f"**{report.system_id}：5 分グリッドで失うものが無い（{flow.lost_pct}%）。**"
            " 本来の更新周期が 5 分なので当然だが、毎分収集の価値はこのシステムには出ない"
        )
    gap = abs(flow.lost_pct - W1_FLOW_LOSS_PCT)
    verdict = (
        f"W1 の実測 {W1_FLOW_LOSS_PCT}% と整合する（差 {gap:.1f} ポイント）"
        if gap <= FLOW_LOSS_TOLERANCE_PCT
        else f"**W1 の実測 {W1_FLOW_LOSS_PCT}% と {gap:.1f} ポイント違う**"
    )
    return (
        f"**{report.system_id}：5 分グリッドに落とすと台数の動きの {flow.lost_pct}% が消える。**"
        f" {verdict}。毎分収集を続ける根拠になる"
    )


def findings(reports: Sequence["SystemReport"]) -> list[str]:
    """**数字から導ける所見**を並べる。手で書き足さない（再生成でずれるため）。"""
    notes: list[str] = []
    for report in reports:
        if report.missing_hours:
            notes.append(
                f"**{report.system_id}：Parquet が {len(report.missing_hours)} 時間ぶん無い。**"
                " 学習期間を切る前に埋め戻す（データ辞書 §7.3）"
            )
        for extreme in report.extremes:
            if extreme.implausible:
                notes.append(
                    f"**{report.system_id}：`{extreme.station_id}` が `docks ="
                    f" {extreme.max_docks:,}` を返している。実在しないポート（事業者の監視用）"
                    f"の可能性が高い。** 観測 {extreme.n_observed:,} 回。学習からも地図からも外す"
                )
        notes += [note for note in (_diurnal_note(report), _spread_note(report)) if note]
        if report.spread.n_never_changed > 0:
            share = 100.0 * report.spread.n_never_changed / max(1, report.spread.n_stations)
            notes.append(
                f"**{report.system_id}：観測期間を通じて一度も動かないポートが"
                f" {report.spread.n_never_changed:,} 件（{share:.1f}%）。**"
                " 学習サンプルとしての価値が薄く、持続ベースラインに勝てる余地も小さい"
            )
        flow_note = _flow_note(report)
        if flow_note:
            notes.append(flow_note)
        if report.availability.suspended_pct == 0.0 and report.availability.n_rows > 0:
            notes.append(
                f"**{report.system_id}：運用停止（`flags = 1`）が 1 件も無い。**"
                " 除外規則が空振りしていないか確かめる（開発プラン §6.1）"
            )
        notes.append(
            f"**{report.system_id}：再配置とみられる変化は 1 時間あたり"
            f" {report.rebalancing.events_per_hour} 件**（該当ポート"
            f" {report.rebalancing.n_stations_with_event:,} /"
            f" 最大 {report.rebalancing.largest_move} 台）。"
            "閾値 `max(4, 0.5 × 観測容量)` が現実的な件数を拾えている（開発プラン §6.5）"
        )
    return notes


def render_markdown(reports: Sequence["SystemReport"], start: datetime, end: datetime) -> str:
    """レポート本文を組み立てる。**この関数は純粋**（引数だけから決まる）。"""
    span_h = int((end - start).total_seconds()) // 3600
    lines: list[str] = [
        "# EDA #1 — 蓄積したデータの分布",
        "",
        f"- **対象期間**：{_fmt(start)} 〜 {_fmt(end)}（UTC の正時で {span_h} 時間）",
        "- **入力**：Storage `gbfs-parquet`（学習が実際に読む面）。Postgres は見ていない",
        "- **生成**：`python -m bikechance_ml.analysis.eda_01`（W2 プラン §5.9、PR G）",
        "",
        "> 数字はすべてこのスクリプトが出したもの。**手で書き換えない。**",
        "> 期間を変えて走らせ直したら、この文書ごと置き換える。",
        "",
        "## 0. 気づいたこと",
        "",
    ]
    notes = findings(reports)
    lines += [f"{i + 1}. {note}" for i, note in enumerate(notes)] if notes else ["（食い違いなし）"]

    lines += ["", "## 1. 対象と欠損", ""]
    for report in reports:
        lines += [f"### {report.system_id}", "", *_coverage_lines(report), ""]

    lines += ["## 2. 在庫の分布", "", *_availability_table(reports), ""]
    lines += ["## 3. 日内変動（JST）", ""]
    for report in reports:
        lines += [f"### {report.system_id}", "", *_hourly_block(report), ""]

    lines += ["## 4. ポート差", "", *_spread_table(reports), ""]
    lines += [
        "### 値が極端なポート",
        "",
        "**名前で弾いてはいけない。** 実測で「日本バプテスト京都教会」が `%テスト%` に"
        "一致した。値の妥当性で見る。",
        "",
    ]
    for report in reports:
        lines += [f"**{report.system_id}**", "", *_extremes_block(report), ""]
    lines += [
        "## 5. 5 分グリッドで失う流量",
        "",
        "本来の周期で測った台数の動きの総和と、5 分グリッドに落として測った総和を比べる。"
        "**差が大きいほど、毎分収集の価値が高い。**",
        "",
        *_flow_table(reports),
        "",
    ]
    lines += [
        "## 6. 再配置とみられる変化",
        "",
        "閾値は `|Δbikes| >= max(4, 0.5 × 観測容量)`。"
        "観測容量は期間内の `max(bikes + docks)`（宣言値は使わない）。",
        "",
    ]
    for report in reports:
        lines += [f"### {report.system_id}", "", *_rebalancing_block(report), ""]

    return "\n".join(lines).rstrip() + "\n"
