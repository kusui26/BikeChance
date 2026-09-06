# CLAUDE.md — BikeChance 開発指針

このファイルは **BikeChance** リポジトリで Claude Code が作業するときの指針を提供するものです。設計の詳細・根拠・決定記録は **`docs/260904_dev_plan.md`（開発プラン）** にあり、本ファイルと食い違う場合はプランの §15（決定記録）を正として本ファイルを直す。

---

## 1. プロジェクトの概要

- **プロダクト**：シェアサイクル（HELLO CYCLING・ドコモ・バイクシェア）のポートに「**到着した時刻に借りられる／返せる確率**」を降水確率のように示す iPhone アプリ（後にスマホ Web）。差別化は予測・確率・行程チェック。現在台数の表示は前提機能。
- **データ**：ODPT の GBFS v2.3。`hellocycling`（14,861 ポート、約 5 分毎更新）と `docomo-cycle`（5,800 ポート、約 80 秒毎更新）。ライセンスは CC BY 4.0（商用可、クレジット表示必須）。予約を表すフィールドは無く、HELLO の `capacity − bikes − docks`（`gap`）を予約の代理指標にする。ドコモの `capacity` は固定ラック数ではなく `bikes + docks` の動的値。
- **構成（Vercel Pro と Supabase Pro の 2 つに集約）**
  - **Vercel Pro**（1 プロジェクト、Services、東京 `hnd1`）：`apps/web` = Next.js（公開 API `/v1`、Cron ジョブ `/api/jobs/*`（毎分収集・属性同期・天気）、スマホ Web）、`apps/ml` = FastAPI + LightGBM（`/ml/infer` 5 分毎推論、`/ml/compact` 毎時 Parquet 化、`/ml/evaluate` 日次評価、`/ml/profiles`）。スケジューラは **Vercel Cron**。
  - **Supabase Pro**（東京）：Postgres（配列形式スナップショット・最新状態・予測・ログ）、Storage（生 gzip JSON＝一次ソース、Parquet、モデル、予測ログ）、pg_cron（保守・ウォッチドッグ・SQL 監視→Webhook）、Edge Function は **バックアップ収集器のみ**。
  - **GitHub Actions**：CI、週次再学習、週次 Drive ミラー。**Colab Pro+**：実験。**iOS**：SwiftUI + MapKit、最小 iOS 26。
- **リポジトリ**：pnpm モノレポ。`apps/web`・`apps/ml`・`apps/ios`・`packages/shared`（Zod スキーマ・型・定数）・`packages/gbfs-core`（収集の純粋関数、Node/Deno 共用）・`supabase/`（migrations・functions・seed）・`fixtures/gbfs/`（実データの縮約サンプル）・`docs/`。ルートの `vercel.json` に services / rewrites / crons。
- **ロードマップ**：W1（〜9/11）収集を本番稼働 → W4 ベースライン予測を地図に → W6 LightGBM v1 → W7 TestFlight → W12（11/27）App Store 申請。学習に使えるデータは収集開始日以降しか存在しない。

---

## 2. 最重要原則

1. **データ収集を止めない**。収集器は他の全てから隔離し、生 gzip JSON を Storage に必ず残す。収集コードの変更は最も慎重に行い、デプロイ後は `feed_fetch_log` で取得率と `last_updated` の連続性を確認する。
2. **ジョブは冪等に**。Vercel Cron は欠落・二重起動・同時実行が起こり得る（再試行なし）。`last_updated`（収集）／`base_observed_at`（推論）をキーに二重処理を無害化し、Postgres アドバイザリロックで同時実行を防ぎ、pg_cron のウォッチドッグで欠落を補完する。
3. **サーバー側で先回り推論、アプリは読むだけ**。5 分毎に全ポート × 10 水平 × 2 指標を `station_forecasts` に書く。iOS/Web は `/v1` API のみを叩く（DB 直結・キー埋め込み禁止）。
4. **ML は Python 一本**。特徴量は `apps/ml/bikechance_ml/features/` の単一実装を学習と推論で共用し、モデルの `feature_set` 版と照合する。収集・API・Web は TypeScript、iOS は Swift。
5. **時系列の規律**：ランダム split 禁止。日単位の時系列分割＋パージ（1 日）。特徴量は基準時刻以前の観測のみ、ポート別プロファイルは前日までのデータで計算。天気は「その時点で入手できた予報値」を使う。
6. **確率の品質**：主指標は Brier（＋BSS）、ECE、信頼度図。`is_unbalance` 等で確率を歪めない。ベースライン（持続・条件付き持続・気候値・ブレンド）を 10% 以上上回らない水平は配信しない。
7. **UI は確率が主、台数は従**。実測値には観測時刻、予測値には予測時刻を必ず添え、両者を明確に区別する。断定表現を避ける。
8. **ライセンス遵守**：CC BY 4.0 のクレジット（「改変して利用」の書式）と ODPT の通知文を表示。ttl 超過の値を「現在値」として出さない。ODPT を過負荷にしない（サーバー側 1 分ポーリングのみ、常時二重化しない）。
9. **ベンダーと予算**：Vercel Pro と Supabase Pro 以外のベンダー・有料サービスを追加する場合は必ず確認。Vercel 使用量は月 $20 クレジット内、DB は 8 GB 内（6 GB で警報）を目標に設計する。
10. **決定は記録してから実装**。設計変更は `docs/260904_dev_plan.md` の該当章と §15（決定記録）を先に更新する。

