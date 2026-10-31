# 当てはめ直しの記録

**月曜の朝の当てはめ直し（`.github/workflows/weekly-refit.yml`、`bikechance_ml.jobs.weekly_refit`）が、1 回ごとに書く記録の置き場所**（W8 プランの W8-03、開発プランの D-45）。記録は自動化が開く PR に入り、**PR が通知と昇格の確認の場所になる**。

## 1 回で入るもの

| ファイル | 中身 |
|---|---|
| `docs/refits/<最終学習日>.md` | その回の記録（下の 7 節）。**B3 の登録簿の `card_path` はここを指す** |
| `docs/refits/<最終学習日>-promote.sql` | 昇格の SQL（`promote.sql.template` に版の名前を差し込んだもの） |
| `docs/model_cards/<合成器の版>.md` | 合成器のモデルカード（`build_composite` の `render_card`） |

**記録の 7 節**：①前提の門（日曜の学習サンプル・プロファイル・日次評価の `ok` の時刻）、②B3（版・学習日・置いたバイト列と手元が同じか・点検の出力）、③評価（検証日を日曜と土曜にした 2 つ。短い水平で B3 が B1 以下の組と最悪の組、日曜の重み付き Brier）、④削るセル（比べた日・森のセル・森が悪いセル・森の改善・削るセル。W6-12）、⑤合成器（版・大きさ・SHA-256・境目の差・点検の出力）、⑥試し打ち（両系統 2 回の所要・CPU・`rss_mb`・予測の数）、⑦昇格の手順。

## 昇格（W8-04。人が確認してから）

1. PR の記録を読む。目安は #5（W8 プラン §8 の 10/26）：点検が合格、評価の BSS が +10% 前後、森が悪いセルが少ない、試し打ちの `rss_mb` が 1.6 GB 未満
2. **確認を取ってから**、推論の合間（毎 5 分の 2〜3 分。ドコモは 1 分・HELLO は 4 分に始まる）に昇格の SQL を流す：

   ```sh
   psql "$SUPABASE_DB_URL" -X -P pager=off -f docs/refits/<最終学習日>-promote.sql
   ```

   合成器を active に、B3 を shadow にする 2 行（前の active・shadow は自動で retired になる。0038）。**B3 だけの週なので合成器の shadow は飛ばす**（D-44）。B3 の作り方を変えた週はこの形で上げない（D-46：合成器ごと shadow 3 日）
3. 昇格の後の 2 周期（`inference_log` が `ok`・`gated`、予測ログと shadow の予測ログ）と、鮮度の見張り（次の周期から新しい最終学習日）を見て、**PR をマージする**
4. 戻すなら、記録に書いた前の合成器を active に（確認を取ってから）

**昇格を呼ぶ文はこの `docs/refits/` にだけ置く**。CI がコード・ワークフロー・スクリプトの中のそれを止める（ci.yml の「昇格と退役が自動化されていないか」。契約 22・39）。ひな形を直すときは `promote.sql.template` を直す。

## 止まったとき（W8-05）

`job_runs` の `weekly_refit` に `failed` と `detail.phase`（止まった段）が残り、`monitor_jobs` の `job_failed:weekly_refit` が鳴る。段は `gate`・`fit_b3`・`verify`・`inspect_b3`・`evaluate`・`register_b3`・`serving`・`drop`・`compose`・`inspect_composite`・`dry_run`・`record`。

- **`gate`**：日次の枠が 3 時間待っても揃わなかった（`detail.missing`）。揃ってから手で起こす（`trial` を外す）。**門で待つ間は行を作らない**——諦めた時刻に行を作るので、見張りの「直近 3 時間の失敗」に入る
- **それより後**：記録（ワークフローの要約）と `detail.error` を読み、直してから手で起こす。B3 と合成器は candidate なので、同じ日の名前で置き直せる（active・shadow の名前は置けない。契約 38）

## 試しの形（手で起こしたときの既定）

置かず・登録せず・`job_runs` にも残さず、PR も開かない。合成器は手元に集めた部品で組み、**試し打ちは今の shadow で叩く**（候補は登録していないので本番の口で引けない）。記録はワークフローの要約にだけ出る。
