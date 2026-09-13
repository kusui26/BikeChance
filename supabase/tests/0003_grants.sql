-- pgTAP: RLS と権限（W1 プラン §6.3、§4.3 の 1）
--
-- 本番は「新規テーブルの自動公開」が OFF で、その設定は `service_role` の既定権限まで
-- 剥奪する。RLS を有効にしただけでは足りず、明示的な grant が無いと REST が 42501 になる。
-- ここでは「anon には何も無い」「service_role には全部ある」を機械で固定する。

begin;
select plan(19);

-- ────────────────────────────────────────────────────────────────
-- RLS：例外を作らない
-- ────────────────────────────────────────────────────────────────
select is(
  (select count(*)::int
     from pg_class c
     join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public'
      and c.relkind in ('r', 'p')
      and not c.relrowsecurity),
  0, 'public の全テーブル（パーティションを含む）で RLS が有効'
);
select is(
  (select count(*)::int from pg_policies where schemaname = 'public'),
  0, 'ポリシーは 1 つも無い（= BYPASSRLS の service_role だけが通る）'
);

-- ────────────────────────────────────────────────────────────────
-- anon / authenticated には何も与えない
-- ────────────────────────────────────────────────────────────────
-- `public` スキーマの USAGE は PUBLIC ロール経由で誰でも持つ（Postgres の既定）。
-- したがって「スキーマを使えないこと」ではなく「テーブルに手が届かないこと」を固定する。
select ok(
  not has_table_privilege('anon', 'public.systems', 'select'),
  'anon は systems を読めない'
);
select ok(
  not has_table_privilege('authenticated', 'public.status_snapshots', 'select'),
  'authenticated はスナップショットを読めない'
);
select is(
  (select count(*)::int
     from pg_class c
     join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public'
      and c.relkind in ('r', 'p')
      and has_table_privilege('anon', c.oid, 'select')),
  0, 'anon が select できるテーブルは 1 つも無い'
);
select is(
  (select count(*)::int
     from pg_class c
     join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public'
      and c.relkind in ('r', 'p')
      and has_table_privilege('anon', c.oid, 'insert')),
  0, 'anon が insert できるテーブルは 1 つも無い'
);
select ok(
  not has_function_privilege('anon', 'public.ensure_snapshot_partitions(integer)', 'execute'),
  'anon は保守関数を実行できない'
);
select ok(
  not has_function_privilege('anon', 'public.drop_expired_snapshot_partitions(integer)', 'execute'),
  'anon はパーティション削除関数を実行できない'
);
-- 個別の関数名を並べるのではなく、**例外が 1 つも無いこと**を固定する。
-- PR B 以降で RPC を足したとき、grant を書き忘れればここが落ちる。
--
-- イベントトリガ関数は除く。戻り値が event_trigger の関数は直接呼び出せず
-- （'can only be called in an event trigger' になる）、PostgREST も公開しない。
-- 本番には Supabase の自動 RLS が作る rls_auto_enable がこの形で存在し、
-- anon に EXECUTE が付いているが、呼べないので実害がない（ローカルには無い）。
select is(
  (select count(*)::int
     from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public'
      and p.prorettype <> 'pg_catalog.event_trigger'::regtype
      and has_function_privilege('anon', p.oid, 'execute')),
  0, 'public に anon が呼び出せる関数は 1 つも無い（イベントトリガ関数を除く）'
);

-- ────────────────────────────────────────────────────────────────
-- service_role には明示的に与える（自動公開 OFF では自動では付かない）
-- ────────────────────────────────────────────────────────────────
select ok(
  has_schema_privilege('service_role', 'public', 'usage'),
  'service_role は public スキーマを使える'
);
-- 実効権限は「最低限あること」だけを見る。ローカルは既定で service_role に ALL を
-- 与えるため（TRUNCATE / REFERENCES / TRIGGER が余分に付く）、集合の完全一致で
-- 断言するとローカルと本番で結果が変わってしまう。
select is(
  (select count(*)::int
     from unnest(array['SELECT', 'INSERT', 'UPDATE', 'DELETE']) p
    where not has_table_privilege('service_role', 'public.feed_state', p)),
  0, 'service_role は feed_state を読み書きできる（begin_fetch の claim に必要）'
);
select is(
  (select count(*)::int
     from unnest(array['SELECT', 'INSERT', 'UPDATE', 'DELETE']) p
    where not has_table_privilege('service_role', 'public.status_snapshots', p)),
  0, 'service_role は status_snapshots を読み書きできる'
);
select is(
  (select count(*)::int
     from pg_class c
     join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public'
      and c.relkind in ('r', 'p')
      and not has_table_privilege('service_role', c.oid, 'select')),
  0, 'service_role が読めないテーブルは 1 つも無い'
);
select ok(
  has_function_privilege('service_role', 'public.ensure_snapshot_partitions(integer)', 'execute'),
  'service_role は保守関数を実行できる'
);

