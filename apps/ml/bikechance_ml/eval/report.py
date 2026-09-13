"""ベースラインの結果を Markdown にする（W3 プラン §5.9）。

**数字を並べるだけでなく、判定と食い違いを機械で書き出す**（`analysis/report.py` と
同じ方針）。人が読んで気づくことに頼ると、次に走らせたとき同じ確認をやり直すことになる。

自動で見るのは 4 つ。

  * **B1 の Brier が全水平で B0 以下か**（上回ったら除外規則の適用漏れを疑う。W3-15）
  * **B2 が B1 に落ちた割合**（高ければ B2 は実質 B1。W3-17）
  * **Brier < 0.001 のバケツ**（相対改善が数値ノイズになるので判定の対象外。W3-16）
  * **B3 が B1 に負けていないか**（負けていれば混合の当てはめを疑う）

**§2 は「何で測ったか」である**（PR I）。`feature_set` は列が在ることしか語らないので、
**天気が入っていた割合**を日ごとに出す（W4 プラン §8.5.3）。
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Final

from bikechance_ml.eval.harness import Outcome, SliceScores
from bikechance_ml.eval.metrics import skill
from bikechance_ml.eval.slices import ALL
from bikechance_ml.eval.split import DaySplit
from bikechance_ml.features.constants import FEATURE_SET
from bikechance_ml.features.coverage import MAX_SPREAD_PP, Coverage, restrict, spread_pp
from bikechance_ml.features.grid import JST
from bikechance_ml.features.schema import WEATHER_COLUMNS

#: 相対改善を判定に使ってよい Brier の下限（W3-16、§4.4 の 30a）。
JUDGEABLE_BRIER: Final[float] = 0.001

#: B2 の落とし方がこれを超えたら「実質 B1」と書く。
FALLBACK_WARN: Final[float] = 0.5

#: 信頼度図のずれを表す棒。1 目盛りが 0.01。
GAP_BAR_SCALE: Final[float] = 100.0
GAP_BAR_WIDTH: Final[int] = 12


def render_markdown(
    outcome: Outcome,
    title: str,
    note: str,
    generator: str = "evaluate_baselines",
    *,
    weather: Mapping[date, Coverage],
) -> str:
    """1 回の評価を Markdown にする。

    `generator` は**この表を作ったジョブ**。読む人が「どのコマンドで作り直せるか」を
    たどれるようにするためで、決め打ちにすると別のジョブが作った表が嘘をつく。

    `weather` は**必ず渡す**（既定値を置かない）。省けるようにすると、次に評価を足す
    人が省いて、また「同じ `v3` なのに中身が違う表」が出る（§8.5.3）。
    """
    lines = [
        f"# {title}",
        "",
        f"- **生成**：{datetime.now(UTC).astimezone(JST):%Y-%m-%d %H:%M} JST"
        f"（`bikechance_ml.jobs.{generator}`、`feature_set = {FEATURE_SET}`）",
        f"- **分割**：{outcome.split.describe()}",
        f"- **件数**：学習 {outcome.n_fit:,} 行 / 検証 {outcome.n_eval:,} 行",
        "",
        note,
        "",
        *_judgement(outcome),
        "",
        *_data_block(outcome, weather),
        "",
        *_overall_block(outcome),
        "",
        *_horizon_block(outcome),
        "",
        *_bucket_block(outcome),
        "",
        *_cut_block(
            outcome,
            outcome.by_dow_type,
            "6. 曜日種別別（重み付き Brier、水平はまとめる）",
            "**W5 の PR D の効果はここに出る。** 曜日種別ごとに気候値のセルが埋まる"
            "日が違うので（土は 09-19、日祝は 09-20 が 2 日目）、**種別で当たり方が"
            "違うのは正常**である。どの種別も同じ割合で 10 水平を持つので、種別どうしは"
            "比べられる。",
            "曜日種別",
        ),
        "",
        *_cut_block(
            outcome,
            outcome.by_time_of_day,
            "7. 時間帯別（重み付き Brier、到着時刻で切る）",
            "**切るのは `t + h`**（利用者が着く時刻）。`t` で切ると、朝に問い合わせて"
            "昼に着く行が「朝」に入る（開発プラン §7.3）。",
            "時間帯",
        ),
        "",
        *_calibration_block(outcome),
        "",
        *_reliability_block(outcome),
        "",
        *_weighting_block(outcome),
        "",
        *_fitting_block(outcome),
        "",
    ]
    return "\n".join(lines) + "\n"


# ── 判定 ──────────────────────────────────────────────────────
def _judgement(outcome: Outcome) -> list[str]:
    losses = _b1_losses(outcome)
    behind = _b3_behind(outcome)
    return [
        "## 1. 判定",
        "",
        "| 見るもの | 期待 | 結果 |",
        "|---|---|---|",
        _row(
            "全モデルの評価件数が一致（§4.4 の 30b）",
            "同じ行の上で測る",
            "○（同じ `evaluate` マスクを配っている）",
        ),
        _row(
            "**B1 の Brier が全水平で B0 以下**（W3-15）",
            "上回ったら除外規則の適用漏れ",
            "○" if not losses else f"**× {len(losses)} 件**：{', '.join(losses)}",
        ),
        _row(
            "B3 が B1 以下",
            "混合が参照表を下回らない",
            "○" if not behind else f"**× {len(behind)} 件**：{', '.join(behind)}",
        ),
        _row(
            "判定対象のバケツ（Brier ≥ 0.001。W3-16）",
            "相対改善が測れる範囲",
            f"{_judgeable(outcome)} / {len(outcome.by_bucket)} 件",
        ),
    ]


def _b1_losses(outcome: Outcome) -> list[str]:
    return [
        f"{one.slice.system}/{one.slice.target}/h={one.slice.horizon_label()}"
        for one in outcome.by_horizon
        if one.weighted["B1"].brier > one.weighted["B0"].brier
    ]


def _b3_behind(outcome: Outcome) -> list[str]:
    return [
        f"{one.slice.system}/{one.slice.target}/h={one.slice.horizon_label()}"
        for one in outcome.by_horizon
        if one.weighted["B3"].brier > one.weighted["B1"].brier
    ]


def _judgeable(outcome: Outcome) -> int:
    return sum(1 for one in outcome.by_bucket if one.weighted["B0"].brier >= JUDGEABLE_BRIER)


# ── データの素性 ──────────────────────────────────────────────
def _data_block(outcome: Outcome, weather: Mapping[date, Coverage]) -> list[str]:
    """**何で測ったか。** 数字の前に、その数字が載っているデータを出す。"""
    return [
        "## 2. データ（読んだ日と天気の被覆）",
        "",
        "**`feature_set` は「列が在る」しか語らない**（W4 プラン §8.5.3）。同じ `v3` でも、"
        "天気が 1 件も入っていない日と 99.98% 入っている日がある。",
        "",
        *weather_block(weather, outcome.split),
    ]


def weather_block(weather: Mapping[date, Coverage], split: DaySplit) -> list[str]:
    """読んだ日と天気の被覆の表。**モデルカードからも呼ぶ**（正を 2 つにしない）。"""
    return [
        _row("日", "役割", "行", "天気の被覆"),
        _rule(2, 2),
        *(
            _row(f"{day:%Y-%m-%d}", _role(day, split), f"{one.rows:,}", f"{one.ratio:.3%}")
            for day, one in sorted(weather.items())
        ),
        "",
        *_spread_lines(weather, split),
        *_uniform_lines(weather),
    ]


def _role(day: date, split: DaySplit) -> str:
    if day in split.fit:
        return "学習"
    return "検証" if day in split.evaluate else "パージ"


def _spread_lines(weather: Mapping[date, Coverage], split: DaySplit) -> list[str]:
    """**学習と検証の差だけを見る。** パージ日は読むが捨てる。"""
    spread = spread_pp(restrict(weather, split.used()).values())
    verdict = "混ざっている" if spread > MAX_SPREAD_PP else "揃っている"
    return [
        f"- **学習と検証の被覆の差**：{spread:.2f} ポイント（許容 {MAX_SPREAD_PP}。**{verdict}**）"
    ]


def _uniform_lines(weather: Mapping[date, Coverage]) -> list[str]:
    """**4 列が同じ行で欠けているか。** 揃っているときも書く（黙らない）。"""
    uneven = [day for day, one in sorted(weather.items()) if not one.is_uniform]
    if not uneven:
        return [f"- **{len(WEATHER_COLUMNS)} 列の欠けかた**：全日で一致"]
    days = "、".join(f"{day:%Y-%m-%d}" for day in uneven)
    return [f"- **{len(WEATHER_COLUMNS)} 列の欠けかた**：**{days} で列ごとに違う**"]


# ── 表 ────────────────────────────────────────────────────────
def _overall_block(outcome: Outcome) -> list[str]:
    return [
        "## 3. 全体（system × ターゲット、重み付き）",
        "",
        "**総合値は自明な行に支配される**（3 台以上ある行が過半）。見当をつけるための表で、"
        "合否は §4 のバケツ別で決める（W3-16）。",
        "",
        _header("| system | ターゲット | n | 陽性率", outcome.models, "| B3 の BSS |"),
        _rule(2, 2 + len(outcome.models) + 1),
        *[_overall_row(one, outcome.models) for one in outcome.overall],
    ]


def _header(prefix: str, models: Sequence[str], suffix: str) -> str:
    """表の見出し。**モデルの列は `outcome.models` から作る**（外から足せるので）。"""
    return f"{prefix} | " + " | ".join(models) + " " + suffix


def _rule(labels: int, numbers: int) -> str:
    """区切り行。**名前の列は左寄せ、数の列は右寄せ**（既存の表と同じ見え方にする）。"""
    return "|" + "|".join(["---"] * labels + ["---:"] * numbers) + "|"


def _overall_row(one: SliceScores, models: Sequence[str]) -> str:
    cells = [f"{one.weighted[name].brier:.5f}" for name in models]
    bss = skill(one.weighted["B3"].brier, one.weighted["B0"].brier)
    return _row(
        one.slice.system,
        one.slice.target,
        f"{one.n:,}",
        f"{one.positives:.4f}",
        *cells,
        "—" if bss is None else f"{bss:+.1%}",
    )


def _horizon_block(outcome: Outcome) -> list[str]:
    return [
        "## 4. 水平別（重み付き Brier）",
        "",
        _header(
            "| system | ターゲット | h（分） | n",
            outcome.models,
            "| B1 の改善 | B3 の改善 |",
        ),
        _rule(2, 2 + len(outcome.models) + 2),
        *[_horizon_row(one, outcome.models) for one in outcome.by_horizon],
    ]


def _horizon_row(one: SliceScores, models: Sequence[str]) -> str:
    base = one.weighted["B0"].brier
    return _row(
        one.slice.system,
        one.slice.target,
        one.slice.horizon_label(),
        f"{one.n:,}",
        *[f"{one.weighted[name].brier:.5f}" for name in models],
        _percent(skill(one.weighted["B1"].brier, base)),
        _percent(skill(one.weighted["B3"].brier, base)),
    )


def _bucket_block(outcome: Outcome) -> list[str]:
    """**合否はここで決める**（W3-16）。だから外から足したモデルも必ず並べる。

    比べる相手は 2 つある。`B0`（持続）からの改善は「そもそも学習する価値があるか」、
    **`B3` からの改善は「ベースラインを超えたか」**で、採用基準は後者である
    （開発プラン §7.1）。
    """
    compared = [one for one in outcome.models if one not in ("B2",)]
    extra = [one for one in outcome.models if one not in BASELINE_COLUMNS]
    lines = [
        "## 5. 台数バケツ別（重み付き Brier）",
        "",
        "**合否はここで決める。** `Brier < 0.001` の行は相対改善が数値ノイズになるので"
        "「判定対象外」と書く（W3-16、§4.4 の 30a）。",
        "",
        _header(
            "| system | ターゲット | h | バケツ | n | 陽性率",
            compared,
            "| B3 の改善" + "".join(f" | {name} の対 B3" for name in extra) + " |",
        ),
        _rule(4, 2 + len(compared) + 1 + len(extra)),
    ]
    lines.extend(_bucket_row(one, compared, extra) for one in outcome.by_bucket)
    return lines


#: ベースラインの列（`_bucket_block` が「外から足したもの」を見分けるのに使う）。
BASELINE_COLUMNS: Final[tuple[str, ...]] = ("B0", "B1", "B2", "B3")


def _bucket_row(one: SliceScores, compared: Sequence[str], extra: Sequence[str]) -> str:
    base = one.weighted["B0"].brier
    judgeable = base >= JUDGEABLE_BRIER
    reference = one.weighted["B3"].brier
    return _row(
        one.slice.system,
        one.slice.target,
        one.slice.horizon_label(),
        one.slice.bucket_label(),
        f"{one.n:,}",
        f"{one.positives:.4f}",
        *[f"{one.weighted[name].brier:.5f}" for name in compared],
        "判定対象外" if not judgeable else _percent(skill(reference, base)),
        *[
            "判定対象外" if not judgeable else _percent(skill(one.weighted[name].brier, reference))
            for name in extra
        ],
    )


def _cut_block(
    outcome: Outcome, parts: Sequence[SliceScores], title: str, note: str, label: str
) -> list[str]:
    """軸を 1 つ足したときの表。**`horizon` は使わず `cut` の札で並べる。**"""
    lines = [f"## {title}", "", note, ""]
    if not parts:
        return [*lines, "（この軸に入る行がありません）"]
    lines.extend(
        [
            _header(f"| system | ターゲット | {label} | n", outcome.models, "|"),
            _rule(3, 1 + len(outcome.models)),
        ]
    )
    lines.extend(
        _row(
            one.slice.system,
            one.slice.target,
            one.slice.cut or ALL,
            f"{one.n:,}",
            *[f"{one.weighted[name].brier:.5f}" for name in outcome.models],
        )
        for one in parts
    )
    return lines


def _calibration_block(outcome: Outcome) -> list[str]:
    """ECE を**2 つ並べる**（W5-07）。**合否は等頻度のほうで決める。**"""
    lines = [
        "## 8. キャリブレーション（ECE、重み付き）",
        "",
        "**「70% と言った日の 7 割で降る」からのずれ。** 合格基準は ECE < 0.03"
        "（開発プラン §7.1）。B0 は 0 か 1 しか出さないので、ECE は「外した割合」に等しい。",
        "",
        "**判定に使うのは等頻度（15 区間）のほう**である（開発プラン §7.3、W5-07）。"
        "等幅（20 区間）は **2026-09-13 より前の記録と比べるため**に並べてある——"
        "配信中の確率は等幅の 20 区間のうち 4 つにしか入らず、**66.7% が最上位に集まる**"
        "ので、**アプリの約束（85% 以上）がいちばん潰れる**（W5 プラン §2.3e）。",
        "",
        "### 8.1 等頻度 15 区間（**判定はこちら**）",
        "",
        _header("| system | ターゲット | h", outcome.models, "|"),
        _rule(2, 1 + len(outcome.models)),
    ]
    lines.extend(_ece_row(one, outcome.models, uniform=False) for one in outcome.by_horizon)
    lines.extend(
        [
            "",
            "### 8.2 等幅 20 区間（**過去の記録と比べるため**）",
            "",
            _header("| system | ターゲット | h", outcome.models, "|"),
            _rule(2, 1 + len(outcome.models)),
        ]
    )
    lines.extend(_ece_row(one, outcome.models, uniform=True) for one in outcome.by_horizon)
    return lines


def _ece_row(one: SliceScores, models: Sequence[str], *, uniform: bool) -> str:
    return _row(
        one.slice.system,
        one.slice.target,
        one.slice.horizon_label(),
        *[
            f"{(one.weighted[name].ece_uniform if uniform else one.weighted[name].ece):.5f}"
            for name in models
        ],
    )


def _reliability_block(outcome: Outcome) -> list[str]:
    """信頼度図（開発プラン §7.1）。**画像を作らず、数と棒で見せる。**"""
    lines = [
        "## 9. 信頼度図（B3、重み付き、system × ターゲットの総合）",
        "",
        "**「p と言った行のうち、実際に何割が 1 だったか」。** 対角線に乗っていれば"
        "確率として正しい。**区間は等幅 20 等分**（図として読むため）で、行の無い区間は"
        "出さない。**判定に使う ECE は §8.1 の等頻度**のほうで、区切りが違う。",
    ]
    for part in outcome.overall:
        lines.extend(
            [
                "",
                f"### {part.slice.system} / {part.slice.target}",
                "",
                "| 予測の区間 | n | 予測の平均 | 実測 | ずれ |",
                "|---|---:|---:|---:|---|",
            ]
        )
        lines.extend(
            _row(
                f"{one.low:.2f} 〜 {one.high:.2f}",
                f"{one.n:,}",
                f"{one.mean_predicted:.4f}",
                f"{one.mean_observed:.4f}",
                _gap_bar(one.mean_observed - one.mean_predicted),
            )
            for one in part.weighted["B3"].bins
        )
    return lines


def _gap_bar(gap: float) -> str:
    """ずれを目で見える形に。**符号を残す**（過信か過小かが分かる）。"""
    marks = min(GAP_BAR_WIDTH, round(abs(gap) * GAP_BAR_SCALE))
    return f"{gap:+.4f} " + ("低" if gap < 0 else "高") * marks


def _weighting_block(outcome: Outcome) -> list[str]:
    """重み付きと重み無しの比較（§7.7）。**差が説明できることを確かめる。**"""
    return [
        "## 10. 重み付きと重み無し",
        "",
        "**重み付きが主**（開発プラン §6.2）。難所（`bikes <= 2` または `docks <= 2`）を "
        "4 倍濃く抽出しているので、**重み無しの Brier は難所に引かれて大きく出る**。"
        "差がこの向きでなければ、抽出か重みを疑う。",
        "",
        "| system | ターゲット | 陽性率（重み付き / 無し） | B0 | B1 | B3 |",
        "|---|---|---|---|---|---|",
        *[_weighting_row(one) for one in outcome.overall],
    ]


def _weighting_row(one: SliceScores) -> str:
    return _row(
        one.slice.system,
        one.slice.target,
        f"{one.weighted['B0'].positives:.4f} / {one.unweighted['B0'].positives:.4f}",
        *[
            f"{one.weighted[name].brier:.5f} / {one.unweighted[name].brier:.5f}"
            for name in ("B0", "B1", "B3")
        ],
    )


def _fitting_block(outcome: Outcome) -> list[str]:
    lines = [
        "## 11. 当てはめの中身",
        "",
        # **気候値をどこから作ったかを書く。** 同じ日でも作り方で B2 の中身が変わる
        f"気候値（B2）の作り方：**{outcome.climate or '学習サンプル（features/）'}**",
        "",
        "| ターゲット | B1 のセル | B1 に無かった行 | B2 のセル "
        "| **B2 が B1 に落ちた割合** | 混合に使った行 | B3 の係数 |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    lines.extend(
        _row(
            one.target.name,
            f"{one.b1_cells:,}",
            f"{one.b1_missing:,}",
            f"{one.b2_cells:,}",
            f"{one.b2_fallback_ratio:.1%}",
            f"{one.n_blend:,}",
            one.coefficients.describe(),
        )
        for one in outcome.fits
    )
    worst = max((one.b2_fallback_ratio for one in outcome.fits), default=0.0)
    if worst > FALLBACK_WARN:
        lines.extend(
            [
                "",
                f"> **B2 は実質 B1 である。** 検証期間の {worst:.1%} の行が、"
                "学習期間にサンプルの足りないセルを引いて B1 に落ちている。"
                "**蓄積日数が足りない**（W3-17）。日数が増えるまで、"
                "B2 と B3 の数字は「配線が通っている」以上の意味を持たない。",
            ]
        )
    return lines


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value:+.1%}"


def _row(*cells: str) -> str:
    return "| " + " | ".join(cells) + " |"
