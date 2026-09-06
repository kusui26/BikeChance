# BikeChance Week 1 実装プラン — 収集基盤の本番稼働

作成日：2026-09-05（土）　改訂：2026-09-06（日）v1.1　作成：Claude Code　対象期間：2026-09-07（月）〜 09-11（金）
上位文書：`docs/260904_dev_plan.md`（開発プラン v1.2）の §5「データ蓄積設計」と §13 の W1 行

---

## 0. この文書について

- **目的**：開発プラン §5.8 に箇条書きで書いた W1 の手順を、**そのまま実装に着手できる粒度**まで分解する。何を・どの順で・どこまでやったら完了か、どう検証するかを PR 単位で定義する。
- **PR 分割の方針**：1 つの PR は「1 つの関心事」「20〜40 分で読める分量」「単独で検証でき、マージしても本番が壊れない」を満たすこと。`main` へのマージは Vercel の本番デプロイを意味するため、**壊れた状態を main に置かない**ことを最優先にする。
- **参照の表記**：本文書内の章節は `§4.2` のように書く。上位文書を指す場合は必ず「開発プラン §5.3」のように前置きする。
- **この文書の読み方**：§4 で実測にもとづく設計判断を示し、§5 に v1.1 の検証で見つかった問題と修正を、§6 に 6 本の PR を定義する。実装時は §6 の各 PR の「完了条件」を満たしたら次に進む。§11 の付録に、複数の PR にまたがる契約（RPC のシグネチャ・配列の意味・`feed_state` の書き込み規律）を置いた。ここが PR B と PR C・D の合意点になる。
- **変更履歴**：v1.0（2026-09-05）初版。**v1.1（2026-09-06）前提から疑う検証を行い、27 件の問題を修正**（§5）。主な変更は、`stations` への毎分書き込みの廃止、`feed_state` の書き込み規律による取りこぼし防止、DEFAULT パーティション、`station_status_latest` の `is_present` と `last_changed_at`、`begin_fetch` による原子的な claim、PR D への 5 分毎カナリア Cron、エラー時の 500 応答、環境ファイルの使い分け。M0 を 9/9 から 9/10 に 1 日遅らせ、カナリア運転を挟んだ。
- **開発プラン本体との関係**：本文書は W1 の実装詳細のみを扱う。設計の根拠・代替案・決定記録は開発プランにある。実装中に設計判断が変わった場合は、**開発プランの該当章と §15 を先に更新**してから実装する（CLAUDE.md §2 の原則 10）。v1.1 で開発プランと食い違いが生じた箇所（§5 末尾）は PR A のマージ後に開発プラン側を追随させる。

## 1. Week 1 のゴールと完了条件

**ゴール：GBFS の収集を本番で連続稼働させ、学習に使えるデータが毎日貯まる状態にする。**

学習データは収集開始日より前には存在しない。W1 の遅延はそのまま W6 の LightGBM v1 の品質低下になるため、他のどの作業よりも優先する。

| マイルストーン | 目標日 | 完了条件 |
|---|---|---|
| **カナリア運転** | 9/9（水）午前〜 | PR D の 5 分毎 Cron で両システムのスナップショットが入り、Cron の到達性と取り込みの一通りを確認する（§7 の段 4〜5） |
| **M0 収集の本番稼働** | 9/10（木） | 毎分の Cron に切り替え、両システムのスナップショットが期待周期で `status_snapshots` に入り、対応する生 gzip JSON が Storage に存在する |
| **M1 24 時間 QA 合格** | 9/11（金） | 下表の合格基準をすべて満たす |

**M1 の合格基準（24 時間の実測で判定）**

| 指標 | 期待値 | 合格ライン |
|---|---|---|
| HELLO のスナップショット取得数 | 288 件/日（5 分周期） | **≥ 287 件**（取得率 99.5%） |
| ドコモのスナップショット取得数 | 約 1,080 件/日（80 秒周期） | **≥ 1,074 件**（同上） |
| 連続欠損の最大長 | 0 | **30 分未満** |
| 重複行（同一 `system_id, observed_at`） | 0 | **0**（Cron の二重配信が起きても 0） |
| Storage の生 JSON 件数 | スナップショット数と一致 | **完全一致** |
| DB サイズの増分 | 22.7 MB/日（実測ベース、§4.1 の (c)） | **≤ 40 MB/日** |
| 収集の Active CPU | 約 0.05 秒/回 | **≤ 0.3 秒/回**（月 1.5 CPU 時間以内） |
| 収集エンドポイントの失敗率 | 0% | **< 1%**（ODPT 側の一時障害を除く） |
| 配列と生 JSON の照合（§8.6） | 一致 | **各システム 3 スナップショットで不一致 0** |
| DEFAULT パーティションの行数 | 0 | **0** |

判定用の SQL と照合スクリプトは §8 にまとめた。

## 2. 現在の状態（2026-09-06 時点）

**完了済み**

| 項目 | 状態 |
|---|---|
| GitHub リポジトリ | `kusui26/BikeChance`（非公開、既定ブランチ `main`） |
| Vercel | Pro、プロジェクト作成済み、関数リージョン `hnd1`、本番 `https://bike-chance.vercel.app` |
| Vercel 環境変数（Production） | `ODPT_ACCESS_TOKEN` / `SUPABASE_URL` / `SUPABASE_SECRET_KEY` / `SUPABASE_DB_URL` / `CRON_SECRET` / `CONTACT_EMAIL` |
| Supabase | Pro、Tokyo、Micro、Data API 有効・新規テーブルの自動公開 OFF・自動 RLS ON |
| コード | モノレポ雛形（`apps/web` / `packages/shared`）、CI（lint・format・typecheck・test・build・機密チェック）、`GET /v1/meta` が本番で 200。PR #2 で本文書 v1.0 をマージ済み |
| ローカル `.env` | 11 変数を整備。ODPT トークン、Supabase 一式、`CRON_SECRET`、`CONTACT_EMAIL`、CLI 用の `SUPABASE_ACCESS_TOKEN`（Management API で疎通と権限を確認済み）と `SUPABASE_DB_PASSWORD` |

**未着手**

Supabase CLI のインストール、DB スキーマ、RPC、Storage バケット、Vault のシークレット登録、収集器、Cron 登録、監視ジョブ。

**ローカル環境の確認結果**：Docker 24.0.5 稼働中（`supabase start` が使える）、ディスク空き 24 GB、Supabase CLI 2.115.0 が Homebrew で入手可能、Vercel CLI 未インストール、`psql` 14.13 と `psycopg` 3.3.4 は導入済み。

**本番 Supabase の確認結果**：PostgreSQL 17.6、`public` スキーマは空、`pg_cron` 1.6.4・`pg_net` 0.20.4・`pg_partman` 5.3.1・`pgtap` 1.3.3 が利用可能、`supabase_vault` と `pgcrypto` は導入済み。

## 3. 事前準備

### 3.1 あなたの作業

**必須の作業は残っていません。** `SUPABASE_ACCESS_TOKEN` は貼り付け済みで、Management API に対して疎通と Projects 権限を確認しました（東京リージョンの BikeChance プロジェクトが `ACTIVE_HEALTHY` で見えています）。Database 権限は PR A の `supabase db push` で実地に確認します。

**任意だが強く推奨**：監視通知の送信先を用意してください。

- Discord のサーバーに専用チャンネルを作り、チャンネル設定 → 連携サービス → ウェブフックで URL を発行する（Slack の Incoming Webhook でも同じ）
- URL を `.env` の `ALERT_WEBHOOK_URL=` に貼り付ける
- 用途：フィード停滞・収集器の停止・DB 容量・日次 QA の通知（PR E）。無ければ通知は `alert_state` テーブルに記録されるだけになり、気づくのが遅れます

**参考：`SUPABASE_ACCESS_TOKEN` の権限**（発行時に選んだ設定の記録）

| カテゴリ | 設定 | 理由 |
|---|---|---|
| Projects / Database / Edge Functions / Secrets | Full access | `link` と `db push` が読み書きする。Edge Functions と Secrets は W3 のバックアップ収集器で使う |
| 上記以外 | No access | CLI から触らない。事故の影響範囲を狭める |

このトークンはアカウント全体に効く。保存先は `.env` だけで、Vercel や GitHub には登録しない。

### 3.2 私の作業（PR A の中で実施）

- Supabase CLI のインストール（`brew install supabase/tap/supabase`）
- `supabase init`、`supabase link --project-ref <ref>`
- ローカル Postgres の起動と `supabase db reset` による適用テスト
- Storage バケットの作成（SQL マイグレーションで実施。ダッシュボード操作は不要）
- Vault へのシークレット登録（`.env` を読むローカルスクリプトで実施。値をリポジトリに置かない。PR E）

## 4. W1 で確定させた設計判断

開発プラン §5 を実装に落とす過程で決めるべき論点を、**2026-09-05 に本番環境と実データで測った結果**にもとづいて確定させた。測定は開発機（Apple Silicon の MacBook Air、Node 22.22.3、Zod 4.5.4）と、本番の Supabase（PostgreSQL 17.6、Tokyo、Micro）および ODPT に対して行った。DB への書き込みはすべて一時テーブルで行い、ロールバックしている。

### 4.1 実測結果

**(a) GBFS フィードの処理コスト**（実フィードをそのまま処理）

| 処理 | HELLO CYCLING（4.0 MB / 14,861 ポート） | ドコモ（0.86 MB / 5,810 ポート） |
|---|---|---|
| `JSON.parse` | 22 ms | 5 ms |
| Zod で全件検証 | 42 ms | 26 ms |
| （参考）エンベロープのみ Zod ＋ 手書き型ガード | 6 ms | 2 ms |
| 配列の組み立て | 1 ms | 1 ms |
| RPC 用 JSON への直列化 | 5 ms | 4 ms |
| **合計（Zod 全件検証を採用）** | **69 ms** | **35 ms** |
| RPC に送るペイロード | 241 KB | 87 KB |

**(b) 取り込み SQL の実行時間**（本番 DB に実データで試作。3 文を個別に計測。ネットワーク往復を含む）

| 回 | 合計 | 内訳（登録 / 配列組み立て＋挿入 / 最新状態の更新） | 新規ポート | 変化行 |
|---|---|---|---|---|
| 初回（14,861 件すべて新規） | **652 ms** | 248 / 183 / 222 ms | 14,861 | 14,861 |
| 2 回目以降（定常） | **410〜450 ms** | 約 58 / 165 / 205 ms | 0 | 354〜416 |

RPC にまとめれば往復は 1 回になるため、実際はこれより速くなる。

**(c) スナップショット 1 行の実サイズ**（実データ、既定の pglz 圧縮）

| システム | 非圧縮 | 実サイズ | 1 日 | 60 日 |
|---|---|---|---|---|
| HELLO CYCLING | 116 KB | **32.0 KB** | 9.0 MB | 0.53 GB |
| ドコモ | 45 KB | **13.0 KB** | 13.7 MB | 0.80 GB |
| **合計** | — | — | **22.7 MB** | **1.33 GB** |

乱数データで lz4 と比較したところ pglz 58.8 KB に対し lz4 75.2 KB で、**pglz の方が小さかった**。書き込み量が 1 日 1,400 行と少ないため、圧縮速度より圧縮率を優先し既定のままにする。

**(d) プラットフォームの実測**

| 項目 | 結果 |
|---|---|
| Supabase Data API の本文サイズ | 176 KB・636 KB・**3,384 KB** のいずれも PostgREST まで到達（`PGRST202` が返る）。241 KB のペイロードは安全 |
| ODPT の条件付きリクエスト | 認証付き・公開の**両方**が `ETag` を返し、`If-None-Match` で **HTTP 304 / 0 バイト** |
| PostgreSQL | 17.6。`pg_cron` 1.6.4・`pg_net` 0.20.4・`pg_partman` 5.3.1・`pgtap` 1.3.3・`postgis` 3.3.7 が利用可能。`supabase_vault` と `pgcrypto` は導入済み |
| `statement_timeout` | `anon` 3 秒 / `authenticated` 8 秒 / `authenticator` 8 秒 / `service_role` は設定なし（`authenticator` の **8 秒を継承**）／`postgres` 2 分 |
| Deployment Protection | 本番ドメイン `bike-chance.vercel.app` は 200。生成デプロイ URL は Vercel SSO へ 302 |

### 4.2 決定事項