-- ────────────────────────────────────────────────────────────────
-- 将来作るテーブルにも既定権限が効く
-- ────────────────────────────────────────────────────────────────
create table public.privilege_probe (id integer primary key);
select ok(
  has_table_privilege('service_role', 'public.privilege_probe', 'select')
    and not has_table_privilege('anon', 'public.privilege_probe', 'select'),
  '以後に作るテーブルも service_role だけが読める（alter default privileges が効いている）'
);

-- PR B 以降で追加する RPC が、明示的に grant するまで匿名から呼べないこと。
-- 関数は既定で PUBLIC に EXECUTE が付くため、ここを外していないと事故になる
create function public.privilege_probe_fn() returns integer language sql as 'select 1';
select ok(
  not has_function_privilege('anon', 'public.privilege_probe_fn()', 'execute'),
  '以後に作る関数は明示的に grant するまで anon から呼べない'
);

-- マイグレーションが設定した既定権限そのものを確かめる。
-- 実効権限はローカルの初期設定に紛れるが、この設定は環境に依らず自分で書いたものだけが出る
select ok(
  exists (
    select 1
      from pg_default_acl d
      join pg_namespace n on n.oid = d.defaclnamespace
      join pg_roles r on r.oid = d.defaclrole
     where n.nspname = 'public' and d.defaclobjtype = 'r' and r.rolname = 'postgres'
       and array_to_string(d.defaclacl, ' ') like '%service_role=%'
  ),
  'postgres が作るテーブルの既定権限に service_role が入っている'
);

-- ────────────────────────────────────────────────────────────────
-- PostgREST から呼ぶ関数には明示的な grant が要る（W3 プラン §12 の 90）
-- ────────────────────────────────────────────────────────────────
-- **本番は「新規オブジェクトの自動公開」が OFF** で、`postgres` が作った関数の
-- EXECUTE が `service_role` に既定で付かない（表とシーケンスには 0006 が既定権限を
-- 入れてあるので付く）。ローカルは既定が違うため、`has_function_privilege` で見ると
-- **ローカルだけ通って本番で 42501 になる**。そこで **明示的な grant があること** を見る。
--
-- 関数を 2 つに分類し、どちらにも入っていないものが無いことも見張る。新しい関数を
-- 足したら、ここでどちらかに入れることになる。
create function pg_temp.rest_called() returns text[] language sql immutable as $$
  select array[
    -- 収集（apps/web/lib/jobs）
    'begin_fetch', 'finish_fetch', 'ingest_snapshot', 'upsert_station_attributes',
    'weather_grid_cells',
    -- ジョブの記録（apps/ml と Edge Function）
    'job_started', 'job_finished',
    -- 再構築スクリプト
    'ensure_snapshot_partitions', 'drop_expired_snapshot_partitions', 'snapshot_partition_exists',
    -- 祝日の取り込みスクリプト
    'replace_jp_holidays',
    -- 推論（apps/ml の /ml/infer）
    'begin_inference', 'finish_inference', 'upsert_forecasts',
    -- 天気の取り込み（apps/ml の /ml/weather）
    'upsert_weather_hourly',
    -- モデルの登録（apps/ml の学習ジョブ）
    'register_model_version',
    -- ポートの大きさ（apps/ml の build_reference。0045）
    'upsert_capacity_est'
  ];
$$;

create function pg_temp.cron_only() returns text[] language sql immutable as $$
  select array[
    -- pg_cron から呼ぶ（PostgREST から呼ばないので grant は要らない）
    'watchdog_collect', 'monitor_feeds', 'monitor_jobs', 'run_maintenance',
    'refresh_station_activity', 'compute_daily_quality', 'trigger_backup_collect',
    'trigger_infer',
    'check_jobs_missing', 'check_jobs_failed', 'check_cron_jobs', 'check_parquet_gap',
    'check_reference_data', 'check_inference',
    'rebuild_geo', 'rebuild_station_geo', 'rebuild_station_neighbors',
    'rollup_station_hourly',
    -- 他の関数の中からだけ呼ぶ補助
    'send_alert', 'config_int', 'jsonb_boolean', 'jsonb_number', 'failure_reason',
    -- Supabase が作る（こちらの管理外）
    'rls_auto_enable',
    -- このテスト自身が既定権限を測るために作る
    'privilege_probe_fn',
    -- **誰からも呼べない**（所有者が psql から呼ぶ。CLAUDE.md §6）
    'promote_model_version'
  ];
$$;

select is(
  (select array_agg(p.proname order by p.proname)
     from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.prokind = 'f'
      and p.proname = any (pg_temp.rest_called())
      and p.proacl::text not like '%service_role=X%'),
  null,
  'PostgREST から呼ぶ関数はすべて service_role に明示的な grant を持つ（本番で 42501 にならない）'
);

select is(
  (select array_agg(p.proname order by p.proname)
     from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.prokind = 'f'
      and not (p.proname = any (pg_temp.rest_called()))
      and not (p.proname = any (pg_temp.cron_only()))),
  null,
  '**どちらにも分類されていない関数が無い**（新しい関数を足したらここで分類する）'
);

select * from finish();
rollback;