---

## 3. コーディング規約

**哲学：人間が一目で理解できるコードを書く。** コードは物語のように読め、命名はストーリーを語る。

- **小さく純粋に**：1 関数は**20 行未満**・単一責務。副作用（HTTP・DB・Storage）は `io/` に分離し、純粋なデータ変換関数は専用ファイルへ（テスト容易化）。関数間の依存を減らし**疎結合**に。
- **不変・関数型**：`let` より **`const`**（`var` 禁止）、変数は極力上書きしない。`for` より **map/filter/reduce**、`.then()` より **async/await**。文字列組立は `let +=` でなく **array + push + join**。
- **型を厳格に**：明示的型を必ず付ける。**`any` 禁止**。**`as` キャスト禁止 → 型ガード**（`(x: unknown): x is T`）。Zod からは **`z.infer`** で導出（重複型を作らない）。GBFS 入力の Zod スキーマは `passthrough` で未知フィールドを保全し、非標準値（HELLO の文字列 `vehicle_capacity` 等）はスキーマで正規化する。
- **DRY・定数・単位**：マジックナンバー禁止 → 名前付き定数（水平・閾値・保持日数は `packages/shared` の定数に集約）。**変数名に単位**を（`timeout_ms`, `distance_km`, `radius_m`, `reported_age_s`）。
- **堅牢性**：失敗しうる処理に try/catch ＋**文脈付きエラー**（system・phase・HTTP ステータス等。URL は含めない）。fetch/API は **`AbortSignal.timeout` / `AbortController`** 必須。
- **Cron ハンドラの定型**（`/api/jobs/*`、`/ml/*`）：`CRON_SECRET` 検証 → アドバイザリロック → 冪等チェック → 処理 → `job_runs` / `feed_fetch_log` / `inference_log` に記録 → 200 で要約 JSON を返す（3xx を返さない。Cron はリダイレクトを追わない）。`maxDuration` を明示。エラーは 500 を返す（Vercel Observability のエラー率検知を効かせるため）。
- **Vercel Services の中では Middleware（Next.js 16 では `proxy.ts`、旧 `middleware.ts`）を使わない**。ファイルがあるとデプロイが拒否される。認証はルートハンドラの中で行う。
- **リージョン指定は `vercel.json` のトップレベル `regions`**。ルートの `preferredRegion` は使わない（Next.js 16 で非推奨。Vercel 上で許される値は `auto` / `global` / `home` のみで、Node ランタイムではルート単位の指定が無視される）。
- **ODPT のトークンを URL 以外の場所に出さない**：トークン付き URL を組み立てるのは `apps/web/lib/jobs/odpt-fetch.ts` だけにし、`Response` をモジュールの外へ出さない（`res.url` に触れる経路を作らない）。捕捉した例外は再送出せず `{ phase, http_status, error_name }` に詰め替え（undici の `cause` は URL を抱える）、記録する文字列は `redact()` を通す。CI が `res.url` の使用を検出する。
- **Python（`apps/ml`）**：`uv`、`ruff`、`mypy --strict`、`pytest`。型ヒント必須、`polars`/`pyarrow` で列指向に処理、ノートブックの成果は必ずモジュールへ移す。
- **Swift（`apps/ios`）**：Swift 6（strict concurrency）、`@Observable`、`URLSession` + `Codable`（`/v1` スキーマから生成）、Swift Testing、SwiftLint/SwiftFormat。
- **品質ゲート**：**lint / typecheck / unit test を必ず回す**。**境界値・エッジケース**（空フィード、重複 ID、`capacity=0`、文字列容量、BBox 外座標、`last_updated` 後退、欠損区間、Cron 二重起動）のテストを厚く書く。**ゴールデンテスト**（固定フィクスチャからの特徴量・予測の差分検知）と**契約テスト**（`/v1` スキーマ ↔ iOS `Codable`）を維持する。テストデータは `fixtures/gbfs/` の実データ縮約サンプルを使う。
- **import**：npm パッケージはトップレベル import（条件付き依存のみ動的 import）。正当な理由なく再エクスポートしない。ライブラリ既存ユーティリティを自作より優先。バージョン移行時は対象バージョンの API シグネチャを検証。

