# BikeChance Week 1 実装プラン — 収集基盤の本番稼働

作成日：2026-09-05（土）　作成：Claude Code　対象期間：2026-09-07（月）〜 09-11（金）
上位文書：`docs/260904_dev_plan.md`（開発プラン v1.2）の §5「データ蓄積設計」と §13 の W1 行

---

## 0. この文書について

- **目的**：開発プラン §5.8 に箇条書きで書いた W1 の手順を、**そのまま実装に着手できる粒度**まで分解する。何を・どの順で・どこまでやったら完了か、どう検証するかを PR 単位で定義する。
- **PR 分割の方針**：1 つの PR は「1 つの関心事」「20〜40 分で読める分量」「単独で検証でき、マージしても本番が壊れない」を満たすこと。`main` へのマージは Vercel の本番デプロイを意味するため、**壊れた状態を main に置かない**ことを最優先にする。
- **参照の表記**：本文書内の章節は `§4.2` のように書く。上位文書を指す場合は必ず「開発プラン §5.3」のように前置きする。
- **この文書の読み方**：§4 で W1 中に確定させる設計判断を先に示し、§5 で 6 本の PR を定義する。実装時は §5 の各 PR の「完了条件」を満たしたら次に進む。§10 の付録に、複数の PR にまたがる契約（RPC のシグネチャと配列の意味）を置いた。ここが PR B と PR C の合意点になる。
- **開発プラン本体との関係**：本文書は W1 の実装詳細のみを扱う。設計の根拠・代替案・決定記録は開発プランにある。実装中に設計判断が変わった場合は、**開発プランの該当章と §15 を先に更新**してから実装する（CLAUDE.md §2 の原則 10）。

## 1. Week 1 のゴールと完了条件

**ゴール：GBFS の収集を本番で連続稼働させ、学習に使えるデータが毎日貯まる状態にする。**

学習データは収集開始日より前には存在しない。W1 の遅延はそのまま W6 の LightGBM v1 の品質低下になるため、他のどの作業よりも優先する。

| マイルストーン | 目標日 | 完了条件 |
|---|---|---|
| **M0 収集の本番稼働** | 9/9（水） | 両システムのスナップショットが `status_snapshots` に連続して入り、対応する生 gzip JSON が Storage に存在する。Vercel Cron が毎分起動している |
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

判定用の SQL は §7 にまとめた。

## 2. 現在の状態（2026-09-05 時点）

**完了済み**

| 項目 | 状態 |
|---|---|
| GitHub リポジトリ | `kusui26/BikeChance`（非公開、既定ブランチ `main`） |
| Vercel | Pro、プロジェクト作成済み、関数リージョン `hnd1`、本番 `https://bike-chance.vercel.app` |
| Vercel 環境変数（Production） | `ODPT_ACCESS_TOKEN` / `SUPABASE_URL` / `SUPABASE_SECRET_KEY` / `SUPABASE_DB_URL` / `CRON_SECRET` / `CONTACT_EMAIL` |
| Supabase | Pro、Tokyo、Micro、Data API 有効・新規テーブルの自動公開 OFF・自動 RLS ON |
| コード | モノレポ雛形（`apps/web` / `packages/shared`）、CI（lint・format・typecheck・test・build・機密チェック）、`GET /v1/meta` が本番で 200 |
| ローカル `.env` | 9 変数を整備。ODPT トークン、Supabase 一式、`CRON_SECRET`、`CONTACT_EMAIL` |

**未着手**

Supabase CLI のインストール、DB スキーマ、RPC、Storage バケット、Vault のシークレット登録、収集器、Cron 登録、監視ジョブ。

**ローカル環境の確認結果**：Docker 24.0.5 稼働中（`supabase start` が使える）、ディスク空き 24 GB、Supabase CLI 2.115.0 が Homebrew で入手可能、Vercel CLI 未インストール、`psql` 14.13 と `psycopg` 3.3.4 は導入済み。

**本番 Supabase の確認結果**：PostgreSQL 17.6、`public` スキーマは空、`pg_cron` 1.6.4・`pg_net` 0.20.4・`pg_partman` 5.3.1・`pgtap` 1.3.3 が利用可能、`supabase_vault` と `pgcrypto` は導入済み。

## 3. 事前準備

### 3.1 あなたの作業（1 つだけ）

W1 を非対話で進めるために、**Supabase の個人アクセストークン**が要ります。`.env` の `SUPABASE_ACCESS_TOKEN=` に貼り付けてください。

- 取得場所：https://supabase.com/dashboard/account/tokens の **Generate new token**
- 用途：`supabase link` と `supabase db push` を `supabase login` なしで実行する
- 形式：`sbp_` で始まる文字列

**権限（Permissions）の設定**

トークン作成時にカテゴリごとに No access / Read only / Full access を選べます。次のように設定してください。

| カテゴリ | 設定 | 理由 |
|---|---|---|
| Projects | **Full access** | `supabase link` がプロジェクト情報を読み、リンク状態を作る |
| Database | **Full access** | `supabase db push` がマイグレーションを適用する。読み取り専用では失敗する |
| Edge Functions | **Full access** | W3 のバックアップ収集器のデプロイで使う |
| Secrets | **Full access** | Edge Function の環境変数設定で使う（W3） |
| 上記以外すべて（Billing、Organizations、Auth、Storage、Domains、Analytics ほか） | **No access** | CLI からは触らない。事故の影響範囲を狭める |

**Read only ではなく Full access が必要な理由**：`supabase db push` はマイグレーションを適用する書き込み操作です。読み取り専用のトークンでは 403 で止まります。逆に、すべてを Full access にする必要もありません。課金・組織・認証設定に触る操作は一切行わないため、そこは No access のままにします。

Edge Functions と Secrets は W3 まで使いませんが、トークンの権限は後から変更できない場合があるため、最初から付けておいて再発行の手間を省きます。

**取り扱い**：このトークンはアカウント全体に効き、所属する組織とプロジェクトすべてに届きます。パスワードと同じ扱いにしてください。保存先は `.env`（gitignore 済み）だけで、Vercel や GitHub には登録しません。有効期限を選べる場合は設定してください。

**すでに済ませてあること**：`SUPABASE_DB_PASSWORD` は既存の `SUPABASE_DB_URL` から取り出して設定済みです。貼り付けは不要です。

トークンが入れば、以降の Supabase 操作はすべて私の側で実行できます。

### 3.2 私の作業（PR A の中で実施）