| ID | 論点 | 決定 | 根拠 |
|---|---|---|---|
| **W1-1** | 収集器から DB への接続方法 | **`supabase-js` の RPC（PostgREST 経由）** | ペイロード 241 KB に対し上限は 3.3 MB 以上。取り込み処理は定常 0.45 秒・初回 0.65 秒で、`service_role` の 8 秒制限に対し 12 倍以上の余裕がある。接続プールや SSL の管理が不要で、Storage でも同じクライアントを使う。将来 Python から直接接続でも同じ RPC を呼べる |
| **W1-2** | RPC の `statement_timeout` | **関数定義に `set statement_timeout to '30s'` を付ける** | PostgREST は関数の設定をトランザクションに持ち上げる（`db-hoisted-tx-settings` の既定に `statement_timeout` が含まれる）。ロールの 8 秒に縛られなくなる。ゲートウェイの上限 60 秒は超えられないため 30 秒とする |
| **W1-3** | GBFS の検証範囲 | **Zod で全件を検証する**。Zod v4 の `z.looseObject` を使う | 14,861 件で 42 ms。手書き型ガードとの差 36 ms は月 0.06 CPU 時間（約 $0.01）にすぎない。型安全と可読性を優先する |
| **W1-4** | 未更新時の取得 | **`ETag` による条件付きリクエスト**。`feed_state.last_etag` に保存し `If-None-Match` を付ける | HELLO は 1,440 回の取得のうち新規は 288 回。**ODPT からの転送量が 155 MB/日 → 31 MB/日（80% 削減）**。304 なら JSON パースも不要で CPU も減る。開発プラン R9「ODPT への過負荷」への直接的な対策 |
| **W1-5** | 同時実行の抑止 | **`pg_try_advisory_xact_lock(class_id, systems.lock_key)`**（トランザクション単位、キーは明示的な列） | Supavisor のトランザクションモードではセッション単位のロックが意図どおり効かない。`hashtext()` は文書化されていない内部関数なので使わない（§11.4） |
| **W1-6** | 取り込み RPC の分割 | **ポート登録・配列組み立て・挿入・最新値更新は `ingest_snapshot` 1 本に統合**する。v1.1 で、取得前の claim（`begin_fetch`）と取得後の記録（`finish_fetch`）を別の小さな RPC に分けた（W1-17、§11.3） | クライアントに `idx` の対応表を持たせない。サーバーレスではインスタンスが入れ替わるためキャッシュが当てにならない。登録と挿入が同一トランザクションになる。claim と記録を分けるのは、`feed_state` の列ごとに書く者を固定するため |
| **W1-7** | `status_snapshots.gap` | **列を作らない**。特徴量の段階で `station_attributes` の容量と結合して導出する | 容量は日次同期のデータで有効期間を持つ。収集の最短経路から容量参照を外せる（§11.2） |
| **W1-8** | パーティション運用 | **手書きの plpgsql 保守関数**（pg_partman を使わない） | pg_partman 5.3.1 は利用可能で Supabase も文書化しているが、必要なのは「先の月を作る」「古い月を落とす」の 2 つだけ。`part_config` という設定データを別途マイグレーションで再現する必要があり、`retention_keep_table` の既定が `true`（実際には削除されない）という罠もある。15 行の関数なら pgTAP で完全にテストできる。将来複雑になれば pg_partman に移行できる |
| **W1-9** | TOAST 圧縮 | **既定の pglz のまま**。`lz4` に変更しない | 実測で pglz の方が小さい（58.8 KB 対 75.2 KB）。書き込み量が少なく速度は問題にならない |
| **W1-10** | 予測・モデル系テーブル | **W1 のマイグレーションに含めない** | 使う予定のない空テーブルを先に作らない。W4 で必要になった時点で追加する |
| **W1-11** | 関数の背景実行 | **`after()` も `waitUntil()` も使わない。すべて `await` する** | Vercel は Cron の失敗を再試行しない。`after()` の中で失敗するとステータスコードは 200 のまま失敗が隠れる。処理は 1〜3 秒で、Fluid の既定 300 秒に対して余裕がある。加えて `after()` はリクエストスコープ外で例外になるため単体テストが書けなくなる |
| **W1-12** | 関数のリージョン指定 | **`vercel.json` のトップレベル `regions: ["hnd1"]`**。ルートの `preferredRegion` は使わない | Next.js 16 で `preferredRegion` は非推奨。Vercel 上で許される値は `auto` / `global` / `home` のみで `hnd1` を渡すと例外になる。Node ランタイムではルート単位のリージョン指定は無視される |

### 4.2b v1.1 で追加した決定

§5 の検証結果から導いた決定。W1-1〜W1-12 と同じく本文書の前提になる。

| ID | 論点 | 決定 | 根拠 |
|---|---|---|---|
| **W1-13** | 収集の最短経路での `stations` への書き込み | **登録以外は書かない**。`last_seen_at` と `is_active` は日次ジョブが直近 25 時間のスナップショットから計算する | 毎スナップショットで全ポートを UPDATE すると 1 日 430 万行の更新になり、Micro を圧迫する（§5 の 1） |
| **W1-14** | `feed_state` の書き込み規律 | `last_etag` と `last_observed_at` は **`ingest_snapshot` の成功時だけ**更新する。関数側は書かない | Storage 成功 → RPC 失敗 → 304 でスキップ、という静かな取りこぼしを構造的に防ぐ（§5 の 2） |
| **W1-15** | 月替わりの安全網 | **DEFAULT パーティション**を置き、非空を監視する | 保守ジョブが止まっても保存が続く。パーティション作成の DDL を毎分の経路に入れない（§5 の 3） |
| **W1-16** | `station_status_latest` の意味 | `last_changed_at`（最後に変化した時刻）と `is_present` を持つ。フィードの鮮度は `feed_state.last_observed_at` | 「最後に変化」と「最後に観測」を区別しないと鮮度表示が誤る（§5 の 4） |
| **W1-17** | 二重起動の抑止 | **`begin_fetch` RPC** の 1 文 UPDATE で claim し、同時に ETag と `last_observed_at` を返す | 読んでから書く方式は競合する。往復も 1 回で済む（§5 の 6） |
| **W1-18** | 異常なフィードの扱い | 出現ポート数が登録済みの 50% 未満なら保存はするが `is_present` の反転はしない | 空や大幅欠落のフィードで全ポートを「不在」にしない（§5 の 7） |
| **W1-19** | 本番稼働の段取り | PR D で **5 分毎のカナリア Cron**、PR E で毎分化 | Cron の到達性（最大のリスク）を 1 日早く、低い負荷で確認する（§5 の 8） |
| **W1-20** | エラー時の HTTP ステータス | **500** を返す（成功・未更新・スキップは 200） | Vercel Observability のエラー率検知を効かせる（§5 の 9） |

### 4.3 実装時に踏みやすい落とし穴

調査で判明した、放置すると必ず詰まる点。各 PR の受け入れ条件に組み込む。

| # | 落とし穴 | 対処 |
|---|---|---|
| 1 | **新規テーブルは `service_role` からも見えない**。「新規テーブルの自動公開」を OFF にすると、`anon`・`authenticated` だけでなく **`service_role` の権限も既定で剥奪**される。RLS を有効にしただけでは足りず、REST 呼び出しが `42501 permission denied` になる | マイグレーションで明示的に `grant select, insert, update, delete on table ... to service_role;` と `grant usage, select on all sequences in schema public to service_role;` を書く（PR A） |
| 2 | **RPC には明示的な `grant execute` が必要**で、DDL 後に PostgREST のスキーマキャッシュを更新しないと 404 になる | `grant execute on function ... to service_role;` と、マイグレーション末尾の `notify pgrst, 'reload schema';`（PR B） |
| 3 | `cron.job_run_details` は**自動削除されない**。毎分ジョブで月 4.3 万行たまる | 削除ジョブを同じマイグレーションで登録する（PR E） |
| 4 | Supabase では `postgres` ロールが `cron.job` に **SELECT しかできない**。`update cron.job set active=false` は失敗する | 有効・無効の切替は `cron.alter_job()`、削除は `cron.unschedule()` を使う（PR E） |
| 5 | pg_cron は **UTC 固定**（`cron.timezone` は変更不可）。`'0 3 * * *'` は 12:00 JST | 日次ジョブは UTC で書き、コメントに JST を併記する（PR E） |
| 6 | `cron.schedule('名前', ...)` は**同名ジョブを上書き**する（名前は大文字小文字を区別し変更不可） | ジョブ名を定数として管理し、マイグレーションの冪等性に利用する（PR E） |
| 7 | `net.http_post` の引数順は `url, body, params, headers, timeout_milliseconds` で `http_get` と異なる。既定タイムアウトは **2000 ms** | 必ず名前付き引数で呼び、`timeout_milliseconds := 10000` を明示する（PR E） |
| 8 | `pg_net` のリクエストは**トランザクションのコミット後**に送信される | SQL Editor で `begin ... rollback` して試しても何も送られない。検証は commit する（PR E） |
| 9 | Storage に `Buffer` を上げるとき `contentType` を省略すると **`text/plain;charset=UTF-8`** になる。REST 経由では `Content-Encoding` が無視される | `contentType: 'application/gzip'` を明示し、読み出し側で gunzip する（PR D） |
| 10 | Storage の同一パス上書きは CDN に古い内容が残る | パスに `observed_at` を含めて毎回別パスにする。既定の `upsert: false` のまま（PR D） |
| 11 | PostgREST の `max_rows` は 1000。集合を返す RPC は**警告なく切り詰められる** | RPC は `jsonb` のスカラを返す（PR B） |
| 12 | URL とヘッダの合計が 16 KB を超えると **HTTP 520**。413 のときは JSON でなく HTML が返る | 配列は必ず POST の本文に入れる。エラー処理では `Content-Type` を確認してから JSON パースする（PR D） |
| 13 | `statement_timeout` 超過は 504 ではなく **500（`57014`）** | 再試行の判定でステータスコードだけを見ない（PR D） |
| 14 | **Vercel Services の中では Middleware が使えない**（`middleware.ts` があるとデプロイが拒否される） | 認証はルートハンドラ内で行う。この制約を CLAUDE.md に追記する（PR A） |
| 15 | `supabase init` は `supabase/migrations/` を作らない | 最初の `supabase migration new` で作られる（PR A） |
| 16 | `supabase db push` は**タイムスタンプが前後するマイグレーションを拒否**する（内容ではなく版番号だけを比較） | PR を 1 本ずつマージする運用（CLAUDE.md §4）を守る |
| 17 | `supabase db diff` は **Storage バケットと `security_invoker` ビューを検出しない** | これらは手書きでマイグレーションに入れる（PR A） |

### 4.4 確定したコスト見積り

| 項目 | 1 日 | 1 か月 |
|---|---|---|
| Vercel の呼出回数 | 2,880 回 | 86,400 回（約 $0.05） |
| Vercel の Active CPU（ETag により 8 割が 304） | 約 104 秒 | 約 0.9 時間（約 $0.18） |
| Vercel の Provisioned Memory | — | 約 96 GB 時間（約 $1.6） |
| ODPT からの転送量 | 約 74 MB | 約 2.2 GB |
| Postgres の増加量 | 22.7 MB | 約 0.68 GB（60 日保持で上限 1.33 GB） |
| Storage の増加量（生 gzip JSON） | 約 67 MB | 約 2.0 GB |

開発プラン §4.5 の見積り（収集で月 $2.0、DB 30〜40 MB/日）に対し、**実測では月 $1.8・DB 22.7 MB/日**で、いずれも見積りの範囲内に収まった。

## 5. v1.1 の検証で見つかった問題と修正

v1.0 を「前提から疑う」姿勢で読み直し、実データと本番環境で確かめられるものは確かめた。見つかった問題を重大度順に示す。**重大**は「放置すると収集が止まる、またはデータを静かに失う」もの、**中**は「品質・運用に実害が出る」もの、**軽微**は「後で直すと高くつく」もの。すべて v1.1 の本文に反映済みで、右端の列が反映箇所。

