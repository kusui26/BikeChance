-- 0002 拡張の有効化（W1 プラン §6.3）
--
-- W1 で使うのは pg_cron（ウォッチドッグ・保守・監視）と pg_net（Webhook と Vercel の起動）。
-- どちらも PR E1 で初めて使うが、拡張の有効化は環境差が出やすいのでスキーマと同じ
-- タイミングで済ませ、ローカルと本番で同じ状態にしておく。
--
-- pgcrypto と supabase_vault は Supabase が既に導入済み。pgtap は本番に入れず、
-- ローカルの seed.sql でだけ有効化する（テスト専用のため）。
--
-- 配置スキーマ：Supabase の慣例に合わせ、pg_net は extensions スキーマに置く。
-- pg_cron は cron スキーマを自前で作るため schema 指定をしない。

create extension if not exists pg_cron;

create extension if not exists pg_net with schema extensions;