- Supabase CLI のインストール（`brew install supabase/tap/supabase`）
- `supabase init`、`supabase link --project-ref <ref>`
- ローカル Postgres の起動と `supabase db reset` による適用テスト
- Storage バケットの作成（SQL マイグレーションで実施。ダッシュボード操作は不要）
- Vault へのシークレット登録（`.env` を読むローカルスクリプトで実施。値をリポジトリに置かない）

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
| **W1-5** | 同時実行の抑止 | **`pg_try_advisory_xact_lock(class_id, systems.lock_key)`**（トランザクション単位、キーは明示的な列） | Supavisor のトランザクションモードではセッション単位のロックが意図どおり効かない。`hashtext()` は文書化されていない内部関数なので使わない（§10.4） |
| **W1-6** | 取り込み RPC の分割 | **`ingest_snapshot` 1 本に統合**し、ポート登録も内部で行う | クライアントに `idx` の対応表を持たせない。サーバーレスではインスタンスが入れ替わるためキャッシュが当てにならない。往復も 1 回で済み、登録と挿入が同一トランザクションになる |
| **W1-7** | `status_snapshots.gap` | **列を作らない**。特徴量の段階で `station_attributes` の容量と結合して導出する | 容量は日次同期のデータで有効期間を持つ。収集の最短経路から容量参照を外せる（§10.2） |
| **W1-8** | パーティション運用 | **手書きの plpgsql 保守関数**（pg_partman を使わない） | pg_partman 5.3.1 は利用可能で Supabase も文書化しているが、必要なのは「先の月を作る」「古い月を落とす」の 2 つだけ。`part_config` という設定データを別途マイグレーションで再現する必要があり、`retention_keep_table` の既定が `true`（実際には削除されない）という罠もある。15 行の関数なら pgTAP で完全にテストできる。将来複雑になれば pg_partman に移行できる |
| **W1-9** | TOAST 圧縮 | **既定の pglz のまま**。`lz4` に変更しない | 実測で pglz の方が小さい（58.8 KB 対 75.2 KB）。書き込み量が少なく速度は問題にならない |
| **W1-10** | 予測・モデル系テーブル | **W1 のマイグレーションに含めない** | 使う予定のない空テーブルを先に作らない。W4 で必要になった時点で追加する |
| **W1-11** | 関数の背景実行 | **`after()` も `waitUntil()` も使わない。すべて `await` する** | Vercel は Cron の失敗を再試行しない。`after()` の中で失敗するとステータスコードは 200 のまま失敗が隠れる。処理は 1〜3 秒で、Fluid の既定 300 秒に対して余裕がある。加えて `after()` はリクエストスコープ外で例外になるため単体テストが書けなくなる |
| **W1-12** | 関数のリージョン指定 | **`vercel.json` のトップレベル `regions: ["hnd1"]`**。ルートの `preferredRegion` は使わない | Next.js 16 で `preferredRegion` は非推奨。Vercel 上で許される値は `auto` / `global` / `home` のみで `hnd1` を渡すと例外になる。Node ランタイムではルート単位のリージョン指定は無視される |

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

## 5. PR 分割

6 本の PR に分ける。依存は一直線で、前の PR がマージ済みであることを前提にする。**PR E をマージした時点が収集の本番稼働（M0）** で、それ以前の PR は本番の挙動を変えない。

| # | PR | 主な内容 | 本番への影響 | 想定規模 |
|---|---|---|---|---|
| A | スキーマ v1 | テーブル・RLS・パーティション・バケット | なし（DB にテーブルが増えるだけ） | SQL 約 450 行 |
| B | 取り込み RPC | `ingest_snapshot` ＋ SQL テスト | なし（誰も呼ばない） | SQL 約 260 行＋テスト 200 行 |
| C | `packages/gbfs-core` | GBFS のパース・正規化（純粋関数） | なし（未使用） | TS 約 320 行＋テスト 280 行 |
| D | 収集エンドポイント | `/api/jobs/collect/[system]`（Cron 未登録） | なし（起動されない） | TS 約 330 行＋テスト 220 行 |
| E | Cron ・ウォッチドッグ・監視 | `vercel.json` の `crons`、pg_cron ジョブ | **収集が始まる（M0）** | SQL 約 240 行＋TS 80 行 |
| F | ポート属性の日次同期 | `/api/jobs/sync-stations` | 日次ジョブが増える | TS 約 300 行＋SQL 140 行 |

---

### PR A — Supabase プロジェクト初期化とスキーマ v1

**目的**：収集に必要なテーブル・制約・RLS・パーティション運用・Storage バケットをリモート DB に揃える。

**依存**：§3.1 の 2 つの資格情報（`SUPABASE_ACCESS_TOKEN`、`SUPABASE_DB_PASSWORD`）

**変更ファイル**

```
supabase/config.toml                                   supabase init が生成。project_id を設定
supabase/migrations/20260907000100_extensions.sql      pg_cron, pg_net
supabase/migrations/20260907000200_core_tables.sql     テーブル本体
supabase/migrations/20260907000300_partitions.sql      月次パーティションと保守関数
supabase/migrations/20260907000400_rls.sql             RLS 有効化と権限
supabase/migrations/20260907000500_storage.sql         gbfs-raw バケット
supabase/seed.sql                                      systems 2 行
package.json                                           db:reset / db:push / db:test スクリプト
.github/workflows/ci.yml                               SQL 適用テストのジョブを追加
```

**実装内容**

1. **拡張**：`pg_cron` と `pg_net` を有効化する。どちらも PR E で使うが、拡張の有効化はスキーマ側の前提なのでここで済ませる。
2. **テーブル**：開発プラン §5.3 のうち **収集に必要なものだけ**を作る。`systems` / `stations` / `station_attributes` / `status_snapshots` / `station_status_latest` / `feed_state` / `feed_fetch_log` / `job_runs` / `daily_quality`。予測・モデル系（`station_forecasts`、`model_versions` ほか）は W4 のマイグレーションに回す。
3. **開発プランからの差分（意図的な変更）**
   - `feed_state` に **`last_fetch_at timestamptz`** を追加する。開発プラン §5.4 の重複抑止がこの列を参照しているのに、同 §5.3 の DDL に無かった。
   - `feed_state` に **`last_etag text`** を追加する。ODPT が `ETag` を返すことを実測で確認したため、条件付きリクエストに使う（§4.2 の W1-3）。
   - `status_snapshots` から **`gap` 列を削除**する。理由は §10.2。
   - `stations` に **`last_missing_at timestamptz`** を追加する。「何日観測されていないか」を `is_active` の更新で使うため、最後に「登録済みなのに現れなかった」時刻を持つ。
   - `systems` に **`lock_key smallint not null unique`** を追加する。アドバイザリロックのキーに使う（§10.4）。文書化されていない `hashtext()` に依存しないため。