| # | 重大度 | 問題 | 修正 | 反映 |
|---|---|---|---|---|
| 1 | **重大** | v1.0 の RPC は毎スナップショットで `stations.last_seen_at` を更新する設計だった。HELLO では **5 分毎に 14,861 行の UPDATE**（1 日 430 万行）になり、テーブルの膨張と WAL の増加で Micro インスタンスを圧迫する | 収集の最短経路では `stations` に**登録以外の書き込みをしない**。`last_seen_at` と `is_active` は日次ジョブが直近 25 時間のスナップショットの配列から計算する | §6 PR B・PR E、§11.3 |
| 2 | **重大** | **静かにデータを失う経路**があった。Storage への保存に成功した後に RPC が失敗すると、次の取得で ODPT が 304 を返し（ETag は変わっていない）、そのスナップショットは二度と DB に入らない | `feed_state.last_etag` と `last_observed_at` は **RPC が取り込みに成功したときだけ**更新する。関数側は絶対に書かない。これにより「最後に取り込みに成功した ETag」で条件付き要求を出すことになり、失敗分は次回に自然に再取得される | §6 PR D、§11.3、§11.4 |
| 3 | **重大** | 月替わりに該当月のパーティションが無いと INSERT が失敗し、保守ジョブが止まっていれば**収集が丸ごと止まる** | **DEFAULT パーティション**を置き、行が入ったら監視で検知する。保守ジョブは 2 か月先まで作る。これで最悪でも保存は続く | §6 PR A、PR E |
| 4 | **重大** | `station_status_latest` は変化行だけ更新するため、その `observed_at` は「最後に**変化**した時刻」であって「最後に観測した時刻」ではない。鮮度表示に使えず、フィードから消えたポートは古い値のまま「現在値」として残る | 列を **`last_changed_at`** に改名し、**`is_present`** を追加する。フィードの鮮度は `feed_state.last_observed_at` で表す。消えたポートは `is_present=false` になり、値は最後に観測したものを保持する | §6 PR A・PR B、§11.3 |
| 5 | 中 | `reported_age_s` の負値（ODPT 側の時計ずれ）を -1 に丸める仕様は、**欠損の印である -1 と衝突**する | 負値は **0** に丸める。-1 は欠損専用にする | §11.1 |
| 6 | 中 | Cron 二重起動の抑止が「`last_fetch_at` を読んで判断」だったため、同時に走る 2 つの関数が両方とも通過する | **`begin_fetch` RPC** で `update ... where last_fetch_at < now() - 30s returning` により**原子的に claim** する。同時に ETag と `last_observed_at` も返し、往復を 1 回にまとめる | §6 PR B・PR D、§11.3 |
| 7 | 中 | フィードが空や大幅欠落で返ってきたとき、登録済みポートの大半が「不在」に反転し、翌日の `is_active=false` 判定まで汚染する | **異常ガード**：出現ポート数が登録済みの 50% 未満なら、スナップショットは保存するが `is_present` の反転はせず、通知だけ出す | §6 PR B、§11.3 |
| 8 | 中 | Deployment Protection が Cron を弾く可能性（最大のリスク）を確かめるのが **M0 と同時**だった | **PR D に 5 分毎のカナリア Cron** を入れ、本番稼働の 1 日前に Cron の到達性と取り込みの一通りを確認する。PR E で毎分に切り替える | §6 PR D・PR E |
| 9 | 中 | エラー時も 200 を返す設計は、Vercel Observability の**エラー率による異常検知が効かない** | 成功・未更新・スキップは 200、**エラーは 500** を返す。本文に `ok` と `result` を入れる点は変えない | §11.6 |
| 10 | 中 | ドコモの status には **`station_information` に存在しないポートが 11 件**ある（実測）。属性を前提にした処理が例外になる | `ingest_snapshot` は status から登録するので取り込みは問題ない。PR F の同期と将来の API は「属性が無いポート」を前提に作る | §6 PR F、§11.1 |
| 11 | 中 | ドコモのポート ID は数分単位で出入りする。60 分で **122 件が一部の回にしか現れない**が、和集合は 1 回あたり +6 に収まる（実測） | `idx` は有限集合の中で振られるため設計は安全。`is_present` は生の真値として毎回反転させ、「休止」として見せる平滑化は W2 以降の API・特徴量側で行う | §11.1 |
| 12 | 中 | `maxDuration` を `vercel.json` の `functions` で設定する記述だったが、Next.js App Router ではルートの `export const maxDuration` が正式な方法 | ルートファイルの export に統一する。`vercel.json` には `regions` だけ置く | §6 PR A・PR D |
| 13 | 中 | Next.js は **`apps/web` 直下の `.env*` しか読まない**。リポジトリ直下の `.env` を前提にした `pnpm dev` の手順は動かない | 環境ファイルの使い分けを定義する（§11.8）。ローカル開発は `apps/web/.env.local`、CLI と本番検証はリポジトリ直下の `.env` | §11.8 |
| 14 | 軽微 | 生 JSON を「パース後に再直列化して gzip」すると ODPT の原文と一致しない | **受信したバイト列をそのまま gzip** する。再直列化しない | §6 PR D |
| 15 | 軽微 | Storage の同一パス重複（409）の扱いが未定義 | 409 は正常系として扱い、パスが同じなら内容も同じとみなす | §6 PR D |
| 16 | 軽微 | 重複 ID の除去で「どちらを残すか」が未定義 | **先頭を残す**。内容が異なる重複は件数を `feed_fetch_log.warnings` に記録する | §11.1 |
| 17 | 軽微 | `daily_quality.quality_date` の日付基準が未定義 | **JST の日付**にする。Storage のパスと Parquet のパーティションは UTC のままで、用途が違うことを明記する | §6 PR E、§11.5 |
| 18 | 軽微 | User-Agent が未指定。ODPT から見て誰の取得か分からない | `BikeChance/0.1 (+CONTACT_EMAIL)` を付ける。ODPT 利用規約の「著しい負荷」判定で連絡が取れる状態にする | §6 PR D |
| 19 | 軽微 | `feed_state` の初期行が無いと `begin_fetch` の claim が空振りする | seed で 2 行を入れる | §6 PR A |
| 20 | 軽微 | 監視の「同じ事象を繰り返し通知しない」に必要なテーブルが無い | `alert_state` を PR E のマイグレーションに追加する | §6 PR E |
| 21 | 軽微 | 第三のシークレット保管先として Vault に URL を入れていた | 本番 URL は秘密ではないので `app_config` テーブルに置く。Vault は `cron_secret` と Webhook URL だけにする | §6 PR E、§11.7 |
| 22 | 軽微 | 24 時間検証に「配列の値が生 JSON と一致する」確認が無かった | 生 JSON 1 件と DB の配列を突き合わせる照合スクリプトを QA に加える | §8.6 |
| 23 | **重大** | `systems` 等の参照データを `supabase/seed.sql` に置いていたが、**seed はローカルの `db reset` でしか実行されず `db push` は流さない**。本番の `feed_state` が空のままだと `begin_fetch` が claim できず収集が始まらない | 参照データは `on conflict do nothing` の冪等な INSERT としてマイグレーションに入れる。`seed.sql` はローカル専用（pgtap の有効化のみ） | §6 PR A |
| 24 | 中 | DEFAULT パーティションに行があると、Postgres は同じ範囲の新パーティション作成を**エラーにする**。v1.1 の初稿の復旧手順（親に insert して default から delete）も、その前の `create table` で失敗する | `ensure_snapshot_partitions` が DEFAULT の該当行を検出し、detach → 作成 → 移動 → attach を 1 トランザクションで行う。復旧手順もこの関数を呼ぶだけにする | §6 PR A、§9 |
| 25 | 軽微 | ウォッチドッグの到達を `feed_fetch_log` から見分ける手段が無かった | `source`（`cron` / `watchdog` / `manual`）列を追加し、ウォッチドッグは `?source=watchdog` を付けて叩く | §6 PR A・D・E、§11.6 |
| 26 | 軽微 | `last_updated` が後退したスナップショット（ODPT 側のキャッシュ戻り）の扱いが未定義。取り込むと `feed_state.last_observed_at` が巻き戻る | 関数側は「前回以下なら `unchanged`」として取り込まない。RPC 側も `last_observed_at` を前進させるだけにして二重に守る | §6 PR B・D、§11.3 |
| 27 | 軽微 | CI の SQL テストを既存ワークフローの `if` 式で絞る書き方は GitHub Actions に存在しない | `on.pull_request.paths` を使う別ワークフロー `sql-tests.yml` にする | §6 PR A |

**確かめて問題なしと判断したこと**

- `last_updated` は 60 分間・両システムで一度も後退しなかった。ただし将来の後退に備え、`station_status_latest` の更新は「新しい観測のときだけ」に限定する（§11.3）。
- ETag は認証付き・公開の両エンドポイントで**同一の値**を返した。フォールバックで切り替わっても 304 の最適化は失われない。
- HELLO は 60 分間、14,861 ポートが 1 件も出入りしなかった。
- 取り込み SQL は初回 0.65 秒・定常 0.45 秒で、`statement_timeout` を 30 秒にすれば 60 倍以上の余裕がある。
- pgTAP 1.3.3 が利用可能で、Supabase CLI の `supabase test db` で実行できる。

**上位文書（開発プラン）への提案**

- **D-04 の再考**：認証付きエンドポイントを正にすると、トークンが URL のクエリに載る。エラーメッセージや URL のログに紛れて漏れる経路が常に存在する。内容と ETag は公開エンドポイントと完全に同一で、User-Agent に連絡先を入れれば ODPT から見た識別性も保てる。**公開を正・認証付きをフォールバック**に入れ替えることを提案する。W1 の実装はどちらでも 1 行の順序差なので、判断はマージ前で構わない。
- 開発プラン §5.3 の DDL は本文書の §11 と食い違う箇所がある（`gap` 列、`station_status_latest` の列名、`feed_state` の列、`systems.lock_key`）。PR A のマージ後に開発プランを追随させる。

## 6. PR の分割と各 PR の定義

### 6.1 分割の方針

- 1 つの PR は **1 つの関心事**に閉じ、レビューが 20〜60 分で終わる量にする（目安：差分 300〜600 行。PR A はスキーマなので最も大きい）
- **本番に影響する変更を後ろに寄せる**。PR A〜C はマージしても本番の挙動が変わらない。PR D で 5 分毎のカナリア、PR E で毎分の本番稼働
- **各 PR はそれ自体で CI を通し、テストを含む**。「テストは次の PR で」は認めない
- 依存は一直線（A → B → C → D → E → F）。ただし C は A・B と独立に着手できる

| PR | 名前 | 主な成果物 | 本番影響 | 目安 |
|---|---|---|---|---|
| **A** | スキーマ v1 と Supabase 連携 | `supabase/` 一式、5 本のマイグレーション、seed、pgTAP、CI の SQL テスト、`regions` | なし（空のテーブルができるだけ） | 半日 |
| **B** | 取り込み RPC | `begin_fetch` / `ingest_snapshot` / `finish_fetch` と pgTAP | なし（呼び出し元がまだない） | 半日 |
| **C** | `packages/gbfs-core` | Zod スキーマ、正規化、配列組み立て、フィクスチャ、単体テスト | なし | 半日 |
| **D** | 収集エンドポイント＋カナリア Cron | `/api/jobs/collect/[system]`、Storage 保存、5 分毎の Cron、照合スクリプト | **小**（5 分毎に本番へ書く。Cron の無効化で即停止） | 半日〜1 日 |
| **E** | 本番稼働（毎分化・ウォッチドッグ・監視） | `crons` 毎分化、pg_cron ジョブ群、Vault、`alert_state`、日次ジョブ | **大**（M0） | 半日〜1 日 |
| **F** | ポート属性の日次同期 | `/api/jobs/sync-stations/[system]`、SCD2 の RPC、Cron | 小 | 半日 |

**PR 共通の作法**（CLAUDE.md §4 に従う）

- ブランチ名は `feat/w1-a-schema` のように PR の識別子を含める。実装開始前に `git status` と `git branch` で現在地を確認する
- コミット・push・PR 作成の前に必ず確認を取る。`git add` はファイルを個別に指定する
- PR の説明に「完了条件の確認結果」を箇条書きで載せる（テストの実行結果、実データでの計測値）
- マージは常にあなたの判断。main へのマージは本番デプロイになる

### 6.2 PR A：スキーマ v1 と Supabase 連携

**目的**：W1 で使うテーブル・パーティション・RLS・Storage バケットをマイグレーションとして定義し、ローカルと本番の両方に適用できる状態にする。

**前提**：Supabase CLI をインストールし、プロジェクトに link する。

```bash
brew install supabase/tap/supabase
supabase init                      # supabase/config.toml を生成（migrations/ は自分で作る）
supabase link --project-ref "$SUPABASE_PROJECT_REF"   # .env の SUPABASE_ACCESS_TOKEN / SUPABASE_DB_PASSWORD を使う
supabase start                     # ローカルスタック（Postgres 17・Storage・PostgREST）
```

**変更ファイル**

