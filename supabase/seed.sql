-- ローカル専用のシード（W1 プラン §6.3、§5 の 23）
--
-- **このファイルは `supabase db reset`（ローカル）でしか実行されない。**
-- `supabase db push` は既定で seed を流さないため、本番にも必要な参照データ
-- （systems / feed_state / app_config）はマイグレーション 0004 に置いてある。
--
-- ここにはテスト専用の仕込みだけを書く。本番に入れたくないものの置き場所。

-- pgTAP。`supabase test db` が使う。本番には入れない
create extension if not exists pgtap with schema extensions;