4. **パーティション**：`status_snapshots` を `observed_at` の月次 RANGE パーティションにする。主キーはパーティションキーを含む必要があるため `(system_id, observed_at)` のままでよい。当月・翌月を先に作り、保守関数 `maintain_status_snapshot_partitions(months_ahead int, retain_days int)` が「先の月を作る」「保持期間より古いパーティションを detach して drop する」を行う。PR E で pg_cron から日次実行する。
5. **RLS と権限**：全テーブルで `enable row level security` を実行し、**ポリシーは 1 つも作らない**。ただし RLS だけでは足りない。このプロジェクトは「新規テーブルの自動公開」を OFF にしているため、**新しいテーブルは `service_role` からも権限が無い状態で作られる**（§4.3 の 1）。マイグレーションで明示的に権限を与える。

   ```sql
   alter table public.status_snapshots enable row level security;   -- ポリシーは作らない
   revoke all on table public.status_snapshots from anon, authenticated;
   grant select, insert, update, delete on table public.status_snapshots to service_role;
   grant usage, select on all sequences in schema public to service_role;
   ```

   これで匿名・認証済みロールからは一切見えず、`service_role`（RLS をバイパスし、かつ権限を持つ）だけが読み書きできる。公開用の読み取りビューは `/v1/stations` を作る W2 に追加する。
6. **Storage**：`gbfs-raw` バケットを非公開で作る。ダッシュボード操作ではなく SQL で作り、環境の再現性を保つ。
7. **seed**：`systems` に 2 行（`hellocycling` / `docomo-cycle`）。表示名・提供者名・データセット名・ライセンス URL・期待周期・`lock_key` は `packages/shared` の定数と同じ値にする。
8. **`vercel.json`**：トップレベルに `regions: ["hnd1"]` を追加し、`functions` の設定はサービスの中に入れる（Services 構成では `functions` をトップレベルに置けない）。ルートの `preferredRegion` は使わない（§4.2 の W1-12）。
9. **CLAUDE.md への追記**：Vercel Services の中では **Middleware が使えない**（`middleware.ts` があるとデプロイが拒否される）。認証はルートハンドラ内で行う制約を §3 のコーディング規約に 1 行足す。

**テスト**

- `supabase db reset` がローカルで通る（Docker 上の Postgres に全マイグレーション＋seed を適用）
- SQL テスト（pgTAP 1.3.3 が利用可能なことを確認済み）：主要テーブルの存在、全テーブルで RLS が有効、**`service_role` に必要な権限があること**、`systems` が 2 行、当月と翌月のパーティションが存在、保守関数を実行すると翌々月が増え古いパーティションが消える

**注意**：`supabase init` は `supabase/migrations/` を作らない。最初の `supabase migration new` で作られる。`supabase db diff` は Storage バケットを検出しないため、バケット作成の SQL は手書きする。

**検証手順**

```bash
brew install supabase/tap/supabase
pnpm db:reset            # ローカルに適用
pnpm db:test             # pgTAP
pnpm db:push             # リモート（本番プロジェクト）に適用
```

**完了条件**

- リモートに 9 テーブルが存在し、すべて RLS 有効
- `systems` が 2 行
- 当月・翌月のパーティションが存在
- `gbfs-raw` バケットが非公開で存在
- CI の SQL ジョブが緑

---

### PR B — 取り込み RPC と SQL テスト

**目的**：§10.3 の契約を満たす `ingest_snapshot` を実装し、冪等性・同時実行安全性・欠損の扱いをテストで固定する。

**依存**：PR A

**変更ファイル**

```
supabase/migrations/20260908000100_ingest_snapshot.sql
supabase/tests/ingest_snapshot.test.sql
```

**実装内容**

- `public.ingest_snapshot(...)` を `security definer` で作る。`set search_path = ''` を付けてすべての識別子を `public.` などで完全修飾する。`revoke execute from public, anon, authenticated` した上で `service_role` にだけ `grant execute` する。
- **`set statement_timeout to '30s'` を関数定義に付ける**。`service_role` はロール既定で 8 秒に縛られるが、PostgREST は関数の設定をトランザクションに持ち上げるため、これで 30 秒まで許容される（§4.2 の W1-2）。実測は定常 0.45 秒・初回 0.65 秒なので通常は問題にならないが、初回や異常時の保険になる。
- **戻り値は `jsonb` のスカラにする**。集合を返すと PostgREST の `max_rows`（1000）で警告なく切り詰められる。
- マイグレーションの末尾に **`notify pgrst, 'reload schema';`** を置く。これを忘れると REST から関数が 404 になる。
- 内部で `pg_try_advisory_xact_lock(8421, systems.lock_key)` を取る（§10.4）。**セッション単位ではなくトランザクション単位**を使う理由は、Supavisor のトランザクションモードではセッションが接続プールに返るため、セッション単位のロックが意図した範囲で保持されないこと。キーは文書化されていない `hashtext()` ではなく `systems.lock_key` 列から取る。
- 未登録の `station_id` を `stations` に登録し、`idx` を `coalesce(max(idx) + 1, 0)` から連番で採番する。採番は上のロックの内側で行うため競合しない。
- 登録済み全ポート分の密な配列を組み立てる。入力に現れなかったポートは `-1`。
- `status_snapshots` に `on conflict (system_id, observed_at) do nothing` で挿入し、挿入されたかどうかで `inserted` / `duplicate` を返す。
- `station_status_latest` は `is distinct from` で**変化した行だけ**更新する。無変更行を書かないことで WAL と bloat を抑える。
- 引数配列の長さが揃っていなければ例外にする（呼び出し側のバグを早期に検出する）。

**テスト（pgTAP）**

| 観点 | 期待 |
|---|---|
| 初回投入 | `idx` が 0 から密に振られる。配列長 = ポート数 |
| 同じ `observed_at` を 2 回 | 2 回目は `duplicate`、`status_snapshots` は 1 行のまま |
| 新ポートの追加 | 既存の `idx` は不変。配列が 1 伸びる |
| 消えたポート | 該当位置が `-1`。`stations` の行は残る |
| 変化行のみ更新 | 2 回目の投入で `n_changed` が実際の変化数と一致 |
| 配列長の不一致 | 例外 |
| `flags` のビット | `1|2|4` の組み合わせが往復する |
| 権限 | `anon` から `execute` できない |