```
supabase/
  config.toml
  migrations/
    <ts>_0001_extensions.sql          # pg_cron, pg_net（pgcrypto は既存）
    <ts>_0002_core_tables.sql         # 下表のテーブル
    <ts>_0003_reference_data.sql      # systems 2 行、feed_state 2 行、app_config 1 行（on conflict do nothing）
    <ts>_0004_partitions.sql          # 保守関数 + 初期パーティション + DEFAULT
    <ts>_0005_rls_and_grants.sql      # 全テーブル RLS 有効、service_role に明示 grant
    <ts>_0006_storage_buckets.sql     # gbfs-raw（storage スキーマが存在する環境のみ）
  seed.sql                            # ローカル専用：pgtap 拡張の有効化のみ（本番には流れない）
  tests/
    0001_tables.sql                   # pgTAP：テーブル・列・制約・参照データ
    0002_partitions.sql               # pgTAP：パーティション関数と DEFAULT
    0003_grants.sql                   # pgTAP：RLS と権限
vercel.json                           # regions: ["hnd1"] を追加
.github/workflows/sql-tests.yml       # 新規ワークフロー。on.pull_request.paths を supabase/** に限定
.claude/CLAUDE.md                     # 「Services 内で middleware.ts を使わない」を §3 に追記
package.json                          # scripts: db:start / db:reset / db:test / db:push
```

**テーブル定義（W1 で作るもの）**

| テーブル | 要点 |
|---|---|
| `systems` | `system_id text pk`、`name`、`operator_name`、`gbfs_base_url`、`expected_cadence_s int`（300 / 80）、**`lock_key smallint not null unique`**（1 / 2）、`is_active` |
| `stations` | `(system_id, station_id) pk`、**`idx int not null`、`unique (system_id, idx)`**、`first_seen_at`、`last_seen_at`（**日次ジョブが書く**）、`is_active`、`pref_code` / `muni_code`（W3 まで NULL） |
| `station_attributes` | SCD2。`(system_id, station_id, valid_from) pk`、`valid_to null`、`name`、`lat`、`lon`、`capacity`、`raw jsonb`。属性が無いポートがあることを前提にする |
| `status_snapshots` | `(system_id, observed_at) pk`、`fetched_at`、`n_stations`、**`is_anomalous boolean not null default false`**、`bikes` / `docks` / `flags` / `reported_age_s smallint[]`、`raw_path text not null`。**月次 RANGE パーティション ＋ `status_snapshots_default`** |
| `station_status_latest` | `(system_id, station_id) pk`、`bikes`、`docks`、`flags`、**`is_present boolean not null`**、**`last_changed_at timestamptz not null`** |
| `feed_state` | `system_id pk`、`last_fetch_at`、`last_success_at`、`last_observed_at`、`last_etag`、`consecutive_errors int not null default 0` |
| `feed_fetch_log` | `id bigint generated always as identity`、`system_id`、`fetched_at`、**`source`（`cron` / `watchdog` / `manual`）**、`endpoint`（`token` / `public`）、`http_status`、`result`、`ok boolean`、`bytes`、`duration_ms`、`n_stations`、`error text`、`warnings jsonb`。30 日で削除 |
| `job_runs` | `job_name`、`started_at`、`finished_at`、`status`、`detail jsonb` |
| `daily_quality` | `(system_id, quality_date) pk`（**JST の日付**）、`n_snapshots`、`n_expected`、`max_gap_s`、`n_errors`、`n_anomalous`、`db_bytes_delta` |
| `app_config` | `key text pk`、`value text`、`updated_at`。W1 では `project_base_url` のみ |

`status_snapshots` の列には後で `n_present`（`-1` でない要素数）を追加する可能性があるが、`n_stations` がその意味なので W1 では作らない。

**参照データはマイグレーションに入れる**。`supabase/seed.sql` は `supabase db reset`（ローカル）でしか実行されず、`supabase db push` は既定で seed を流さない。`systems`・`feed_state`・`app_config` の初期行が本番に無いと `begin_fetch` が claim できないため、`on conflict do nothing` の冪等な INSERT をマイグレーションとして書く。`seed.sql` にはローカル専用の `create extension if not exists pgtap with schema extensions;` だけを置く。

**パーティション**

```sql
create table public.status_snapshots (...) partition by range (observed_at);
create table public.status_snapshots_default partition of public.status_snapshots default;

create function public.ensure_snapshot_partitions(p_months_ahead int default 2) returns int ...
-- 今月〜 p_months_ahead か月先まで、無ければ作る。名前は status_snapshots_y2026m09。戻り値は作成数
create function public.drop_expired_snapshot_partitions(p_keep_days int default 60) returns int ...
-- 上限が now() - p_keep_days より前のパーティションを drop table する（DEFAULT は対象外）
```

初期マイグレーションで `ensure_snapshot_partitions(2)` を呼び、今月・翌月・翌々月を作る。DEFAULT は保守ジョブが止まったときの受け皿で、**行が入ったら監視で気づく**（PR E）。パーティションの drop は `detach concurrently` を使わない。plpgsql 関数の中では使えず、深夜の数ミリ秒の排他ロックは実害がないため。

**DEFAULT に行があるときの新規パーティション作成**：Postgres は、新しい範囲に該当する行が DEFAULT に存在すると `create table ... partition of ... for values` を**エラーにする**。したがって `ensure_snapshot_partitions` は、作ろうとする範囲の行が DEFAULT にあるかを先に調べ、あれば同一トランザクション内で「DEFAULT を detach → 新パーティションを作成 → 該当行を親に insert し DEFAULT から delete → DEFAULT を attach」の順で処理する。無ければ単純に作成する。この分岐も pgTAP で確認する。

**RLS と権限**

```sql
alter table public.status_snapshots enable row level security;   -- 全テーブルで
grant usage on schema public to service_role;
grant select, insert, update, delete on all tables in schema public to service_role;
alter default privileges in schema public grant select, insert, update, delete on tables to service_role;
-- anon / authenticated には何も与えない（W2 で読み取りビューにだけ select を与える）
```

「Automatically expose new tables」を OFF にした本番では、**`service_role` にも明示的な grant が必要**である（実測：grant なしでは REST 経由で 42501 になった）。`alter default privileges` は将来作るテーブルにも効くが、**パーティションは親テーブル経由でアクセスするため個別の grant は不要**。

**Storage バケット**

```sql
do $$ begin
  if exists (select 1 from information_schema.schemata where schema_name = 'storage') then
    insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
    values ('gbfs-raw', 'gbfs-raw', false, 52428800, array['application/gzip'])
    on conflict (id) do nothing;
  end if;
end $$;
```

`storage` スキーマの存在で分岐するのは、素の Postgres イメージでマイグレーションを流す環境（将来の軽量 CI）でも失敗しないため。ローカルの `supabase start` と本番では作成される。

**CI の SQL テスト**

```yaml
# .github/workflows/sql-tests.yml
on:
  pull_request:
    paths: ["supabase/**", ".github/workflows/sql-tests.yml"]
jobs:
  sql-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: supabase/setup-cli@v1
        with: { version: latest }
      - run: supabase start -x realtime,edge-runtime,logflare,vector,imgproxy,studio   # 不要なサービスを省いて起動を速くする
      - run: supabase db reset
      - run: supabase test db
```

`supabase start` は初回にイメージを取得するため 2〜4 分かかる。既存の `ci.yml` に混ぜず別ワークフローにし、`supabase/**` が変わった PR だけで動かす。TypeScript の CI には影響させない。

**テスト（pgTAP）**

- 全テーブルと列の存在、PK・UNIQUE・NOT NULL 制約
- `ensure_snapshot_partitions(2)` が冪等（2 回目は 0 を返す）。翌々月の範囲の行を INSERT できる。範囲外の行は DEFAULT に入る。**DEFAULT に 3 か月先の行を入れた状態で `ensure_snapshot_partitions(3)` を呼ぶと、新パーティションが作られ行がそこへ移り、DEFAULT が空になる**
- `drop_expired_snapshot_partitions` が DEFAULT と現行月を消さない
- RLS が全テーブルで有効。`anon` に `select` 権限が無い。`service_role` に `insert` 権限がある
- マイグレーション適用後（seed ではなく）、`systems` に 2 行、`feed_state` に 2 行、`app_config` に 1 行、`lock_key` が一意

**完了条件**

- `supabase db reset` がローカルで通り、`supabase test db` が全通過
- `supabase db push` で本番に適用。Table Editor で 10 テーブルとパーティション（当月〜翌々月 ＋ DEFAULT）、`systems` と `feed_state` の 2 行ずつを確認
- 本番に対し `service_role` キーで `feed_state` を REST 経由で読める（`curl -H "apikey: ..." .../rest/v1/feed_state` → 200 で 2 行）
- CI の SQL テストジョブが通る
- `vercel.json` に `regions: ["hnd1"]` が入り、デプロイが成功する（`preferredRegion` は使わない）

### 6.3 PR B：取り込み RPC

**目的**：§11.3 の 3 つの RPC を実装し、pgTAP で境界条件を固める。

**変更ファイル**

```
supabase/migrations/<ts>_0007_ingest_rpc.sql     # begin_fetch / ingest_snapshot / finish_fetch、grant execute、notify pgrst
supabase/tests/0004_begin_fetch.sql
supabase/tests/0005_ingest_snapshot.sql
supabase/tests/0006_finish_fetch.sql
scripts/bench-ingest.ts                          # 実データのフィクスチャで初回・定常の所要時間を測る（node の pg を使う）
```

**`ingest_snapshot` の実装上の要点**

```sql
create or replace function public.ingest_snapshot(...) returns jsonb
language plpgsql security definer
set search_path = ''
set statement_timeout = '30s'
as $$
declare
  v_lock_key smallint; v_len int; v_registered int; v_present int;
  v_anomalous boolean; v_inserted boolean; v_new int; v_changed int;
begin
  select lock_key into v_lock_key from public.systems where system_id = p_system_id;
  if v_lock_key is null then raise exception 'unknown system %', p_system_id; end if;
  if not pg_try_advisory_xact_lock(8421, v_lock_key) then
    return jsonb_build_object('status', 'locked');
  end if;

  -- 引数配列の長さ検査（4 本すべて p_station_ids と同じ長さ）
  -- 未登録ポートの登録（idx = coalesce(max(idx), -1) + 1 から連番）。stations への書き込みはここだけ
  -- 登録済み全ポート × 入力を左結合し、idx 順に array_agg（無ければ -1）
  -- insert ... on conflict (system_id, observed_at) do nothing → 挿入されなければ duplicate を返す
  -- 異常ガード：v_present < v_registered * p_min_presence_ratio → v_anomalous
  -- station_status_latest の更新（下記）
  -- feed_state の last_etag / last_observed_at を更新（p_observed_at がより新しいときだけ前進させる）
  return jsonb_build_object('status', 'inserted', 'n_stations', v_present, 'n_new_stations', v_new,
                            'n_changed', v_changed, 'array_length', v_len, 'is_anomalous', v_anomalous);
end $$;
```

`station_status_latest` の更新は 1 文で書く。異常ガード時は「今回現れたポート」だけを対象にし、不在への反転を行わない。

```sql
insert into public.station_status_latest as l (system_id, station_id, bikes, docks, flags, is_present, last_changed_at)
select p_system_id, s.station_id,
       coalesce(i.bikes, l0.bikes, -1), coalesce(i.docks, l0.docks, -1), coalesce(i.flags, l0.flags, -1),
       i.station_id is not null, p_observed_at
from public.stations s
left join input i using (station_id)
left join public.station_status_latest l0 on (l0.system_id, l0.station_id) = (s.system_id, s.station_id)
where s.system_id = p_system_id
  and (i.station_id is not null or not v_anomalous)                      -- 異常時は不在への反転をしない
on conflict (system_id, station_id) do update
set bikes = excluded.bikes, docks = excluded.docks, flags = excluded.flags,
    is_present = excluded.is_present, last_changed_at = excluded.last_changed_at
where l.last_changed_at < excluded.last_changed_at                          -- last_updated の後退に備える
  and (l.bikes, l.docks, l.flags, l.is_present)
      is distinct from (excluded.bikes, excluded.docks, excluded.flags, excluded.is_present);
```

**テスト（pgTAP）**

