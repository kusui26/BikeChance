# 手順書：天気を生アーカイブから戻す

- **目的**：`weather_hourly`（保持 **30 日**）から消えた発行を、生アーカイブ `weather-raw`（**無期限**）から入れ直す。学習サンプルを過去にさかのぼって作り直すときに要る。
- **いつ読むか**：①30 日より前の日の `features/` を作り直したいとき、②`v_weather_pending` に古い発行が溜まっているとき、③**期限が来る前の点検**（下の「期限」）。
- **最終確認**：**2026-09-12**（W4 の PR L）。手順は本番で通してある。

---

## 1. 期限 — 確かめるなら「消える前」

**消えたあとでは確かめられない。** 突き合わせる相手（表に入っている行）が無くなるからである。

| | |
|---|---|
| `weather_hourly` の保持 | **30 日**（`run_maintenance` が `issued_hour < now() - 30 days` を消す） |
| いま残っているいちばん古い発行 | **2026-09-07 15:00 UTC**（アーカイブの 1 件目） |
| それが消える日 | **2026-10-08 00:00 JST**（＝ 10-07 15:00 UTC） |

**次の点検はこの日より前に。** 確認は 4 秒で終わる（下の §2）。

---

## 2. 確かめる（読むだけ・4 秒）

**書かない。** 生アーカイブを読み、行に組み立て、表に入っているものと突き合わせるだけである。

```bash
set -a; . ./.env; set +a
cd apps/ml
./.venv/bin/python -m bikechance_ml.jobs.verify_weather_archive
```

発行を指定しなければ、**いちばん古い発行**（次に消えるもの）を見る。特定の発行なら：

```bash
./.venv/bin/python -m bikechance_ml.jobs.verify_weather_archive \
  --issued-hour 2026-09-08T00:00:00Z
```

通ったときの出力（**2026-09-12 の実測**）：

```json
{
  "ok": true,
  "issued_hour": "2026-09-07T15:00:00+00:00",
  "batches": 6,
  "cells_archive": 595,
  "cells_table": 595,
  "values_compared": 19040,
  "n_mismatches": 0,
  "only_in_archive": [],
  "only_in_table": []
}
```

**読み方**

| 欄 | 意味 | 異常のとき |
|---|---|---|
| `batches` | 生アーカイブに在った分割の数 | **6 未満なら分割が欠けている**（`weather-raw` 側の欠落） |
| `cells_archive` / `cells_table` | 格子の数。**増えるのは正常**（ポートが増えると格子も増える。09-07 は 595、09-12 は 600） | 食い違えば `only_in_*` に出る |
| `n_mismatches` | 値の食い違い | **1 でも出たら戻せない**。`mismatches` に「どの格子の・どの系列の・何時間先か」が出る |

`ok` が偽なら終了コードは **1**。

> **`weather_code` まで比べている。** 特徴量が読むのは 3 系列（`precip_mm` / `temp_c` / `wind_kmh`）だが、比べる側で絞ると「比べていない列がずれていても一致した」と言ってしまう。

---

## 3. 戻す

**`load_weather` がそのまま使える。** 保持で行が消えると `v_weather_pending` がその発行を「未処理」として返すので、**下限を古く指定するだけ**でよい。

```bash
set -a; . ./.env; set +a
cd apps/ml
./.venv/bin/python -m bikechance_ml.jobs.load_weather \
  --since 2026-08-01 --max-issues 200
```

| 引数 | 意味 |
|---|---|
| `--since` | `available_at` の下限。**既定は 48 時間前**で、これは「消えた発行が毎時のジョブで蘇るのを防ぐ」ための下限である。戻すときだけ古くする |
| `--max-issues` | 1 回に入れる発行の数。**1 発行 = 6 ファイル・約 600 行**。1 日 24 発行なので、30 日ぶんなら 720 |

**冪等である。** 同じ発行を入れ直しても主キーで UPSERT するだけで、結果は変わらない。

**終わったら確かめる**（§2）。戻した発行を `--issued-hour` で指定する。

---

## 4. 戻せないもの

**取得そのものが落ちた発行は、生アーカイブにも無い。** Open-Meteo は過去の発行を返さないので、**永久に失われる**。

実例：**2026-09-10 17:00 JST（08:00 UTC）の発行**。6 分割のうち 1 つが `fetch/TypeError` で取れず、部分的な発行は取り込まない設計なので、表にもアーカイブにも無い（W4 プラン §8.5.6）。

この発行を指定すると、突き合わせる相手が無いので**そう言って止まる**。

```
NoIssueError: その発行が weather_hourly にありません: 2026-09-10T08:00:00+00:00
```

**影響は小さい。** 特徴量は `available_at <= t` の**最新の発行**を引くので、その時間帯は 1 つ前の発行を使う。NULL にはならず、**予報の齢が 1 時間ぶん古くなるだけ**である。

---

## 5. 触ってはいけないもの

**`job_runs` を刈らない。**

戻す入口は `v_weather_pending` → `v_weather_files` で、**`v_weather_files` は `job_runs` の `archive_weather` の記録から作られている**（0023）。ここを刈ると、**生アーカイブが無期限に在っても、どの発行が在るのかを DB が忘れる**。

`job_runs` は**いま誰も消していない**（`run_maintenance` は `feed_fetch_log`・`inference_log`・`weather_hourly`・`cron.job_run_details` だけを消す）。ただし行は **1 日 2,100 件**増える（2026-09-12 の実測。総数 11,210 件）ので、**刈りたくなる日は来る**。

そのときは：

- `archive_weather` の行を**残す**（ほかのジョブだけを刈る）、または
- `v_weather_files` の作り元を `job_runs` から別の表に移す

**壊したら落ちるようにしてある**：`supabase/tests/0016_weather_hourly.sql` が「400 日前の `archive_weather` の記録が `run_maintenance` で消えないこと」「保持を過ぎた発行が `v_weather_pending` に出ること」を確かめている。

---

## 6. 関連

| | |
|---|---|
| 検査の実装 | `apps/ml/bikechance_ml/jobs/verify_weather_archive.py`（`tests/test_verify_weather_archive.py` が 17 件） |
| 取り込み | `apps/ml/bikechance_ml/jobs/load_weather.py`（毎時 :25 UTC） |
| 取得 | `apps/web/lib/jobs/archive-weather.ts`（毎時 :17 UTC） |
| 保持 | `run_maintenance`（0037）。`weather_hourly` は 30 日 |
| 経緯 | W4 プラン §8.5.2（宿題）・§6.8 の PR L（実装と確認）・§8.5.6（失われた 1 発行） |