**完了条件**：pgTAP が全通過し、リモートに適用済み。`select public.ingest_snapshot(...)` を手で叩いて `{"status":"inserted"}` が返る。**実データ 14,861 件で初回 1 秒以内・定常 0.6 秒以内**（試作時の実測は 0.65 / 0.45 秒）。Data API 経由でも `service_role` のキーで呼べる。

---

### PR C — `packages/gbfs-core`（GBFS のパースと正規化）

**目的**：フィード JSON を RPC の引数の形に変換する純粋関数を作る。副作用は持たせない。

**依存**：なし（PR A・B と並行して進めてよい）

**変更ファイル**

```
packages/gbfs-core/package.json / tsconfig.json
packages/gbfs-core/src/schema.ts              GBFS 2.3 の Zod スキーマ
packages/gbfs-core/src/parse.ts               parseStationStatus
packages/gbfs-core/src/normalize.ts           dedupe / flags / reportedAge / clamp
packages/gbfs-core/src/index.ts
packages/gbfs-core/src/*.test.ts
fixtures/gbfs/2026-09-04/*.json               実フィードを 50 ポートに縮約
scripts/fetch-fixtures.ts                     フィクスチャの再生成スクリプト
```

**実装内容**

- **Zod スキーマ**：GBFS 2.3 の `station_status` と `station_information`。**Zod v4 の `z.looseObject()` を使う**（未知フィールドを保持する。v3 の `.passthrough()` は非推奨）。HELLO の文字列 `vehicle_capacity` のような非標準値はスキーマ側で数値に正規化する。v4 で変わった点として、`z.number().int()` は `z.int()`、`z.string().datetime()` は `z.iso.datetime()` を使う。踏みやすい罠が 3 つある。裸の `z.unknown()` はキーが必須になるため `.optional()` を付ける、`z.record()` は引数 2 つの形しか無い、`z.coerce.*` はキーが無いとエラーになる。
- **`parseStationStatus(json)`**：`last_updated` / `ttl` / `version` と、ポート配列を取り出す。
- **`dedupeStations(stations)`**：ドコモに実在する完全重複行（10 件）を除く。内容が一致しない重複は警告として返し、呼び出し側がログに残せるようにする。
- **`toIngestArrays(stations, observedAt)`**：`station_ids` と並行する 4 本の配列を返す。`flags` はビット和、`reported_age_s` は `observedAt - last_reported` を秒で計算し `smallint` の範囲に丸める。
- **値のクランプ**：`bikes` / `docks` が負値や 32767 超なら丸めてフラグを立てる。実データでは起きないが、上流の変化で壊れないようにする。

**テスト**

- 縮約フィクスチャで両システムをパースできる
- ドコモの重複 ID が除かれる
- `flags` のビットが往復する
- `reported_age_s`：正常値、負値、上限超え、ドコモの 0
- 空の `stations`、未知フィールドの保持、`last_updated` の後退
- **性能**：14,861 件を合成した入力で `parse` ＋ `toIngestArrays` が 150 ms 未満（Vercel の Active CPU 予算を守るため）

**フィクスチャの方針**：実フィードを 50 ポートに縮約したものを `fixtures/gbfs/2026-09-04/` に置く（合計 55 KB 程度）。時系列の挙動（重複排除、変化検出）は、フィクスチャを手で加工した明示的なケースで検証する。生の大容量フィードはリポジトリに置かない。

**完了条件**：テスト通過、CI 緑、`packages/shared` と責務が重複していない（定数は `shared`、GBFS の解釈は `gbfs-core`）。性能テストが 150 ms 未満で通る（実測値は 69 ms）。

**テスト環境の注意**：Vitest の環境は既定の `node` のままにする。`jsdom` にすると `Request` が未定義になりルートハンドラのテストが壊れる。`next/server` は追加設定なしで import できることを確認済み。

---

### PR D — 収集エンドポイント（Cron はまだ登録しない）

**目的**：手動で叩けば 1 スナップショットが保存される状態にする。`vercel.json` は変更しないため、マージしても何も起動しない。

**依存**：PR A・B・C

**変更ファイル**

```
apps/web/app/api/jobs/collect/[system]/route.ts
apps/web/lib/env.ts            環境変数の検証（起動時に 1 回）
apps/web/lib/cron-auth.ts      CRON_SECRET のタイミング安全比較
apps/web/lib/odpt.ts           フィード取得・フォールバック・タイムアウト
apps/web/lib/storage.ts        gzip して Storage へ
apps/web/lib/ingest.ts         RPC 呼び出し
apps/web/lib/*.test.ts
```

**実装内容**：開発プラン §5.4 の手順どおり。副作用は `lib/` の各モジュールに閉じ込め、ルートハンドラは組み立てだけを行う。

- 認証：`Authorization: Bearer <CRON_SECRET>` をタイミング安全に比較する。不一致は 401。
- 重複抑止：`feed_state.last_fetch_at` が **30 秒以内**なら `skipped_recent` で終了する。ウォッチドッグの発火は 150 秒後なので衝突しない。
- 取得：認証付き URL を先に試し、4xx/5xx/タイムアウトなら公開 URL にフォールバックする。`AbortSignal.timeout(20_000)`。
- **条件付きリクエスト**：`feed_state.last_etag` があれば `If-None-Match` を付ける。**304 が返ったら本文を読まずに `unchanged` で終了**する（HELLO では 5 回に 4 回がこれになる）。フォールバックで別のエンドポイントに切り替えた場合は ETag が一致しないことがあるが、その場合は通常の 200 が返るだけで害はない。
- 変化判定：304 でなければ `last_updated` を `feed_state.last_observed_at` と比較し、同じなら `unchanged` で終了して `feed_fetch_log` にだけ記録する。
- 保存：生 JSON を gzip して Storage へ（既存ならスキップ）。その後 `ingest_snapshot` を呼ぶ。**Storage を先**にするのは、DB に行があるのに生データが無い状態を作らないため。アップロードは `contentType: 'application/gzip'` を**必ず指定**する（省略すると `text/plain` になる）。`upsert` は既定の `false` のままにし、パスに `observed_at` を含めて毎回別パスにする。
- **背景実行は使わない**。`after()` や `waitUntil()` に処理を逃がすと、失敗してもステータスコードが 200 のままになり、Vercel は Cron を再試行しないため失敗が永久に埋もれる。すべて `await` してから応答を返す（§4.2 の W1-11）。
- **エラー処理**：`statement_timeout` 超過は 504 ではなく 500（`57014`）で返る。413 のときは JSON でなく HTML が返るため、`Content-Type` を確認してから JSON をパースする。
- ログ：成否に関わらず `feed_fetch_log` に 1 行入れる。**URL とトークンは絶対に書かない**（`endpoint` に `'token'` / `'public'` の別だけ残す）。