- `begin_fetch`：初回は `claimed=true`。直後の 2 回目は `false`。31 秒後（`last_fetch_at` を手で戻して再現）は `true`。戻り値に `last_etag` と `last_observed_at` が入る
- `ingest_snapshot`：空のポート集合でも例外にならない（`n_stations=0`、異常ガードが効く）。3 ポート登録 → `idx` が 0,1,2。5 ポートで再実行 → 既存の `idx` は不変で新規は 3,4。配列長は 5。1 ポート欠けた入力 → 該当要素が `-1`、`is_present=false`。同じ `observed_at` の再実行 → `duplicate` で行数不変、`feed_state` 不変。**古い `observed_at`** で実行 → 行は入るが `station_status_latest` は変わらない。配列長不一致 → 例外。登録済み 10 ポートに対し 4 ポートの入力 → `is_anomalous=true`、`is_present` は据え置き。値の変化が無い再取り込み → `n_changed=0`
- `finish_fetch`：`result` が `inserted` / `duplicate` / `unchanged` なら `consecutive_errors` が 0 に戻り `last_success_at` が進む。`skipped_recent` / `locked` はログだけで `feed_state` を変えない。`ok=false` で `consecutive_errors` が加算。`feed_fetch_log` に 1 行入り `source` が記録される
- 権限：`service_role` から 3 つの RPC を `execute` できる。`anon` からはできない

同時実行（`locked` の経路）は pgTAP の単一セッションでは再現できない。設計で担保し、PR D のカナリア運転で `dedup_hits` の内訳を見る。

**完了条件**

- pgTAP 全通過
- 実データのフィクスチャ（HELLO 14,861 件）で初回 1 秒以内、定常 0.5 秒以内（プロトタイプ実測：0.65 秒 / 0.45 秒）
- 本番に `db push` 後、`service_role` で `begin_fetch` を REST 経由で呼べる（`POST /rest/v1/rpc/begin_fetch`）。呼んだ後に `feed_state.last_fetch_at` を手で NULL に戻す

### 6.4 PR C：`packages/gbfs-core`

**目的**：GBFS の JSON を検証・正規化し、RPC の引数に変換する**純粋関数**をまとめる。I/O を持たないため、フィクスチャで完全にテストできる。

**変更ファイル**

```
packages/gbfs-core/
  package.json  tsconfig.json  eslint.config.js
  src/
    schemas.ts        # z.looseObject による station_status / station_information のスキーマ
    normalize.ts      # 重複排除（先頭優先）、フラグのビット化、reported_age_s の丸め
    build-args.ts     # RPC 引数（p_station_ids と 4 本の配列）への変換
    index.ts
  fixtures/gbfs/2026-09-04/
    hellocycling.station_status.json.gz
    hellocycling.station_information.json.gz
    docomo-cycle.station_status.json.gz
    docomo-cycle.station_information.json.gz
  test/
    schemas.test.ts  normalize.test.ts  build-args.test.ts  golden.test.ts  perf.test.ts
```

**設計上の要点**

- **配列は「現れたポート」の分だけ作る**。`-1` を埋めた密な配列は RPC が組み立てる。TypeScript 側は `idx` を知らない
- `reported_age_s = observed_at − last_reported`。負値は 0、32767 超は 32767、`last_reported` 無し（ドコモは全件同値だが将来の欠落に備える）は `observed_at` と同じとみなして 0
- `flags = installed*1 + renting*2 + returning*4`
- 重複 `station_id` は先頭を残す。内容が異なる重複の件数を `warnings.conflicting_duplicates` に入れる
- HELLO の `vehicle_capacity` が文字列である等の非標準値は `looseObject` で保持し、検証で落とさない。`num_bikes_available` 等の必須値は `z.int().min(0)`
- `capacity` は読まない（§11.2）

**テスト**

- スキーマ：実データ 4 件が通る。必須キー欠落・負の台数・`station_id` の型違いは弾く。未知キーは保持する
- 正規化：重複 10 件（ドコモ実データ）が 5,800 件になる。先頭が残ることをフィクスチャの順序で確認。負の `reported_age_s` が 0 になる。フラグの 8 通り
- 配列組み立て：長さが揃う。順序が `p_station_ids` と一致
- ゴールデン：実データ 2 件について RPC 引数の SHA-256 をスナップショットとして保存し、リファクタで変わらないことを確認
- 性能：HELLO 実データの検証＋正規化＋組み立てが **150 ms 未満**（実測 69 ms）

**完了条件**：lint・typecheck・test 通過。`apps/web` から `@bikechance/gbfs-core` を import してビルドが通る。

### 6.5 PR D：収集エンドポイントとカナリア Cron

**目的**：ODPT → 検証 → Storage → RPC の一連を Route Handler として実装し、**5 分毎のカナリア Cron** で本番に流す。M0 の前に Cron の到達性と取り込みの正しさを低い負荷で確認する。

**変更ファイル**

```
apps/web/app/api/jobs/collect/[system]/route.ts
apps/web/lib/jobs/
  auth.ts             # CRON_SECRET の timingSafeEqual 比較
  odpt-fetch.ts       # 条件付き取得・UA・フォールバック・タイムアウト
  storage.ts          # gzip して gbfs-raw に保存（409 は成功扱い）
  collect.ts          # 一連の手順（下記）
  supabase.ts         # service_role クライアント（サーバー専用）
apps/web/test/jobs/collect.test.ts     # fetch と supabase をモックし、結果の分岐をすべて通す
apps/web/.env.local.example
scripts/reconcile-raw.ts               # Storage と DB の件数を UTC 日で突き合わせる
scripts/reconcile-snapshot.ts          # 生 JSON 1 件と DB の配列を idx で突き合わせる
vercel.json                            # crons: 2 本、*/5 * * * *
```

**一連の手順（`collect.ts`）**

1. `Authorization: Bearer` を `CRON_SECRET` と定数時間比較。不一致 → 401（**DB には何も書かない**。スキャナの試行でログを埋めないため）
2. `system` を `SYSTEM_IDS` で検証。未知 → 400。クエリの `source`（`cron` / `watchdog` / `manual`、既定 `cron`）を検証し、ログに記録する
3. `begin_fetch(system)` を呼ぶ。`claimed=false` → `finish_fetch(skipped_recent)` → 200
4. ODPT を取得。`If-None-Match: <last_etag>`、`User-Agent: BikeChance/0.1 (+CONTACT_EMAIL)`、`AbortSignal.timeout(20_000)`。認証付きが失敗（ネットワーク・5xx・タイムアウト）なら公開エンドポイントに 1 回だけフォールバック（ETag は同一なので 304 の最適化は失われない）
5. 304 → `finish_fetch(unchanged, ok=true)` → 200
6. 200 → `gbfs-core` で検証・正規化。`last_updated` が `last_observed_at` **以下**（同じか、後退している）→ `finish_fetch(unchanged)` → 200。後退したスナップショットは取り込まない
7. **受信したバイト列を gzip** し、§11.5 のパスに `contentType: 'application/gzip'`・`upsert: false` で保存。409 は成功扱い
8. `ingest_snapshot(...)` を呼ぶ。`inserted` / `duplicate` / `locked` はいずれも正常系
9. `finish_fetch(result, ok=true)` → 200
10. どこかで例外 → `finish_fetch(error, ok=false, message)` → **500**。`message` に URL を含めない

```ts
export const dynamic = "force-dynamic";
export const maxDuration = 60;   // 20 s × 2（フォールバック）＋ Storage ＋ RPC で最悪 45 秒程度
```

**Cron（カナリア）**

```json
{
  "regions": ["hnd1"],
  "services": { "web": { "root": "apps/web", "framework": "nextjs" } },
  "rewrites": [{ "source": "/(.*)", "destination": { "service": "web" } }],
  "crons": [
    { "path": "/api/jobs/collect/hellocycling", "schedule": "*/5 * * * *" },
    { "path": "/api/jobs/collect/docomo-cycle", "schedule": "*/5 * * * *" }
  ]
}
```

HELLO は 5 分周期なので 5 分毎でもほぼ全スナップショットが入る。ドコモ（80 秒周期）は 3 分の 1 程度になるが、カナリアの目的は到達性と正しさの確認なので構わない。

**テスト**

- 単体（vitest）：`result` の 8 分岐（`inserted` / `duplicate` / `unchanged`（304）/ `unchanged`（同一 `last_updated`）/ `skipped_recent` / `locked` / `error` / 401 / 400）をモックで通す。`error` のとき `finish_fetch` が `ok=false` で呼ばれ、応答が 500 であること。エラーメッセージに `consumerKey` が含まれないこと
- 統合（手動、ローカル Supabase）：`pnpm dev` で `curl -H "Authorization: Bearer $CRON_SECRET" localhost:3000/api/jobs/collect/hellocycling` → `inserted`。直後に再実行 → `skipped_recent`。31 秒後 → `unchanged`。ローカルの Storage に gzip がある。`reconcile-snapshot.ts` で不一致 0

**マージ後の確認（段 4 のゲート）**

- マージから 15 分以内に `feed_fetch_log` に両システムの行が入り、`status_snapshots` に少なくとも 1 行ずつある
- Vercel の Cron 画面で直近の実行が 200 になっている。**302 や 401 なら Deployment Protection が Cron を弾いている**。その場合は W1 の予定を止めず、対処を決める（開発プラン §5.7 の pg_cron 主系への切り替え、または Protection Bypass for Automation の設定）
- Observability で 1 回の Active CPU が 0.3 秒以下

**カナリア運転（段 5、約 18 時間）**：§8.2・§8.3・§8.5・§8.6 を実施。**§8.6 の照合が通らなければ PR E に進まない**。

**完了条件**：上記テスト・確認がすべて通ること。`.env.local.example` に値を入れないこと。

### 6.6 PR E：本番稼働（毎分化・ウォッチドッグ・監視）

**目的**：Cron を毎分にし、Vercel Cron が欠けたときの再起動、異常の検知と通知、日次の保守を揃える。**M0**。

**変更ファイル**

```
vercel.json                                        # crons を * * * * * に
supabase/migrations/<ts>_0008_alert_state.sql      # alert_state(alert_key pk, first_seen_at, last_sent_at, last_value jsonb)
supabase/migrations/<ts>_0009_ops_functions.sql    # send_alert / watchdog_collect / monitor_feeds / run_maintenance / refresh_station_activity / compute_daily_quality
supabase/migrations/<ts>_0010_cron_jobs.sql        # cron.schedule（名前つき。同名は上書き）
supabase/tests/0007_ops_functions.sql
scripts/setup-vault.ts                             # .env を読み vault.create_secret を実行（node の pg、パラメータ化クエリ。値をコマンドラインに載せない）
```

**pg_cron のジョブ（すべて UTC）**

| 名前 | スケジュール | 内容 |
|---|---|---|
| `watchdog_collect` | `* * * * *` | `feed_state.last_fetch_at` が 150 秒より古いシステムについて、`app_config.project_base_url` ＋ `/api/jobs/collect/{system}?source=watchdog` を `net.http_get` で叩く。`Authorization` は Vault の `cron_secret`。`timeout_milliseconds := 10000` を明示（§4.3 の 7） |
| `monitor_feeds` | `*/5 * * * *` | 下表の検知を行い、`alert_state` で重複を抑えて `net.http_post` で Webhook に送る |
| `maintain_partitions` | `0 18 * * *`（03:00 JST） | `ensure_snapshot_partitions(2)`、`drop_expired_snapshot_partitions(60)`、`feed_fetch_log` 30 日超と `cron.job_run_details` 7 日超の削除 |
| `refresh_station_activity` | `30 18 * * *`（03:30 JST） | 直近 25 時間のスナップショットの配列を `unnest with ordinality` で展開し、`stations.last_seen_at` を更新。72 時間見えないポートを `is_active=false`、再出現で `true` |
| `daily_quality` | `0 22 * * *`（07:00 JST） | 前日（JST）の取得数・期待数・最大欠損・エラー数・異常数・DB 増分を `daily_quality` に書き、要約を Webhook に送る |

**監視項目（`monitor_feeds`）**

| 検知 | 条件 | 抑制 |
|---|---|---|
| フィードの停滞 | `now() - feed_state.last_observed_at` が HELLO 15 分・ドコモ 4 分を超える | 同じ `alert_key` は 60 分に 1 回 |
| 収集器の連続失敗 | `consecutive_errors >= 5` | 同上 |
| 収集器の無反応 | `now() - last_fetch_at > 10 分`（ウォッチドッグでも復帰しない） | 同上 |
| 異常スナップショット | 直近 1 時間に `is_anomalous` が 1 件以上 | 同上 |
| DEFAULT パーティション | `status_snapshots_default` に 1 行以上 | 同上 |
| ポート数の急変 | 最新スナップショットの `n_stations` が 24 時間前の中央値から ±5% 以上ずれる | 同上 |
| DB 容量 | `pg_database_size` が 6 GB を超える（Pro の 8 GB に対する早期警告） | 日 1 回 |