---

## 4. Git / 変更範囲 / デバッグ（ガードレール）

**Git（ユーザーの明示的許可なしに書き込み系 git を実行しない）**
- 作業前に必ず `git status` / `git branch` で現在地を確認。想定と違えば確認する。
- **`main` に直接 push しない** → フィーチャーブランチを切って **PR**。実装開始前にブランチ作成。
- `git commit` / `push` / `merge` / `rebase` 前に必ず確認を取る。読み取り系（status/diff/log）は自由。
- **`git add .` 禁止**（ファイルは個別に add）。**未追跡ファイルを削除しない**。
- **`push --force` 禁止・`rebase` 禁止**。マージは **merge commit**（`--merge`）で、**squash 禁止**。
- コミットメッセージのプレフィックス：`feat:` / `fix:` / `refactor:` / `docs:` / `chore:`。
- 「PR を作って」＝**PR 作成**（マージではない）。作成前にターゲットブランチを確認。
- `main` へのマージは Vercel の本番デプロイ＝**収集器と Cron 設定の更新**を意味する。収集に触れる PR は影響範囲を PR 本文に明記する。

**変更範囲**
- **明示的に依頼された変更のみ**行う。機能・ツール・パッケージ・ベンダー・コンテンツを**自律的に追加しない**。必要そうならまず確認。ドキュメント・コミットメッセージは簡潔に。

**デバッグ**
- 修正前に**根本原因を診断**。クイックフィックス（値のハードコード・JSON ワークアラウンド）**禁止**。リグレッションは git 履歴 / diff を確認。
- 運用系の調査は `feed_fetch_log` → `job_runs` → `inference_log` → `daily_quality` → Vercel の Cron ログ、の順に見る。

**日付**
- 今日の日付が必要なら**必ず `date` コマンドで取得**する（モデル内部知識に依存しない）。Vercel Cron は **UTC**（JST − 9 時間）で記述する。

---

## 5. セキュリティ / 機密

- **ローカル**は `.env`（gitignore）に `ODPT_ACCESS_TOKEN` 等。**本番**は Vercel 環境変数（`CRON_SECRET`、Supabase サービスロールキー、`ODPT_ACCESS_TOKEN`）と Supabase Vault（ウォッチドッグ用の `CRON_SECRET`・Webhook URL）。GitHub Secrets は CI・再学習用のみ。
- **API キー・トークン付き URL・Webhook URL・サービスロールキーを、出力・ログ・例外・テスト・ドキュメントに出さない**（CI に正規表現チェック）。取得ログには `endpoint: 'token' | 'public'` のみ残す。
- Supabase は**全テーブル RLS 有効化**。匿名ロールには読み取り用ビューのみ。書き込みは Vercel の関数とバックアップ収集器からサービスロールで行う。
- **iOS アプリにトークンやキーを埋め込まない**。アプリは `/v1` のみを叩き、Authorization ヘッダを付けない（CDN キャッシュのため）。
- `/v1` は Vercel WAF のレート制限で保護。プレビュー環境は Deployment Protection で閉じる。
- **位置情報**はタイル境界に量子化した座標だけをサーバーに送り、保持しない（プライバシーラベル「収集しない」を維持）。

---

## 6. 運用ガードレール

- **確認なしに実行しない**：本番 DB の破壊的操作（DROP / TRUNCATE / 保持期間の短縮 / パーティション削除）、pg_cron ジョブの削除・停止、Vercel Cron の無効化・スケジュール変更、Storage バケットやオブジェクトの削除、モデルの `active` 切替・ロールバック。
- スキーマ変更は `supabase/migrations` 経由（ローカル `supabase db reset` で適用テスト後）。本番へ手動 SQL で変更を入れない。
- モデルの昇格は `model_versions.status` の更新のみ（candidate → shadow（3 日）→ active）。モデルカードのないモデルを登録しない。
- 収集の欠損は**補間しない**（学習サンプルから除外する）。生 JSON からの再構築スクリプトを常に動く状態に保つ。
- 費用・容量の変化を伴う変更（推論周期・水平数・木数・保持日数・リージョン）は、プランの見積り（§4.5・§8.2）を更新してから行う。