**テスト**

- 認証失敗で 401、未知の `system` で 400
- 304 が返ったときに本文を読まず `unchanged` になる
- `unchanged` の判定（304 と `last_updated` 一致の両方）
- io をモックした正常系で、RPC に渡る配列の長さと順序が一致する
- 例外時のメッセージに URL・トークンが含まれない（正規表現で検査）
- タイムアウトが `AbortSignal` で発火する（`error.name === 'TimeoutError'`）
- Storage への `contentType` が `application/gzip` である

**検証手順**

```bash
# ローカル（ローカル Supabase ＋ 本物の ODPT）
pnpm dev
curl -H "Authorization: Bearer $CRON_SECRET" localhost:3000/api/jobs/collect/hellocycling

# プレビューデプロイ（本番 Supabase に 1 件だけ入れる）
curl -H "Authorization: Bearer $CRON_SECRET" "$PREVIEW_URL/api/jobs/collect/docomo-cycle"
```

**完了条件**：手動起動でスナップショットが 1 件、Storage にファイルが 1 つ。続けて叩くと `unchanged` か `skipped_recent` が返り、行が増えない。

---

### PR E — Cron 有効化・ウォッチドッグ・監視（M0）

**目的**：収集を本番で連続稼働させる。**この PR のマージが M0** なので、マージ前に PR D の手動検証を必ず終えておく。

**依存**：PR D

**変更ファイル**

```
vercel.json                                        crons を 2 本追加
supabase/migrations/20260909000100_cron_jobs.sql   pg_cron のジョブ登録
supabase/migrations/20260909000200_monitoring.sql  監視関数と通知
scripts/setup-vault.ts                             .env から Vault へシークレット登録
package.json                                       setup:vault スクリプト
```

**実装内容**

1. **Vercel Cron**：`/api/jobs/collect/hellocycling` と `/api/jobs/collect/docomo-cycle` を `* * * * *` で登録する。
2. **Vault**：`CRON_SECRET` と `PROJECT_BASE_URL`、（あれば）`ALERT_WEBHOOK_URL` を登録する。値をマイグレーションに書くとリポジトリに残るため、`.env` を読むローカルスクリプトで投入する。
3. **ウォッチドッグ（毎分）**：`feed_state.last_fetch_at` が 150 秒より古いシステムがあれば、Vault の `CRON_SECRET` を `Authorization` ヘッダに載せて `net.http_post` で同じエンドポイントを叩く。Vercel Cron の配信漏れをプラットフォーム内で補完する。`net.http_post` は**必ず名前付き引数**で呼び（`http_get` と引数順が違う）、`timeout_milliseconds := 10000` を明示する（既定は 2000 ms）。`pg_net` の送信はトランザクションのコミット後なので、SQL Editor で試すときは commit すること。

   ```sql
   select net.http_post(
     url := (select decrypted_secret from vault.decrypted_secrets where name = 'project_base_url')
            || '/api/jobs/collect/' || s.system_id,
     headers := jsonb_build_object(
       'Content-Type', 'application/json',
       'Authorization', 'Bearer ' || (select decrypted_secret from vault.decrypted_secrets where name = 'cron_secret')),
     timeout_milliseconds := 10000
   )
   from public.feed_state s
   where s.last_fetch_at < now() - interval '150 seconds';
   ```
4. **保守ジョブ（日次）**：パーティションの作成と削除、`feed_fetch_log` の 30 日超削除、**`cron.job_run_details` の 7 日超削除**（自動削除されず毎分ジョブで月 4.3 万行たまる）、`stations.is_active` の更新（72 時間未観測で false）。スケジュールは **UTC で書く**（pg_cron のタイムゾーンは変更できない。`'0 19 * * *'` が 04:00 JST）。ジョブ名は定数として管理する（`cron.schedule('名前', ...)` は同名を上書きするため冪等になる。ただし大文字小文字の違いは別ジョブになる）。有効・無効の切替は `cron.alter_job()`、削除は `cron.unschedule()` を使う（Supabase では `postgres` が `cron.job` に SELECT しかできない）。
5. **監視（10 分毎）**：開発プラン §5.5 の表のうち、W1 で意味のあるもの（フィード停滞、収集器の停止、取得失敗率、ポート数の急変、値の異常、DB サイズ）を SQL で判定し、該当があれば Webhook に通知する。**同じ事象を繰り返し通知しない**よう、直近の通知内容をテーブルに残して抑制する。
6. **日次 QA（07:00 JST）**：`daily_quality` に前日分を書き、要約を Webhook に送る。

**検証手順**

1. **マージ前に Cron の登録を確認する**：`vercel build --prod` をローカルで実行し、`.vercel/output/config.json` に `crons` が入っていることを確認する。
2. **マージ直後に Deployment Protection の影響を確認する**（下の「最大のリスク」を参照）。2 分以内に `feed_fetch_log` に行が入らなければ、`vercel crons run /api/jobs/collect/hellocycling` で手動起動し、ログのステータスコードを見る。401 や 302 なら Cron が保護に阻まれている。
3. マージ後 10 分で `feed_fetch_log` が 20 行前後になる
4. ウォッチドッグを強制的に発火させる（`feed_state.last_fetch_at` を 5 分前に書き換え、1 分待って再取得されることを確認）
5. 通知を強制的に発火させる（閾値を一時的に厳しくする、または監視関数を直接呼ぶ）

**最大のリスク：Deployment Protection が Cron を弾く可能性**

Vercel Cron は「本番デプロイの URL」に GET する。このプロジェクトでは本番ドメイン `bike-chance.vercel.app` は公開（実測 200）だが、生成デプロイ URL は Vercel の SSO に 302 する（実測）。Cron がどちらを叩くかは公式文書からは断定できず、**Cron が保護を免除されるという明文の記載も無い**。Cron はリダイレクトを追わないため、もし生成 URL を叩いていれば 302 で終わり、ジョブは一度も実行されない。

対処は 3 段階で用意する。

| 段階 | 対処 |
|---|---|
| 1 | 上の手順 2 で実際に動くかを確認する。動けば何もしない |
| 2 | 動かなければ、Deployment Protection の適用範囲を確認し、本番が保護されない設定（Standard Protection）になっているかを見る |
| 3 | それでも動かなければ、**ウォッチドッグを恒常的なスケジューラに切り替える**。pg_cron を毎分実行にし、`net.http_post` で本番ドメイン `https://bike-chance.vercel.app/api/jobs/collect/{system}` を直接叩く。Vercel Cron を使わずに収集が回る。この経路は本番ドメイン宛なので保護の影響を受けない |