Webhook が未設定（Vault に `alert_webhook_url` が無い）でも `alert_state` には記録し、ジョブは失敗させない。本文の形式は `app_config.alert_webhook_kind`（`discord` → `{"content"}`、`slack` → `{"text"}`、`generic` → そのまま）で切り替える。時刻は JST で書く。pg_cron から呼ぶ関数はすべて開始・終了・結果を `job_runs` に記録し、`pg_try_advisory_xact_lock(8423, 0)` で多重起動を避ける。

**ウォッチドッグの補足**：Vercel Cron が正常なら `last_fetch_at` は常に 60 秒以内に更新されるため、ウォッチドッグは発火しない。発火するのは Cron が 2 回以上欠けたとき。`net.http_get` は応答を待たない（fire-and-forget）ので、結果は次の分の `feed_state` で判断する。Deployment Protection が Cron を弾く場合でも、本番ドメイン `https://bike-chance.vercel.app` への直接要求は公開されているため、ウォッチドッグは通る。

**テスト**

- pgTAP：`refresh_station_activity` が 25 時間内に現れたポートの `last_seen_at` を更新し、72 時間見えないポートを非活性にし、再出現で活性に戻す。`daily_quality` が JST の日付で集計する。`ensure`/`drop` の実行後にパーティションが期待どおり。`alert_state` の抑制が効く（同じ key を 2 回呼んで 1 回だけ送る）
- 本番での強制発火：Vercel の Cron を 5 分間 Disable → `last_fetch_at` が 150 秒を超える → ウォッチドッグが叩く → `feed_fetch_log` に `source = 'watchdog'` の行が入ることで到達を確認。Webhook にテスト通知が届く（`select public.send_alert('test', '{}'::jsonb)` を手で実行）

**完了条件（M0）**

- 両システムが期待周期で増え続ける（30 分観測：HELLO 6 件、ドコモ 22 件前後）
- ウォッチドッグと通知の強制発火に成功
- `vercel crons run` 相当の手動実行で 200
- 開発プラン §15 に W1-13〜W1-20 を転記し、開発プラン §5.3 の DDL を本文書 §11 に追随させる（同じ PR で `docs/` を更新）

### 6.7 PR F：ポート属性の日次同期

**目的**：`station_information` を日次で取得し、`station_attributes` を SCD2 で更新する。

**変更ファイル**

```
apps/web/app/api/jobs/sync-stations/[system]/route.ts
apps/web/lib/jobs/sync-stations.ts
supabase/migrations/<ts>_0011_station_attributes_rpc.sql   # upsert_station_attributes
supabase/tests/0008_station_attributes.sql
vercel.json                                                # crons に 2 本追加（0 19 * * * = 04:00 JST）
```

**`upsert_station_attributes(p_system_id, p_fetched_at, p_rows jsonb) returns jsonb`**

- `pg_try_advisory_xact_lock(8422, lock_key)`
- 入力に含まれるが `stations` に無いポートは登録する（`idx` を採番）。FK の親行を先に作る
- 現在有効な行（`valid_to is null`）と `(name, lat, lon, capacity)` を比べ、変わっていれば旧行の `valid_to` を閉じて新行を追加。同じなら何もしない
- 入力に含まれないポートの有効行は**閉じない**（フィードの一時的な欠落で属性を失わないため）。閉じるのは W2 以降の判断
- 戻り値：`{"n_input", "n_new_stations", "n_changed", "n_unchanged"}`
- ドコモの `capacity`（動的）はそのまま保存し、意味の解釈は特徴量側で行う。HELLO は `capacity` が無く `vehicle_capacity`（文字列）のみのため、数値に変換できたら `capacity` に入れる
- 生 JSON は `gbfs-raw/{system}/{YYYY}/{MM}/{DD}/station_information_{fetched_at_epoch}.json.gz` に保存

**テスト**：初回で全件が新規。同じ入力の 2 回目で `n_changed=0`。座標を 1 件変えた入力で 1 件だけ新しい `valid_from` の行ができ、旧行の `valid_to` が閉じる。入力から 1 件消しても有効行が残る。`stations` に無い ID を含む入力で登録が行われる。

**完了条件**：本番で 2 回実行し、2 回目の `n_changed=0`。`station_attributes` の有効行数が HELLO 14,861・ドコモ 5,800 前後。status にしか現れないポート（ドコモ 11 件）に属性が無いことを確認し、将来の API がそれを許容する旨を開発プラン §8.3 に追記。

## 7. スケジュール（ゲート方式）

日付は目標であり、**各段のゲートを満たすまで次に進まない**。収集はこのアプリの生命線なので、早さより確実さを優先する。v1.0 より M0 を 1 日遅らせ、その代わりに 5 分毎のカナリア運転を挟んだ。

| 段 | 目標日 | 内容 | ゲート（次に進む条件） |
|---|---|---|---|
| 0 | 9/7（月）午前 | 事前準備：Supabase CLI、`link`、ローカル起動 | `supabase db reset` がローカルで通る |
| 1 | 9/7（月）午後 | **PR A** スキーマ v1 | pgTAP 全通過、リモートに適用済み、`service_role` の権限を REST 経由で確認 |
| 2 | 9/8（火）午前 | **PR B** 取り込み RPC | pgTAP 全通過、実データ 14,861 件で初回 1 秒以内 |
| 3 | 9/8（火）午後 | **PR C** `packages/gbfs-core` | 単体テスト通過、性能テスト 150 ms 未満 |
| 4 | 9/9（水）午前 | **PR D** 収集エンドポイント＋**カナリア Cron（5 分毎）** | マージ後 15 分以内に両システムのスナップショットが入る（= Cron の到達性と取り込みの一通りを確認） |
| 5 | 9/9（水）午後〜9/10 午前 | **カナリア運転**（約 18 時間） | 5 分毎の取得で欠損なし、エラー率 1% 未満、Storage と DB の件数一致、配列と生 JSON の照合が一致（§8.6） |
| 6 | 9/10（木）午前 | **PR E** 毎分化・ウォッチドッグ・監視 | **M0**：両システムが期待周期で増え続ける。ウォッチドッグと通知の強制発火を確認 |
| 7 | 9/10（木）〜9/11（金） | 24 時間観測 | **M1**：§8 の合格基準をすべて満たす |
| 8 | 9/11（金） | **PR F** 属性同期、開発プランの追随更新 | 2 回目の同期で新規行 0 |

**カナリア運転で見るもの**：Cron の到達（Deployment Protection の影響）、5 分毎という低い負荷での取り込みの正しさ、Vercel の Active CPU と Supabase の DB 増分が §4.4 の見積りと合うか。ここで問題が出れば PR E の前に直す。

**前倒しする場合**：PR A と PR C は他への依存がないため、9/5〜9/6 に着手できる。カナリア運転の時間は短縮しない。

**遅れた場合**：ゲートを満たさないまま次に進まない。9/11 に M1 が達成できなければ W2 の頭に持ち越し、W2 の Parquet 圧縮を後ろにずらす。収集の品質が学習データの品質を決めるため、この順序は動かさない。

## 8. 検証の手順と合格判定

カナリア運転（段 5）と 24 時間観測（段 7）で使う。すべて Supabase の SQL Editor か `psql` で実行できる。

### 8.1 取得率と欠損

```sql
-- 直近 24 時間の取得数と、期待値に対する取得率
with expected as (
  select 'hellocycling'::text as system_id, 288 as expected_per_day
  union all select 'docomo-cycle', 1080
),
actual as (
  select system_id, count(*) as n,
         min(observed_at) as first_at, max(observed_at) as last_at
  from status_snapshots
  where observed_at >= now() - interval '24 hours'
  group by system_id
)
select a.system_id, a.n, e.expected_per_day,
       round(100.0 * a.n / e.expected_per_day, 2) as pct,
       a.first_at, a.last_at
from actual a join expected e using (system_id);
```

```sql
-- 連続欠損の検出：隣り合うスナップショットの間隔が期待周期の 3 倍を超えた区間
select system_id, observed_at as gap_start, next_at as gap_end,
       next_at - observed_at as gap
from (
  select system_id, observed_at,
         lead(observed_at) over (partition by system_id order by observed_at) as next_at
  from status_snapshots
  where observed_at >= now() - interval '24 hours'
) t
join systems using (system_id)
where next_at - observed_at > make_interval(secs => expected_cadence_s * 3)
order by gap desc;
```

**合格ライン**：取得率が両システムとも 99.5% 以上。欠損区間の最大が 30 分未満。カナリア運転（5 分毎）ではドコモの取得数は期待値の 3 分の 1 程度になるため、この基準は段 7 で適用する。

### 8.2 重複・冪等性・エラー率

```sql
select system_id, observed_at, count(*)
from status_snapshots
where observed_at >= now() - interval '24 hours'
group by 1, 2 having count(*) > 1;   -- 0 行であること

select
  count(*) filter (where result = 'inserted')  as inserted,
  count(*) filter (where result = 'unchanged') as unchanged,
  count(*) filter (where result in ('duplicate','skipped_recent','locked')) as dedup_hits,
  count(*) filter (where not ok) as errors,
  count(*) as total_calls,
  round(100.0 * count(*) filter (where not ok) / count(*), 2) as error_pct
from feed_fetch_log
where fetched_at >= now() - interval '24 hours';
```

**合格ライン**：重複行 0。エラー率 1% 未満。`unchanged` が多いのは正常（HELLO は 5 回に 4 回が 304）。`dedup_hits` は Cron の二重配信やウォッチドッグの発火回数の目安になる。

### 8.3 生データとの整合（件数）

Storage の `gbfs-raw/{system}/{YYYY}/{MM}/{DD}/` のオブジェクト数と、同じ UTC 日の `status_snapshots` の行数を突き合わせる。照合スクリプト `scripts/reconcile-raw.ts`（PR D で追加）が両方を数えて差分を出す。

**合格ライン**：差分 0。

### 8.4 容量とコスト

```sql
select relname,
       pg_size_pretty(pg_total_relation_size(c.oid)) as total,
       pg_size_pretty(pg_relation_size(c.oid)) as heap,
       pg_size_pretty(pg_total_relation_size(reltoastrelid)) as toast
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relkind in ('r', 'p')
order by pg_total_relation_size(c.oid) desc limit 15;

select pg_size_pretty(pg_database_size(current_database())) as db_size;
```

Vercel 側は Observability の Active CPU と Invocations を確認する。

**合格ライン**：DB 増分 40 MB/日以下（実測ベースの期待値は 22.7 MB/日）。収集の Active CPU が 1 回 0.3 秒以下。

### 8.5 値の健全性

```sql
select s.system_id, s.observed_at, s.n_stations, s.is_anomalous,
       array_length(s.bikes, 1) as len_bikes,
       (select count(*) from stations st where st.system_id = s.system_id) as registered,
       (select min(v) from unnest(s.bikes) v) as min_bikes,
       (select max(v) from unnest(s.bikes) v) as max_bikes,
       (select count(*) from unnest(s.bikes) v where v = -1) as n_missing
from status_snapshots s
where s.observed_at = (select max(observed_at) from status_snapshots x where x.system_id = s.system_id);
```

**合格ライン**：配列長が登録ポート数と一致。`min_bikes` は -1 以上、`max_bikes` は 300 未満（実測の最大は HELLO 100・ドコモ 115）。`is_anomalous` が偽。`n_missing` がドコモで 30 未満、HELLO で 0 付近。

### 8.6 配列と生 JSON の照合

配列の並び（`idx`）と生 JSON の値が一致していることを、実データで確認する。`scripts/reconcile-snapshot.ts`（PR D で追加）が、指定した `observed_at` の生 gzip JSON を Storage から取得し、`stations` の `idx` を使って DB の配列と全ポートを突き合わせる。

**合格ライン**：カナリア運転中に少なくとも 3 スナップショット（各システム）で不一致 0。この照合が通らなければ PR E に進まない。

### 8.7 DEFAULT パーティションと保守

```sql
-- DEFAULT パーティションに行があれば、当月のパーティションが作られていない
select count(*) as rows_in_default from status_snapshots_default;

-- 存在するパーティション
select inhrelid::regclass as partition
from pg_inherits where inhparent = 'status_snapshots'::regclass order by 1;
```

**合格ライン**：DEFAULT は 0 行。当月・翌月・翌々月のパーティションが存在。

## 9. 障害時の対応とロールバック

