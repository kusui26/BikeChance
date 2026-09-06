-- 0004 参照データ（W1 プラン §6.3、§5 の 23）
--
-- **なぜ seed.sql ではなくマイグレーションなのか**：`supabase/seed.sql` はローカルの
-- `supabase db reset` でしか実行されず、`supabase db push` は流さない。本番の
-- `feed_state` が空のままだと `begin_fetch` の claim（1 文 UPDATE）が空振りし、
-- 収集が始まらない。参照データは冪等な INSERT としてマイグレーションに入れる。
--
-- 冪等性：`on conflict do nothing`。既存の行は上書きしない。値を変えたいときは
-- 新しいマイグレーションで明示的に UPDATE する（本番へ手で SQL を入れない）。
--
-- 値は packages/shared/src/constants.ts の SYSTEMS と一致させる。

insert into public.systems
  (system_id, display_name, operator_name, gbfs_base_url, expected_cadence_s, poll_interval_s, lock_key)
values
  (
    'hellocycling',
    'HELLO CYCLING',
    'OpenStreet株式会社 / 公共交通オープンデータ協議会',
    'https://api.odpt.org/api/v4/gbfs/hellocycling',
    300,   -- 実測 299〜301 秒
    60,
    1
  ),
  (
    'docomo-cycle',
    'ドコモ・バイクシェア',
    '株式会社ドコモ・バイクシェア / 公共交通オープンデータ協議会',
    'https://api.odpt.org/api/v4/gbfs/docomo-cycle',
    80,    -- 実測 76〜85 秒
    60,
    2
  )
on conflict (system_id) do nothing;

-- begin_fetch は UPDATE で claim するため、行が無いと永久に claim できない（§5 の 19）
insert into public.feed_state (system_id)
select system_id from public.systems
on conflict (system_id) do nothing;

-- ウォッチドッグが叩く先。秘密ではないので Vault ではなくここに置く（§5 の 21）
insert into public.app_config (key, value)
values ('project_base_url', 'https://bike-chance.vercel.app')
on conflict (key) do nothing;