段階 3 の仕組みは PR E で作るウォッチドッグそのものなので、**追加実装なしで切り替えられる**。この冗長性が、ウォッチドッグを W1 に入れる二番目の理由でもある。

**完了条件（M0）**：両システムのスナップショットが期待周期で増え続け、Storage にも対応するファイルが増える。24 時間の観測を開始する。

---

### PR F — ポート属性の日次同期

**目的**：`station_information` から台帳を SCD Type 2 で更新し、ポートの新設・廃止・改名・容量変更を履歴として残す。

**依存**：PR E（収集が動いていること）

**変更ファイル**

```
apps/web/app/api/jobs/sync-stations/route.ts
packages/gbfs-core/src/station-information.ts ＋ テスト
supabase/migrations/20260910000100_upsert_station_attributes.sql
vercel.json                                    日次 cron を追加
```

**実装内容**

- `gbfs.json` / `system_information` / `station_information` / `vehicle_types` の 4 本を取得する。
- 属性を正規化する。HELLO の文字列 `vehicle_capacity` を数値に、`rental_uris` を 3 列に展開、日本の BBox 外なら `geo_suspect` を立てる（ドコモに実在する経度 39.55 のポートが該当する）。
- RPC `upsert_station_attributes` で SCD2 更新する。既存の有効行と比較し、**内容が変わった行だけ** `valid_to` を閉じて新しい行を挿入する。
- フィード構成の変化（feed の増減、`version` の変化、`vehicle_types` の増減）を検知したら通知する。GBFS 3.0 への移行を見逃さないための仕掛け。
- 生の `station_information` も Storage に保存する（§10.5 のパス規約）。
- `pref_code` / `muni_code` は **NULL のまま**にする。行政区域データの取り込みは W3。

**テスト**

- 初回実行で全ポート分の行が入る
- 2 回目の実行で新規行が 0（`valid_from` が変わらない）
- 名前・容量・座標のいずれかが変わった行だけ新行になる
- BBox 外の座標に `geo_suspect` が立つ
- `vehicle_capacity` が文字列でも数値でもパースできる

**完了条件**：実行後 `station_attributes` の有効行が約 20,661 行。2 回目の実行で新規行が 0。日次 Cron が登録されている。

## 6. スケジュール

各 PR の想定所要は「実装 → ローカル検証 → PR 作成 → CI とプレビュー確認 → マージ」までを含む。

| 日 | PR | 内容 | 終了時点の状態 |
|---|---|---|---|
| 9/7（月）午前 | 事前準備 | Supabase CLI、`link`、ローカル起動 | マイグレーションを流せる |
| 9/7（月）午後 | **PR A** | スキーマ v1（テーブル・RLS・パーティション・バケット） | リモート DB に空のテーブルが揃う |
| 9/8（火）午前 | **PR B** | 取り込み RPC ＋ SQL テスト | DB 側の書き込み契約が確定 |
| 9/8（火）午後 | **PR C** | `packages/gbfs-core`（純粋関数） | フィードを配列に変換できる（単体テスト済み） |
| 9/9（水）午前 | **PR D** | 収集エンドポイント（Cron 未登録） | 手動起動で 1 スナップショット保存できる |
| 9/9（水）午後 | **PR E** | Cron 有効化・ウォッチドッグ・監視 | **M0 収集の本番稼働** |
| 9/10（木） | **PR F** | ポート属性の日次同期 | 台帳が毎日更新される |
| 9/10（木）〜9/11（金） | 検証 | 24 時間運転の観測、QA レポート | **M1 QA 合格** |
| 9/11（金）午後 | 予備 | 不具合対応、開発プランの更新 | W2 に入れる状態 |

**バッファの考え方**：9/10 と 9/11 は意図的に軽くしてある。PR A〜E のどこかで想定外（Vercel Services と Cron の相互作用、pg_net の権限、PostgREST のペイロード上限など）に当たった場合、ここで吸収する。逆に順調なら 9/10 から W2 の Parquet 圧縮に着手してよい。

**土日に前倒しする場合**：PR A と PR C は他への依存がないため、9/5〜9/6 に着手できる。M0 が前倒しになれば、それだけ学習データが増える。

## 7. 24 時間検証の手順と合格判定

PR E をマージした翌日に実行する。すべて Supabase の SQL Editor か `psql` で実行できる。

### 7.1 取得率と欠損

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

**合格ライン**：取得率が両システムとも 99.5% 以上。欠損区間の最大が 30 分未満。

### 7.2 重複と冪等性

```sql
-- 重複行はゼロであるべき（PK があるため理論上 0。念のため確認する）
select system_id, observed_at, count(*)
from status_snapshots
where observed_at >= now() - interval '24 hours'
group by 1, 2 having count(*) > 1;

-- Cron の二重配信・ウォッチドッグ起動が何回あったか
select
  count(*) filter (where is_new_snapshot) as new_snapshots,
  count(*) filter (where not is_new_snapshot and error is null) as deduped,
  count(*) filter (where error is not null) as errors,
  count(*) as total_calls,
  round(100.0 * count(*) filter (where error is not null) / count(*), 2) as error_pct
from feed_fetch_log
where fetched_at >= now() - interval '24 hours';
```

**合格ライン**：重複行 0 件。エラー率 1% 未満。`deduped` が多いのは正常（HELLO は 5 回に 4 回が重複）。

### 7.3 生データとの整合

```sql
-- Storage に保存したパスが全スナップショットに存在するか
select count(*) as snapshots_without_raw
from status_snapshots
where observed_at >= now() - interval '24 hours'
  and (raw_path is null or raw_path = '');
```

Storage 側の実ファイル数は、`gbfs-raw/{system}/{YYYY}/{MM}/{DD}/` を CLI か API で数えて突き合わせる。

**合格ライン**：`snapshots_without_raw` が 0。ファイル数がスナップショット数と一致。

### 7.4 容量とコスト

```sql
-- テーブル別サイズ（TOAST 込み）
select relname,
       pg_size_pretty(pg_total_relation_size(c.oid)) as total,
       pg_size_pretty(pg_relation_size(c.oid)) as heap,
       pg_size_pretty(pg_total_relation_size(reltoastrelid)) as toast
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relkind in ('r', 'p')
order by pg_total_relation_size(c.oid) desc limit 15;

-- DB 全体
select pg_size_pretty(pg_database_size(current_database())) as db_size;
```

