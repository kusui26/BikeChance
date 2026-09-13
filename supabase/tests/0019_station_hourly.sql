-- pgTAP: ポート別・1 時間ごとの実績（マイグレーション 0046、W5 プラン §6.6 の PR F）
--
-- ここで固定したい契約は 6 つ。
--   1. **貸出と返却の両方が観測できた点だけを数える**（片方だけの点で 2 つの平均の
--      母数がずれると、どちらかが静かに嘘になる。W5 プラン §12 の 138 と同じ形）
--   2. **進行中の時間は作らない**（途中経過を「その時間の平均」として配らない）
--   3. **観測の無い時間帯は行を作らない**（0 を入れると「0 台だった」に見える）
--   4. **冪等**（同じ回を 2 度走らせても行数も値も変わらない。CLAUDE.md §2 の原則 2）
--   5. **26 時間より古い行は、書く側と同じ回で消える**
--   6. **停止したシステムはビューに出さない**（0018 の 3 と同じ約束）
--
-- 前提条件はこのテストが自分で作る。**行単位の検査は必ず `system_id` で絞る。**

begin;
select plan(16);

-- ────────────────────────────────────────────────────────────────
-- 権限
-- ────────────────────────────────────────────────────────────────
select ok(has_table_privilege('anon', 'public.v1_station_hourly', 'select'),
          'anon は v1_station_hourly を読める');
select ok(not has_table_privilege('anon', 'public.station_hourly', 'select'),
          'anon は基底テーブルを読めない');
select ok(not has_function_privilege('anon', 'public.rollup_station_hourly(integer)', 'execute'),
          'anon は集計の関数を呼べない');

-- ────────────────────────────────────────────────────────────────
-- 前提データ
-- ────────────────────────────────────────────────────────────────
delete from public.station_hourly where system_id like 't-hr-%';

insert into public.systems
  (system_id, display_name, operator_name, gbfs_base_url, expected_cadence_s, poll_interval_s,
   lock_key, is_active, capacity_is_dynamic)
values
  ('t-hr-a', '稼働',   '事業者', 'https://example.test/gbfs', 300, 60, 9401, true,  false),
  ('t-hr-x', '停止中', '事業者', 'https://example.test/gbfs', 300, 60, 9402, false, false);

-- idx は 0 起点で密。配列の `arr[idx + 1]` がそのポートの値
insert into public.stations (system_id, station_id, idx) values
  ('t-hr-a', 'observed',   0),
  ('t-hr-a', 'half',       1),
  ('t-hr-a', 'never',      2),
  ('t-hr-x', 'halted',     0);

-- **前の時間**（完了済み）に 2 回、**いまの時間**（進行中）に 1 回
--   observed … 両方そろう（4 と 6 → 平均 5 / 枠は 10 と 8 → 平均 9）
--   half     … 返却だけ観測できていない（-1）。**数えない**
--   never    … 一度も観測されない（-1）
insert into public.status_snapshots
  (system_id, observed_at, fetched_at, n_stations, bikes, docks, flags, reported_age_s, raw_path)
values
  ('t-hr-a', date_trunc('hour', now()) - interval '50 minutes',
             date_trunc('hour', now()) - interval '50 minutes', 3,
   '{4,7,-1}', '{10,-1,-1}', '{7,7,-1}', '{0,0,0}', 't/1'),
  ('t-hr-a', date_trunc('hour', now()) - interval '20 minutes',
             date_trunc('hour', now()) - interval '20 minutes', 3,
   '{6,9,-1}', '{8,-1,-1}', '{7,7,-1}', '{0,0,0}', 't/2'),
  -- **進行中の時間。** 集計に入ってはいけない
  ('t-hr-a', date_trunc('hour', now()) + interval '5 minutes',
             date_trunc('hour', now()) + interval '5 minutes', 3,
   '{99,99,-1}', '{99,99,-1}', '{7,7,-1}', '{0,0,0}', 't/3');

-- 古い行（掃除されること）
insert into public.station_hourly (system_id, station_id, hour_start, n, bikes_mean, docks_mean)
values ('t-hr-a', 'observed', date_trunc('hour', now()) - interval '40 hours', 3, 1, 1);

-- 停止中システムの行（ビューに出ないこと）
insert into public.station_hourly (system_id, station_id, hour_start, n, bikes_mean, docks_mean)
values ('t-hr-x', 'halted', date_trunc('hour', now()) - interval '1 hour', 3, 1, 1);

-- ────────────────────────────────────────────────────────────────
-- 集計する
-- ────────────────────────────────────────────────────────────────
select lives_ok($$select public.rollup_station_hourly(2)$$, '集計が通る');

select is(
  (select count(*)::int from public.station_hourly
    where system_id = 't-hr-a' and hour_start = date_trunc('hour', now()) - interval '1 hour'),
  1, '前の時間に行が 1 本だけできる（両方そろったポートのみ）');

select is(
  (select station_id from public.station_hourly
    where system_id = 't-hr-a' and hour_start = date_trunc('hour', now()) - interval '1 hour'),
  'observed', '両方そろったポートだけが入る（片方だけ・未観測は入らない）');

select is(
  (select n::int from public.station_hourly
    where system_id = 't-hr-a' and station_id = 'observed'
      and hour_start = date_trunc('hour', now()) - interval '1 hour'),
  2, 'その時間に両方そろった点は 2 つ');

select is(
  (select bikes_mean::numeric(5,2) from public.station_hourly
    where system_id = 't-hr-a' and station_id = 'observed'
      and hour_start = date_trunc('hour', now()) - interval '1 hour'),
  5.00::numeric(5,2), '台数の平均は (4 + 6) / 2');

select is(
  (select docks_mean::numeric(5,2) from public.station_hourly
    where system_id = 't-hr-a' and station_id = 'observed'
      and hour_start = date_trunc('hour', now()) - interval '1 hour'),
  9.00::numeric(5,2), '枠の平均は (10 + 8) / 2');

-- ────────────────────────────────────────────────────────────────
-- 進行中の時間・古い行
-- ────────────────────────────────────────────────────────────────
select is(
  (select count(*)::int from public.station_hourly
    where system_id = 't-hr-a' and hour_start >= date_trunc('hour', now())),
  0, '進行中の時間は作らない');

select is(
  (select count(*)::int from public.station_hourly
    where system_id = 't-hr-a' and hour_start < now() - interval '26 hours'),
  0, '26 時間より古い行は同じ回で消える');

-- ────────────────────────────────────────────────────────────────
-- 冪等
-- ────────────────────────────────────────────────────────────────
select lives_ok($$select public.rollup_station_hourly(2)$$, '2 度目も通る');

select is(
  (select count(*)::int from public.station_hourly where system_id = 't-hr-a'),
  1, '2 度走らせても行は増えない');

select is(
  (select bikes_mean::numeric(5,2) from public.station_hourly
    where system_id = 't-hr-a' and station_id = 'observed'
      and hour_start = date_trunc('hour', now()) - interval '1 hour'),
  5.00::numeric(5,2), '値も変わらない');

-- ────────────────────────────────────────────────────────────────
-- 停止中システム・引数の検証
-- ────────────────────────────────────────────────────────────────
select is(
  (select count(*)::int from public.v1_station_hourly where system_id = 't-hr-x'),
  0, '停止中システムの行はビューに出ない（基底表には在る）');

select throws_ok($$select public.rollup_station_hourly(0)$$, '22023',
                 null, '範囲外の引数は例外にする');

select * from finish();
rollback;
