-- pgTAP: RLS と権限（W1 プラン §6.3、§4.3 の 1）
--
-- 本番は「新規テーブルの自動公開」が OFF で、その設定は `service_role` の既定権限まで
-- 剥奪する。RLS を有効にしただけでは足りず、明示的な grant が無いと REST が 42501 になる。
-- ここでは「anon には何も無い」「service_role には全部ある」を機械で固定する。

begin;
select plan(17);

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
-- PR B 以降で RPC を足したとき、grant を書き忘れればここが落ちる
select is(
  (select count(*)::int
     from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public'
      and has_function_privilege('anon', p.oid, 'execute')),
  0, 'public に anon が実行できる関数は 1 つも無い'
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

select * from finish();
rollback;