Vercel 側は Observability の Active CPU と Invocations を確認する。

**合格ライン**：DB 増分 40 MB/日以下（実測ベースの期待値は 22.7 MB/日）。収集の Active CPU が 1 回 0.3 秒以下。

### 7.5 値の健全性

```sql
-- 直近スナップショットの配列長がポート数と一致し、値域が妥当か
select s.system_id, s.observed_at, s.n_stations,
       array_length(s.bikes, 1) as len_bikes,
       array_length(s.docks, 1) as len_docks,
       array_length(s.flags, 1) as len_flags,
       (select count(*) from stations st where st.system_id = s.system_id) as registered,
       (select min(v) from unnest(s.bikes) v) as min_bikes,
       (select max(v) from unnest(s.bikes) v) as max_bikes
from status_snapshots s
where s.observed_at = (select max(observed_at) from status_snapshots x where x.system_id = s.system_id);
```

**合格ライン**：配列長がポート数と一致。`min_bikes` は -1（未観測）以上、`max_bikes` は 200 未満。

## 8. 障害時の対応とロールバック

| 事象 | 一次対応 | 備考 |
|---|---|---|
| 収集器が失敗し続ける | Vercel の Cron 設定画面で該当ジョブを **Disable**。または `vercel.json` から `crons` を外して再デプロイ | Disable してもジョブ数の上限にはカウントされる |
| pg_cron のジョブを止めたい | `select cron.unschedule('<jobname>');` | ジョブ名を固定して登録しておく |
| マイグレーションを間違えた | **down は書かない**。修正内容を新しいマイグレーションとして追加する | `supabase db push` は前進のみ。既存の適用済みマイグレーションは書き換えない |
| デプロイを戻したい | Vercel の Instant Rollback | **Cron 設定は旧デプロイに戻らない**。`vercel.json` の変更を戻す場合は再デプロイが必要 |
| 誤ってデータを消した | Supabase の日次バックアップ（7 日保持）から復元。`status_snapshots` は Storage の生 JSON から再構築できる（再構築スクリプトは W2） | 生 JSON が一次ソースである理由 |
| ODPT 側が落ちた | 何もしない。欠損として記録し、補間はしない | `feed_fetch_log.error` に残る |

**PR ごとのロールバック可否**

| PR | main へマージ後の巻き戻し |
|---|---|
| A・B（SQL のみ） | 前進マイグレーションで修正。テーブルが空のうちは `drop table` を含む修正も安全 |
| C（純粋関数のみ） | 呼び出し元がないため影響なし |
| D（エンドポイント追加、Cron 未登録） | 誰も呼ばないため影響なし |
| E（Cron 有効化） | **ここが本番稼働の分岐点**。Cron を Disable すれば収集が止まるだけで、既存データは無傷 |
| F（日次同期） | Cron を Disable。`station_attributes` は追記のみなので既存行は壊れない |

## 9. W1 では作らないもの

範囲を絞るために、次は意図的に W1 の外に置く。開発プラン §13 の該当週で扱う。

| 項目 | 実施週 | 理由 |
|---|---|---|
| Parquet 圧縮ジョブ（`/ml/compact`）と Python サービス | W2 | 収集が動いていないと圧縮するものがない |
| ホットストア再構築スクリプト | W2 | 同上 |
| 座標 → 都道府県・市区町村（`pref_code` / `muni_code`） | W3 | 国土数値情報の取り込みが別作業。カラムは W1 で用意し NULL のままにする |
| バックアップ収集器（Supabase Edge Function） | W3 | まず単一系＋ウォッチドッグの欠損率を測ってから判断（開発プラン §5.7） |
| 予測・モデル関連テーブル（`station_forecasts` 等） | W4 | W1 のマイグレーションには含めない。空テーブルを先に作らない |
| 天気・祝日の取り込み | W4 | 特徴量の作業 |
| 近傍リスト・履歴プロファイル | W3 | 特徴量の作業 |
| pgTAP 以外のカバレッジ計測、負荷試験 | 未定 | 規模的に不要 |

## 10. 付録：PR をまたぐ契約

PR B（DB 側）と PR C・D（アプリ側）が同じものを作るために、境界の意味をここで固定する。実装中にここを変える場合は、両方の PR に反映し本文書を更新する。

### 10.1 配列の意味

各システムには、ポートごとに **不変・密・0 起点の整数インデックス `idx`** を割り当てる。スナップショット行の配列は `idx` 順に並び、**Postgres の配列は 1 起点なので `idx` のポートの値は `arr[idx + 1]`** にある。

配列の長さは、そのスナップショットを取り込んだ時点で登録済みのポート数（`max(idx) + 1`）。過去の行は当時の長さのままで、後から伸ばさない。

| 列 | 型 | 値 | 欠損時 |
|---|---|---|---|
| `bikes` | `smallint[]` | `num_bikes_available` | `-1` |
| `docks` | `smallint[]` | `num_docks_available` | `-1` |
| `flags` | `smallint[]` | ビット和：`1`=`is_installed`、`2`=`is_renting`、`4`=`is_returning`（すべて真なら `7`） | `-1` |
| `reported_age_s` | `smallint[]` | `observed_at - last_reported` の秒数。負値と 32767 超は丸める | `-1` |

「欠損」は、そのポートが登録済みなのに今回のフィードに現れなかった場合を指す。ドコモではポート ID が数件単位で出入りするため（開発プラン §3.4）、日常的に発生する。

`reported_age_s` はドコモでは常に 0 になる（全ポートが同一の `last_reported` を返すため）。列は共通に持ち、意味の違いは特徴量側で扱う。

### 10.2 `gap` を保存しない理由（W1 の設計変更）

開発プラン §5.3 では `status_snapshots` に `gap`（`capacity − bikes − docks`）列を置いていたが、**W1 では作らない**。

- `capacity` は `station_information` 由来で、日次同期（PR F）で `station_attributes` に有効期間つきで入る。収集器は毎分動くため、容量を持つには古い値をキャッシュするしかない
- 特徴量を作る時点で `station_attributes` の**その時刻に有効だった容量**と結合すれば、常に正しい `gap` が得られる
- 収集の最短経路から容量参照が消え、収集器が状態を持たなくなる

`gap` は開発プラン §6.3 の特徴量として、W3 の特徴量パイプラインで導出する。監視の「bikes + docks > capacity」も同じ結合で確認する。

### 10.3 取り込み RPC の契約

**`public.ingest_snapshot(...) returns jsonb`**