| 事象 | 一次対応 | 備考 |
|---|---|---|
| 収集器が失敗し続ける | Vercel の Cron 設定画面で該当ジョブを **Disable**。または `vercel.json` から `crons` を外して再デプロイ | Disable してもジョブ数の上限にはカウントされる |
| pg_cron のジョブを止めたい | `select cron.alter_job(job_id := (select jobid from cron.job where jobname = '<名前>'), active := false);` | `postgres` は `cron.job` を直接更新できない。削除は `cron.unschedule('<名前>')` |
| マイグレーションを間違えた | **down は書かない**。修正内容を新しいマイグレーションとして追加する | `supabase db push` は前進のみ。既存の適用済みファイルは書き換えない |
| デプロイを戻したい | Vercel の Instant Rollback | **Cron 設定は旧デプロイに戻らない**。`vercel.json` の変更を戻す場合は再デプロイが必要 |
| 誤ってデータを消した | Supabase の日次バックアップ（7 日保持）から復元。`status_snapshots` は Storage の生 JSON から再構築できる（再構築スクリプトは W2） | 生 JSON が一次ソースである理由 |
| ODPT 側が落ちた | 何もしない。欠損として記録し、補間はしない | `feed_fetch_log` に `ok=false` で残る。ウォッチドッグは叩き続けるが害はない |
| DEFAULT パーティションに行が入った | `select public.ensure_snapshot_partitions(2);` を手で実行する。関数が DEFAULT の detach → 作成 → 行の移動 → attach を 1 トランザクションで行う。**新パーティションを直接 `create table ... partition of` すると、DEFAULT に該当行があるためエラーになる** | 保守ジョブが止まっている原因（`cron.job_run_details`）を調べる |
| カナリア運転で問題が出た | Cron を Disable し、原因を直して PR D に追加コミット。カナリアを再開して再度観測する | PR E には進まない |

**PR ごとのロールバック可否**

| PR | main へマージ後の巻き戻し |
|---|---|
| A・B（SQL のみ） | 前進マイグレーションで修正。テーブルが空のうちは `drop table` を含む修正も安全 |
| C（純粋関数のみ） | 呼び出し元がないため影響なし |
| D（エンドポイント＋カナリア Cron） | Cron を Disable すれば収集が止まるだけで、既存データは無傷。5 分毎なので影響範囲は小さい |
| E（毎分化・監視） | **本番稼働の分岐点**。Cron を Disable、または `vercel.json` を 5 分毎に戻す |
| F（日次同期） | Cron を Disable。`station_attributes` は追記のみなので既存行は壊れない |

## 10. W1 では作らないもの

範囲を絞るために、次は意図的に W1 の外に置く。開発プラン §13 の該当週で扱う。

| 項目 | 実施週 | 理由 |
|---|---|---|
| Parquet 圧縮ジョブ（`/ml/compact`）と Python サービス | W2 | 収集が動いていないと圧縮するものがない |
| ホットストア再構築スクリプト | W2 | 同上 |
| ポートの出入りの平滑化（「休止」判定） | W2 | 生の `is_present` を保存すれば、平滑化は後から自由に決められる |
| 座標 → 都道府県・市区町村（`pref_code` / `muni_code`） | W3 | 国土数値情報の取り込みが別作業。カラムは W1 で用意し NULL のままにする |
| バックアップ収集器（Supabase Edge Function） | W3 | まず単一系＋ウォッチドッグの欠損率を測ってから判断（開発プラン §5.7） |
| 予測・モデル関連テーブル（`station_forecasts` 等） | W4 | W1 のマイグレーションには含めない。空テーブルを先に作らない |
| 天気・祝日の取り込み | W4 | 特徴量の作業 |
| 近傍リスト・履歴プロファイル | W3 | 特徴量の作業 |
| 公開 API 用の読み取りビュー（`/v1/stations` の現在値） | W2 | `station_status_latest` に `is_present` が入ったので、ビューはその上に作れる |

## 11. 付録：PR をまたぐ契約

PR B（DB 側）と PR C・D（アプリ側）が同じものを作るために、境界の意味をここで固定する。実装中にここを変える場合は、両方の PR に反映し本文書を更新する。

### 11.1 配列の意味

各システムには、ポートごとに **不変・密・0 起点の整数インデックス `idx`** を割り当てる。スナップショット行の配列は `idx` 順に並び、**Postgres の配列は 1 起点なので `idx` のポートの値は `arr[idx + 1]`** にある。

配列の長さは、そのスナップショットを取り込んだ時点で登録済みのポート数（`max(idx) + 1`）。過去の行は当時の長さのままで、後から伸ばさない。

| 列 | 型 | 値 | 欠損時 |
|---|---|---|---|
| `bikes` | `smallint[]` | `num_bikes_available` | `-1` |
| `docks` | `smallint[]` | `num_docks_available` | `-1` |
| `flags` | `smallint[]` | ビット和：`1`=`is_installed`、`2`=`is_renting`、`4`=`is_returning`（すべて真なら `7`） | `-1` |
| `reported_age_s` | `smallint[]` | `observed_at - last_reported` の秒数。**負値は 0 に丸め**、32767 超は 32767 に丸める | `-1` |

**`-1` は欠損（そのポートが登録済みなのに今回のフィードに現れなかった）専用**である。GBFS の台数・返却枠・フラグに負値は存在せず、`reported_age_s` の負値は 0 に丸めるため、`-1` が本物の値と衝突することはない。

**ドコモのポート ID の出入り**：実測では 60 分間に 122 件のポートが一部の更新にしか現れなかった（1 更新あたり 5〜19 件が出入りする）。ただし和集合は 1 回あたりの件数 +6 に収まり、無限に増えることはない。したがって `idx` は有限集合の中で振られ、配列長が際限なく伸びる心配はない。出入りするポートの `-1` は生の真値としてそのまま保存し、「休止」として見せるかどうかの平滑化（例：3 回連続で欠損したら休止扱い）は W2 以降の API と特徴量側で行う。

**`station_information` に無いポート**：ドコモの status には情報フィードに存在しないポートが 11 件ある（実測）。`ingest_snapshot` は status のポート ID から登録するため、取り込みには影響しない。属性（名称・座標）が無いポートが存在することを、PR F の同期と将来の API は前提にする。

**重複 ID の扱い**：同一スナップショット内に同じ `station_id` が複数あるとき（ドコモに実在する完全重複 10 件）は**先頭を残す**。内容が異なる重複は件数を数えて `feed_fetch_log.warnings` に記録する。

### 11.2 `gap` を保存しない理由（W1 の設計変更）

開発プラン §5.3 では `status_snapshots` に `gap`（`capacity − bikes − docks`）列を置いていたが、**W1 では作らない**。

- `capacity` は `station_information` 由来で、日次同期（PR F）で `station_attributes` に有効期間つきで入る。収集器は毎分動くため、容量を持つには古い値をキャッシュするしかない
- 特徴量を作る時点で `station_attributes` の**その時刻に有効だった容量**と結合すれば、常に正しい `gap` が得られる
- 収集の最短経路から容量参照が消え、収集器が状態を持たなくなる

`gap` は開発プラン §6.3 の特徴量として、W3 の特徴量パイプラインで導出する。監視の「bikes + docks > capacity」も同じ結合で確認する。

### 11.3 RPC の契約と `feed_state` の書き込み規律

収集器は 3 つの RPC を順に呼ぶ。**どの列を誰が書くか**を固定することで、失敗時に取りこぼしが起きない構造にする。

| `feed_state` の列 | 書く者 | 意味 |
|---|---|---|
| `last_fetch_at` | `begin_fetch` | 取得を始めた時刻。二重起動の抑止とウォッチドッグの判定に使う |
| `last_success_at` | `finish_fetch` | ODPT から正常な応答（200 か 304）を得た最後の時刻（`result` が `inserted` / `duplicate` / `unchanged` のとき）。バックアップ収集器の起動判定に使う |
| `consecutive_errors` | `finish_fetch` | 連続失敗回数。成功で 0 に戻す |
| `last_etag` | **`ingest_snapshot` のみ** | 最後に**取り込みに成功した**スナップショットの ETag |
| `last_observed_at` | **`ingest_snapshot` のみ** | 最後に取り込みに成功したスナップショットの `last_updated` |

`last_etag` と `last_observed_at` を関数側で書かないのは、Storage への保存後に RPC が失敗した場合の取りこぼしを防ぐためである。次の取得は「最後に取り込みに成功した ETag」で条件付き要求を出すため、ODPT は 200 で同じ内容を返し、取り込みが再試行される。

**1. `public.begin_fetch(p_system_id text, p_min_interval_s integer default 30) returns jsonb`**

`update public.feed_state set last_fetch_at = now() where system_id = p_system_id and (last_fetch_at is null or last_fetch_at < now() - make_interval(secs => p_min_interval_s)) returning ...` により、**1 文で原子的に claim** する。更新できたら `claimed: true`、他の実行が直前に claim していれば `claimed: false`。読んでから書く方式ではないため、同時に走る 2 つの関数が両方通過することはない。

```json
{ "claimed": true, "last_etag": "W/\"400ede-...\"", "last_observed_at": "2026-09-07T04:26:34Z" }
```

`claimed` が `false` のとき、関数は `skipped_recent` で終了する。

**2. `public.ingest_snapshot(...) returns jsonb`**

```sql
create function public.ingest_snapshot(
  p_system_id      text,
  p_observed_at    timestamptz,   -- フィードの last_updated
  p_fetched_at     timestamptz,
  p_etag           text,          -- 応答の ETag（無ければ null）
  p_station_ids    text[],        -- フィードに現れたポート ID（重複排除済み）
  p_bikes          smallint[],    -- 以下 4 つは p_station_ids と同じ長さ・同じ順序
  p_docks          smallint[],
  p_flags          smallint[],
  p_reported_age_s smallint[],
  p_raw_path       text,
  p_min_presence_ratio numeric default 0.5   -- 異常ガードの閾値
) returns jsonb
```

処理内容（1 トランザクション、`set statement_timeout to '30s'`）

1. `pg_try_advisory_xact_lock(8421, systems.lock_key)` を取る。取れなければ `{"status":"locked"}` を返す
2. 引数配列の長さが揃っていなければ例外
3. `p_station_ids` のうち未登録のものを `stations` に登録し、`idx` を採番する。**`stations` への書き込みはこれだけ**（`last_seen_at` 等は日次ジョブが計算する）
4. 登録済み全ポート分の密な配列を組み立てる（現れなかったポートは `-1`）
5. `status_snapshots` に挿入。`(system_id, observed_at)` が既にあれば `{"status":"duplicate"}` を返して終了（`feed_state` は触らない）
6. **異常ガード**：出現ポート数が「登録済みポート数 × `p_min_presence_ratio`」未満なら `is_anomalous = true`。スナップショットは保存するが、手順 7 の `is_present` 反転は行わない
7. `station_status_latest` を更新する。対象は `p_observed_at > 現在の last_changed_at` の行のみ（`last_updated` の後退に備える）。`(bikes, docks, flags, is_present)` のいずれかが変わった行だけ書き、`last_changed_at = p_observed_at` にする。今回現れなかったポートは `is_present = false`（異常ガード時は据え置き）
8. `feed_state` の `last_etag`・`last_observed_at` を更新する。ただし **`p_observed_at` が現在の `last_observed_at` より新しいときだけ**前進させる（後退したスナップショットを別経路から渡されても状態を巻き戻さない）
9. 戻り値を返す

```json
{ "status": "inserted", "n_stations": 14861, "n_new_stations": 3,
  "n_changed": 341, "array_length": 14861, "is_anomalous": false }
```

`status` は `inserted` / `duplicate` / `locked`。呼び出し側はいずれも正常系として記録する。

**3. `public.finish_fetch(p_system_id text, p_log jsonb) returns void`**

`feed_fetch_log` に 1 行入れる（`source` を含む）。`p_log.result` が `inserted` / `duplicate` / `unchanged` なら `last_success_at = now()`・`consecutive_errors = 0`。`p_log.ok` が偽なら `consecutive_errors + 1`。`skipped_recent` / `locked` はログだけで `feed_state` を変えない。`p_log` の中身は §11.6 の応答と同じ項目（URL とトークンは含めない）。

**`station_status_latest` の意味**

| 列 | 意味 |
|---|---|
| `bikes` / `docks` / `flags` | 最後に**観測された**値。ポートがフィードから消えても保持する |
| `is_present` | 最新スナップショットにそのポートが現れたか |
| `last_changed_at` | 上の 4 つのいずれかが最後に変わった時刻。「最後に観測した時刻」ではない |

フィード全体の鮮度（「○分前の観測」）は `feed_state.last_observed_at` を使う。ポート単位の「最後に見た時刻」が必要なら、日次ジョブが `stations.last_seen_at` に書く値を使う。

### 11.4 アドバイザリロックのキー

