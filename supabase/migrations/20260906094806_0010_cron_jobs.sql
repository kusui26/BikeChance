-- 0010 pg_cron のジョブ登録（W1 プラン §6.7）
--
-- **すべて UTC で書く。** pg_cron の `cron.timezone` は変更できない（§4.3 の 5）。
-- コメントに JST を併記する。
--
-- `cron.schedule('名前', ...)` は**同名ジョブを上書き**する（§4.3 の 6）。名前を固定して
-- おけばマイグレーションを何度流しても増えない。名前は大文字小文字を区別し、変更できない。
--
-- 有効・無効の切替は `cron.alter_job()`、削除は `cron.unschedule()` を使う。
-- Supabase では `postgres` ロールが `cron.job` に SELECT しかできないため、
-- `update cron.job set active = false` は失敗する（§4.3 の 4）。

-- 毎分：Vercel Cron の配信漏れを補う。閾値は collect_interval_s から導く（W1-29）
select cron.schedule('watchdog_collect', '* * * * *', $$select public.watchdog_collect()$$);

-- 5 分毎：停滞・連続失敗・異常などの検知と通知
select cron.schedule('monitor_feeds', '*/5 * * * *', $$select public.monitor_feeds()$$);

-- 03:00 JST：パーティションの作成・削除、ログの掃除
select cron.schedule('maintain_partitions', '0 18 * * *', $$select public.run_maintenance(60)$$);

-- 03:30 JST：台帳の最終観測と活性の更新
select cron.schedule('refresh_station_activity', '30 18 * * *', $$select public.refresh_station_activity()$$);

-- 07:00 JST：前日（JST）の収集品質を集計して通知
select cron.schedule('daily_quality', '0 22 * * *', $$select public.compute_daily_quality()$$);