クライアントは「ポート ID の配列」と「それに並行する値の配列」を渡す。`idx` への変換と未知ポートの登録は DB 側で行う。クライアントが `idx` の対応表を保持しないため、サーバーレスでインスタンスが入れ替わっても壊れない。

```sql
create function public.ingest_snapshot(
  p_system_id      text,
  p_observed_at    timestamptz,   -- フィードの last_updated
  p_fetched_at     timestamptz,   -- 取得した時刻
  p_station_ids    text[],        -- フィードに現れたポート ID（重複排除済み）
  p_bikes          smallint[],    -- 以下 4 つは p_station_ids と同じ長さ・同じ順序
  p_docks          smallint[],
  p_flags          smallint[],
  p_reported_age_s smallint[],
  p_raw_path       text           -- Storage 上の gzip JSON のパス
) returns jsonb
```

処理内容（1 トランザクション）

1. `pg_try_advisory_xact_lock(8421, systems.lock_key)` でシステム単位の排他を取る。取れなければ `{"status":"locked"}` を返す
2. `p_station_ids` のうち未登録のものを `stations` に登録し、`idx` を採番する
3. 登録済み全ポート分の密な配列を組み立てる（現れなかったポートは `-1`）
4. `status_snapshots` に挿入。`(system_id, observed_at)` が既にあれば何もしない
5. 変化のあったポートだけ `station_status_latest` を更新する
6. `stations.last_seen_at` と `feed_state` を更新する

戻り値（`jsonb`）

```json
{ "status": "inserted", "n_stations": 14861, "n_new_stations": 3,
  "n_changed": 341, "array_length": 14861 }
```

`status` は `inserted` / `duplicate`（同じ `observed_at` が既にある）/ `locked` のいずれか。呼び出し側はこの値をログに記録し、`duplicate` と `locked` は正常系として扱う。

**呼び出し方は 2 通りあり、どちらでも同じ関数を呼ぶ**。PostgREST 経由（`supabase-js` の `.rpc()`）でも、Supavisor 経由の直接接続（`select public.ingest_snapshot($1, ...)`）でも動く。どちらを採用するかは §4 の設計判断で決める。

### 10.4 アドバイザリロックのキー

同時実行の抑止に使うキーは、`hashtext()` のような文書化されていない内部関数に依存させない。`systems` テーブルに **`lock_key smallint not null unique`** を持たせ（`hellocycling` = 1、`docomo-cycle` = 2）、2 引数形式の `pg_try_advisory_xact_lock(class_id, object_id)` を使う。

| 用途 | `class_id` | `object_id` |
|---|---|---|
| 取り込み（`ingest_snapshot`） | 8421 | `systems.lock_key` |
| 属性同期（`upsert_station_attributes`） | 8422 | `systems.lock_key` |
| パーティション保守 | 8423 | 0 |

アドバイザリロックのキー空間はデータベース全体で共有されるため、`class_id` で用途を分けて衝突を防ぐ。`class_id` の値はこの表でのみ管理し、マイグレーションにコメントとして残す。

**トランザクション単位を使う理由**：Supavisor のトランザクションモードでは、トランザクションが終わると接続がプールに返る。セッション単位の `pg_try_advisory_lock` はセッションが続く限りロックを保持するため、プール越しでは「どのセッションが持っているか」が実行のたびに変わり、意図した排他にならない。`pg_try_advisory_xact_lock` はコミット・ロールバック時に必ず解放されるため、プール構成でも安全に使える。

### 10.5 Storage のパス規約

```
gbfs-raw/{system_id}/{YYYY}/{MM}/{DD}/station_status_{observed_at_epoch}.json.gz
gbfs-raw/{system_id}/{YYYY}/{MM}/{DD}/station_information_{fetched_at_epoch}.json.gz
```

- 日付は **UTC**。Parquet のパーティションも UTC に揃え、JST への変換は特徴量側で明示的に行う
- `observed_at_epoch` はフィードの `last_updated`（POSIX 秒）。同じ観測を二重に保存しない
- 既に同じパスがあればアップロードしない（`upsert: false` とし、重複エラーは正常系として握りつぶす）
- バケットは非公開。読み出しはサービスロールまたは S3 互換の資格情報経由のみ

### 10.6 収集エンドポイントの応答

`GET /api/jobs/collect/{system}` は常に 200 を返し、結果は本文で表す。Cron のログに残す目的なので、例外時も 500 ではなく 200 とし、`ok: false` で示す。ただし認証失敗だけは 401 を返す。

```json
{ "ok": true, "system": "hellocycling", "result": "inserted",
  "observed_at": "2026-09-07T04:31:34.000Z", "n_stations": 14861,
  "n_new_stations": 0, "n_changed": 341, "bytes": 4204793, "duration_ms": 1284 }
```

`result` は `inserted` / `unchanged`（`last_updated` が前回と同じ）/ `skipped_recent` / `locked` / `error` のいずれか。`error` の場合は `message` に文脈を入れるが、**URL とトークンは含めない**。

### 10.7 環境変数の一覧（W1 時点）

| 変数 | ローカル `.env` | Vercel（Production） | Supabase Vault | 用途 |
|---|---|---|---|---|
| `ODPT_ACCESS_TOKEN` | ○ | ○ | — | GBFS の認証付き取得 |
| `SUPABASE_URL` | ○ | ○ | — | Storage と PostgREST |
| `SUPABASE_SECRET_KEY` | ○ | ○ | — | サーバーからの書き込み |
| `SUPABASE_DB_URL` | ○ | ○ | — | 直接接続（Supavisor、6543） |
| `CRON_SECRET` | ○ | ○ | ○ | Cron 認証。Vault の値はウォッチドッグが使う |
| `CONTACT_EMAIL` | ○ | ○ | — | ODPT 通知文 |
| `SUPABASE_PUBLISHABLE_KEY` | ○ | — | — | 現時点で未使用 |
| `SUPABASE_PROJECT_REF` | ○ | — | — | `supabase link` |
| `SUPABASE_ACCESS_TOKEN` | ○（**貼り付け待ち**） | — | — | CLI の非対話実行。権限は §3.1 の表のとおり |
| `SUPABASE_DB_PASSWORD` | ○（設定済み） | — | — | `supabase link` / `db push`。`SUPABASE_DB_URL` から導出済み |
| `ALERT_WEBHOOK_URL` | ○（任意） | — | ○ | 監視通知 |
| `PROJECT_BASE_URL` | — | ○ | ○ | ウォッチドッグが叩く本番 URL |

## 11. 本文書の根拠にした一次情報

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