同時実行の抑止に使うキーは、`hashtext()` のような文書化されていない内部関数に依存させない。`systems` テーブルに **`lock_key smallint not null unique`** を持たせ（`hellocycling` = 1、`docomo-cycle` = 2）、2 引数形式の `pg_try_advisory_xact_lock(class_id, object_id)` を使う。

| 用途 | `class_id` | `object_id` |
|---|---|---|
| 取り込み（`ingest_snapshot`） | 8421 | `systems.lock_key` |
| 属性同期（`upsert_station_attributes`） | 8422 | `systems.lock_key` |
| パーティション保守・日次集計 | 8423 | 0 |

`begin_fetch` の claim はアドバイザリロックではなく `feed_state` の行更新そのものが排他になるため、ロックを使わない。

**トランザクション単位を使う理由**：Supavisor のトランザクションモードでは、トランザクションが終わると接続がプールに返る。セッション単位の `pg_try_advisory_lock` はセッションが続く限りロックを保持するため、プール越しでは「どのセッションが持っているか」が実行のたびに変わり、意図した排他にならない。`pg_try_advisory_xact_lock` はコミット・ロールバック時に必ず解放されるため、プール構成でも安全に使える。

### 11.5 Storage のパス規約

```
gbfs-raw/{system_id}/{YYYY}/{MM}/{DD}/station_status_{observed_at_epoch}.json.gz
gbfs-raw/{system_id}/{YYYY}/{MM}/{DD}/station_information_{fetched_at_epoch}.json.gz
```

- 日付は **UTC**。Parquet のパーティションも UTC に揃える。一方、`daily_quality.quality_date` は人が読む QA 用なので **JST の日付**にする。用途が違うので基準が違うことを明記して混同を防ぐ
- `observed_at_epoch` はフィードの `last_updated`（POSIX 秒）。同じ観測を二重に保存しない
- **保存するのは ODPT から受信したバイト列そのもの**を gzip したもの。パース後に再直列化しない（原文との一致を保つため）
- `upsert: false` で保存し、既存（HTTP 409）は正常系として扱う。パスが同じなら内容も同じとみなす
- `contentType` は `application/gzip` を明示する（省略すると `text/plain` になる）
- バケットは非公開。読み出しはサービスロールまたは S3 互換の資格情報経由のみ

### 11.6 収集エンドポイントの応答

`GET /api/jobs/collect/{system}` の応答は、結果の種類でステータスコードを分ける。Vercel Observability のエラー率で異常を検知できるようにするため、失敗は 5xx にする。

| `result` | HTTP | 意味 |
|---|---|---|
| `inserted` | 200 | 新しいスナップショットを保存した |
| `duplicate` | 200 | 同じ `observed_at` が既にあった（Cron の二重配信など） |
| `unchanged` | 200 | 304 だった、または `last_updated` が前回以下（同じか後退） |
| `skipped_recent` | 200 | 直前（30 秒以内）に別の実行が取得している |
| `locked` | 200 | 同じシステムの取り込みが実行中 |
| `error` | **500** | 取得・保存・取り込みのいずれかで失敗した |
| （認証失敗） | **401** | `CRON_SECRET` 不一致 |
| （未知のシステム） | **400** | パスの `system` が `systems` に無い |

```json
{ "ok": true, "system": "hellocycling", "result": "inserted",
  "observed_at": "2026-09-07T04:31:34.000Z", "http_status": 200,
  "n_stations": 14861, "n_new_stations": 0, "n_changed": 341,
  "bytes": 4204793, "duration_ms": 1284, "endpoint": "token", "source": "cron" }
```

`error` の場合は `message` に文脈（phase、HTTP ステータス、例外名）を入れるが、**URL とトークンは含めない**。`response.url` もログに出さない（クエリにトークンが載っている）。

### 11.7 環境変数と設定値の一覧（W1 時点）

| 変数 | ローカル `.env` | `apps/web/.env.local` | Vercel（Production） | 用途 |
|---|---|---|---|---|
| `ODPT_ACCESS_TOKEN` | ○ | ○ | ○ | GBFS の認証付き取得 |
| `SUPABASE_URL` | ○（本番） | ○（ローカル） | ○ | Storage と PostgREST |
| `SUPABASE_SECRET_KEY` | ○（本番） | ○（ローカル） | ○ | サーバーからの書き込み |
| `SUPABASE_DB_URL` | ○（本番） | — | ○ | 直接接続（スクリプトと W2 の Python サービス）。Route Handler は使わない |
| `CRON_SECRET` | ○ | ○ | ○ | Cron 認証 |
| `CONTACT_EMAIL` | ○ | ○ | ○ | ODPT 通知文と User-Agent |
| `SUPABASE_PUBLISHABLE_KEY` | ○ | — | — | 現時点で未使用 |
| `SUPABASE_PROJECT_REF` | ○ | — | — | `supabase link` |
| `SUPABASE_ACCESS_TOKEN` | ○（設定済み・検証済み） | — | — | CLI の非対話実行 |
| `SUPABASE_DB_PASSWORD` | ○（設定済み） | — | — | `supabase link` / `db push` |
| `ALERT_WEBHOOK_URL` | ○（任意） | — | — | 監視通知。Vault へ投入する元 |

DB 側に置く値：

| 保管先 | 名前 | 内容 |
|---|---|---|
| Vault | `cron_secret` | ウォッチドッグが Vercel を叩くときの `Authorization` |
| Vault | `alert_webhook_url` | 監視通知の送信先（設定されていれば） |
| `app_config` テーブル | `project_base_url` | `https://bike-chance.vercel.app`。秘密ではないため Vault に入れない |

### 11.8 環境ファイルの使い分け

Next.js は**アプリのディレクトリ（`apps/web`）直下の `.env*` だけ**を読み、リポジトリ直下の `.env` は読まない。逆に Supabase CLI はリポジトリ直下で動かす。用途で分ける。

| ファイル | 中身 | 使う場面 | git |
|---|---|---|---|
| `.env`（リポジトリ直下） | **本番**の Supabase・ODPT・CLI の資格情報 | `supabase link` / `db push`、Vault 投入スクリプト、本番への手動確認 | 無視 |
| `apps/web/.env.local` | **ローカル Supabase** の URL・キー（`supabase status -o env` の出力）、ODPT トークン、`CRON_SECRET` | `pnpm dev`、ルートハンドラの手動実行 | 無視 |
| Vercel の環境変数 | 本番の値 | 本番・プレビューの関数 | — |

ローカル開発の既定は**ローカル Supabase**に向ける。本番 DB に向けた `pnpm dev` は、プレビューデプロイで代替できるため原則行わない。`apps/web/.env.local` の雛形は PR D で `apps/web/.env.local.example` として追加する（値は空）。

## 12. 本文書の根拠にした一次情報

W1 の実装で判断が分かれた点について、実際に読んだ、あるいは測った出典。開発プラン §16 の一覧とは重複しない、W1 固有のものだけを挙げる。

**Vercel**

- Cron の仕組みと制約（UTC 固定、ベストエフォート配信、リダイレクトを追わない、`CRON_SECRET` の自動付与）：https://vercel.com/docs/cron-jobs ／ https://vercel.com/docs/cron-jobs/manage-cron-jobs
- Cron のトラブルシュート（`vercel build --prod` で `.vercel/output/config.json` を確認する手順、`force-dynamic` の推奨）：https://vercel.com/kb/guide/troubleshooting-vercel-cron-jobs
- Services の構成と制約（`functions` はサービス内、公開ルーティングはトップレベル、Middleware 不可）：https://vercel.com/docs/services ／ https://vercel.com/docs/services/config-reference ／ https://vercel.com/docs/build-output-api/services
- `vercel.json` のスキーマ（`crons` と `services` が同階層に共存できること）：https://openapi.vercel.sh/vercel.json
- Deployment Protection の適用範囲（Standard Protection は本番ドメインを保護しない）：https://vercel.com/docs/deployment-protection
- 関数の実行時間・メモリ・リージョン別単価：https://vercel.com/docs/functions/configuring-functions/duration ／ https://vercel.com/docs/functions/usage-and-pricing
- `after()` と `waitUntil()` の意味論：https://vercel.com/docs/functions/functions-api-reference/vercel-functions-package

**Supabase / PostgreSQL**

- 拡張の有効化と一覧：https://supabase.com/docs/guides/database/extensions
- pg_cron（インストール、秒単位スケジュール、同時実行の指針、`job_run_details` が自動削除されないこと）：https://supabase.com/docs/guides/cron ／ https://supabase.com/docs/guides/cron/quickstart
- pg_net（関数シグネチャ、既定タイムアウト 2000 ms、コミット後に送信、応答は 6 時間保持）：https://supabase.com/docs/guides/database/extensions/pg_net
- Vault（`vault.create_secret`、`vault.decrypted_secrets`）と pg_cron からの利用例：https://supabase.com/docs/guides/database/vault ／ https://supabase.com/docs/guides/functions/schedule-functions
- ロール別の `statement_timeout` と関数単位での上書き：https://supabase.com/docs/guides/database/postgres/timeouts
- RLS とビューの注意（ビューは既定で RLS を迂回する）：https://supabase.com/docs/guides/database/postgres/row-level-security
- API の保護と既定権限の取り消し：https://supabase.com/docs/guides/api/securing-your-api
- Data API のキー（`sb_publishable_` / `sb_secret_`）：https://supabase.com/docs/guides/api/api-keys
- サーバーレスからの接続方法（Supavisor トランザクションモード、プリペアドステートメント無効）：https://supabase.com/docs/guides/database/connecting-to-postgres
- Storage のアップロード（`contentType` の既定、同一パス上書きの CDN 影響、S3 互換エンドポイント）：https://supabase.com/docs/guides/storage/uploads/standard-uploads ／ https://supabase.com/docs/guides/storage/s3/authentication
- CLI（`init` / `link` / `migration new` / `db push` / `db reset` と `supabase_migrations.schema_migrations`）：https://supabase.com/docs/guides/deployment/database-migrations
- pg_partman（利用可能。`create_parent`、`run_maintenance_proc`、`retention_keep_table` の既定）：https://supabase.com/docs/guides/database/extensions/pg_partman
- PostgREST（関数の設定をトランザクションに持ち上げる、RPC は POST 本文で引数を受ける）：https://postgrest.org/en/stable/references/transactions.html ／ https://postgrest.org/en/stable/references/api/functions.html
- 行セキュリティは GRANT に**追加**で適用される（`BYPASSRLS` は権限を代替しない）：https://www.postgresql.org/docs/17/ddl-rowsecurity.html

**Next.js / ライブラリ**

- Route Segment Config（Next.js 16 で `preferredRegion` が非推奨）：https://nextjs.org/docs/app/api-reference/file-conventions/route-segment-config
- Zod v4 の変更点（`z.looseObject`、`z.int`、`z.iso.datetime`）：https://zod.dev

**本文書のための実測**

2026-09-05 に以下を実施した。いずれも本番の Supabase と ODPT に対して行い、DB への書き込みは一時テーブルとロールバックで完結させている。

| 測定 | 対象 | 結果の記載箇所 |
|---|---|---|
| GBFS のパース・検証・配列化・直列化の CPU 時間 | 実フィード（HELLO 4.0 MB / ドコモ 0.86 MB） | §4.1 (a) |
| 取り込み SQL 3 文の実行時間 | 本番 DB ＋ 実データ 13 スナップショット | §4.1 (b) |
| スナップショット 1 行の実サイズと圧縮方式の比較 | 本番 DB ＋ 実データ | §4.1 (c) |
| Data API の本文サイズ上限 | 本番プロジェクトに 176 KB〜3.3 MB を POST | §4.1 (d) |
| ODPT の `ETag` / `If-None-Match` 対応 | 認証付き・公開の両エンドポイント | §4.1 (d) |
| 拡張の入手可否とロール別タイムアウト | 本番 DB の `pg_available_extensions` / `pg_db_role_setting` | §4.1 (d) |
| Deployment Protection の挙動 | 本番ドメインと生成デプロイ URL | §4.1 (d) |

**v1.1 の検証で追加した実測（2026-09-06、保存済みスナップショット 60 分分と本番エンドポイント）**

- ドコモの `station_id`：45 スナップショットで 1 回あたり 5,792〜5,805 件、和集合 5,811 件、全回出現 5,689 件、一部の回のみ 122 件。`station_information` に無い ID 11 件、status に一度も出ない information の ID 0 件
- HELLO の `station_id`：13 スナップショットで常に 14,861 件、出入り 0
- `last_updated` の後退：両システムとも 60 回の取得で 0 回
- ETag：`api-public.odpt.org` と `api.odpt.org`（認証付き）で同一の値（両システム）
