-- pgTAP: `/v1/trip-check` が読む近傍のビュー（W4 プラン §6.7、マイグレーション 0039）
--
-- ここで固定したい契約は 4 つ。
--   1. **匿名はビューを読めて、基底テーブルは読めない**（CLAUDE.md §5）
--   2. **列を固定する。** 増減に気づけるようにする（0010 と同じ方針）
--   3. **停止したシステムのポートは出さない。** `v1_stations_current` と同じ約束にしないと、
--      代替候補として出したポートを `/v1/stations` が知らない、という食い違いが起きる
--   4. **絞り込みを焼き付けていない。** `distance_m` も `same_system` もそのまま出す
--      （絞るのは問い合わせ側の仕事。W4-26）
--
-- 前提条件はこのテストが自分で作る（0010 と同じ方針）。ローカルの状態に依存させない。
-- **行単位の検査は必ず `system_id` で絞る。**

begin;
select plan(16);

-- ────────────────────────────────────────────────────────────────
-- 権限
-- ────────────────────────────────────────────────────────────────
select ok(has_table_privilege('anon', 'public.v1_station_neighbors', 'select'),
          'anon は v1_station_neighbors を読める');
select ok(has_table_privilege('service_role', 'public.v1_station_neighbors', 'select'),
          'service_role も読める（/v1 はサービスロールで問い合わせる）');
select ok(not has_table_privilege('anon', 'public.station_neighbors', 'select'),
          'anon は基底テーブルを読めない');
select ok(not has_table_privilege('authenticated', 'public.v1_station_neighbors', 'select'),
          'authenticated には与えていない（最小権限）');

-- ────────────────────────────────────────────────────────────────
-- 列
-- ────────────────────────────────────────────────────────────────
-- **`select *` をしないので、列が増えても実行時まで気づけない。** ここで固定する。
select columns_are('public', 'v1_station_neighbors',
  array['system_id', 'station_id', 'nb_system_id', 'nb_station_id', 'distance_m', 'same_system'],
  '列は 6 つ');

-- ────────────────────────────────────────────────────────────────
-- 前提データ
-- ────────────────────────────────────────────────────────────────
delete from public.station_neighbors where true;

insert into public.systems
  (system_id, display_name, operator_name, gbfs_base_url, expected_cadence_s, poll_interval_s,
   lock_key, is_active, capacity_is_dynamic)
values
  ('t-nb-a', '稼働 A', '事業者', 'https://example.test/gbfs', 300, 60, 9301, true,  false),
  ('t-nb-b', '稼働 B', '事業者', 'https://example.test/gbfs', 300, 60, 9302, true,  false),
  ('t-nb-x', '停止中', '事業者', 'https://example.test/gbfs', 300, 60, 9303, false, false);

insert into public.station_neighbors
  (system_id, station_id, nb_system_id, nb_station_id, distance_m, same_system)
values
  -- 同一システム。近い順に 3 つ
  ('t-nb-a', 's1', 't-nb-a', 's2',  90, true),
  ('t-nb-a', 's1', 't-nb-a', 's3', 380, true),
  ('t-nb-a', 's1', 't-nb-a', 's4', 460, true),
  -- 別システム（稼働中）。**ビューは落とさない**——落とすのは問い合わせ側
  ('t-nb-a', 's1', 't-nb-b', 'b1', 120, false),
  -- 停止中のシステムが絡む組。**どちら側でも落とす**
  ('t-nb-a', 's1', 't-nb-x', 'x1', 100, false),
  ('t-nb-x', 'x1', 't-nb-a', 's1', 100, false);

-- ────────────────────────────────────────────────────────────────
-- 停止中のシステムを出さない
-- ────────────────────────────────────────────────────────────────
select is(
  (select count(*)::int from public.v1_station_neighbors where system_id = 't-nb-a'),
  4, '稼働中どうしの 4 行だけが出る（停止中が絡む 1 行は落ちる）');

select is(
  (select count(*)::int from public.v1_station_neighbors where nb_system_id = 't-nb-x'),
  0, '相手が停止中の組は出さない');

select is(
  (select count(*)::int from public.v1_station_neighbors where system_id = 't-nb-x'),
  0, '自分が停止中の組も出さない');

-- ────────────────────────────────────────────────────────────────
-- 値をそのまま写す（絞り込みを焼き付けていない）
-- ────────────────────────────────────────────────────────────────
select is(
  (select distance_m from public.v1_station_neighbors
    where system_id = 't-nb-a' and station_id = 's1' and nb_station_id = 's2'),
  90::smallint, 'distance_m をそのまま返す');

select is(
  (select count(*)::int from public.v1_station_neighbors
    where system_id = 't-nb-a' and station_id = 's1' and distance_m > 400),
  1, '**400 m を超える組も落とさない**（絞るのは問い合わせ側。W4-26）');

select is(
  (select count(*)::int from public.v1_station_neighbors
    where system_id = 't-nb-a' and station_id = 's1' and not same_system),
  1, '**別システムの組も落とさない**（同じく問い合わせ側で絞る）');

select is(
  (select same_system from public.v1_station_neighbors
    where system_id = 't-nb-a' and station_id = 's1' and nb_station_id = 'b1'),
  false, 'same_system をそのまま返す');

-- ────────────────────────────────────────────────────────────────
-- 問い合わせ側が実際に使う絞り込み（同一システム・400 m 以内）
-- ────────────────────────────────────────────────────────────────
-- **`view-query.ts` の `neighborFilters` と同じ条件**を SQL 側でも 1 度通しておく。
select is(
  (select string_agg(nb_station_id, ',' order by distance_m)
     from public.v1_station_neighbors
    where system_id = 't-nb-a' and station_id = 's1' and same_system and distance_m <= 400),
  's2,s3', '同一システム・400 m 以内は 2 件（460 m と別システムは外れる）');

-- ────────────────────────────────────────────────────────────────
-- 両向きに行がある（片方向の表を両向きぶん持つ設計）
-- ────────────────────────────────────────────────────────────────
insert into public.station_neighbors
  (system_id, station_id, nb_system_id, nb_station_id, distance_m, same_system)
values ('t-nb-a', 's2', 't-nb-a', 's1', 90, true);

select is(
  (select count(*)::int from public.v1_station_neighbors
    where system_id = 't-nb-a' and station_id = 's2'),
  1, '逆向きの行も引ける（station_id で絞れば「そのポートの近傍」が出る）');

-- ────────────────────────────────────────────────────────────────
-- ビューであること（基底テーブルを直接公開していない）
-- ────────────────────────────────────────────────────────────────
select is(
  (select c.relkind from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relname = 'v1_station_neighbors'),
  'v'::"char", 'ビューである');

select ok(
  (select not relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relname = 'station_neighbors') is not true,
  '基底テーブルの RLS は有効なまま（ビューは所有者権限で迂回する。0018 と同じ形）');

select * from finish();
rollback;
