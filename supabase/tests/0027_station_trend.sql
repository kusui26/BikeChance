-- ────────────────────────────────────────────────────────────────
-- pgTAP: ポートの「ふだんの傾向」（マイグレーション 0059、W9 プランの PR B）
--
-- ここで固定したい契約は 6 つ。
--   1. **72 個の 1 次元の配列しか入らない**（長さが違うと、読む側の添字が別の時間を指す）
--   2. **確率は 0〜1000、出せない時間は NULL の要素で入る**
--   3. **古い版で新しい行を上書きしない**（欠けた日を手で埋め直す回が、傾向を巻き戻さない）。
--      同じ版なら置き直す（冪等）
--   4. **台帳に無いポートは書けない**（外部キー）
--   5. **停止したシステムはビューに出さない**（0046 と同じ約束）
--   6. **匿名は公開ビューだけを読める**（基底表と書く関数には手が届かない）
--
-- 前提条件はこのテストが自分で作る。**行単位の検査は必ず `system_id` で絞る。**

begin;
select plan(28);

-- ────────────────────────────────────────────────────────────────
-- 権限
-- ────────────────────────────────────────────────────────────────
select ok(has_table_privilege('anon', 'public.v1_station_trend', 'select'),
          'anon は v1_station_trend を読める');
select ok(has_table_privilege('anon', 'public.v1_holidays', 'select'),
          'anon は v1_holidays を読める');
select ok(not has_table_privilege('anon', 'public.station_trend', 'select'),
          'anon は基底表を読めない');
select ok(not has_function_privilege('anon', 'public.upsert_station_trend(jsonb)', 'execute'),
          'anon は書く関数を呼べない');
select ok(has_function_privilege('service_role', 'public.upsert_station_trend(jsonb)', 'execute'),
          'service_role は書く関数を呼べる（本番は自動公開が OFF なので明示的に付けてある）');
select is(
  (select relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relname = 'station_trend'),
  true, 'station_trend は RLS 有効');

-- ────────────────────────────────────────────────────────────────
-- 前提データ
-- ────────────────────────────────────────────────────────────────
insert into public.systems
  (system_id, display_name, operator_name, gbfs_base_url, expected_cadence_s, poll_interval_s,
   lock_key, is_active, capacity_is_dynamic)
values
  ('t-tr-a', '稼働',   '事業者', 'https://example.test/gbfs', 300, 60, 9701, true,  false),
  ('t-tr-x', '停止中', '事業者', 'https://example.test/gbfs', 300, 60, 9702, false, false);

insert into public.stations (system_id, station_id, idx) values
  ('t-tr-a', 'p1', 0),
  ('t-tr-a', 'p2', 1),
  ('t-tr-x', 'halted', 0);

-- 1 行ぶん。`p_bike` だけ差し替えられるようにする（`p_dock` は 500 が 72 個、`n_days` は 20 が 72 個）
create function pg_temp.trend(
  p_system text, p_station text, p_date date, p_bike int[]
) returns jsonb language sql as $$
  select jsonb_build_array(jsonb_build_object(
    'system_id', p_system, 'station_id', p_station, 'profile_date', p_date,
    'p_bike_x1000', p_bike,
    'p_dock_x1000', array_fill(500, array[72]),
    'n_days', array_fill(20, array[72])
  ));
$$;

-- ────────────────────────────────────────────────────────────────
-- 書く
-- ────────────────────────────────────────────────────────────────
select is(
  public.upsert_station_trend(
    pg_temp.trend('t-tr-a', 'p1', date '2026-10-31', array_fill(800, array[72]))),
  1, '1 行入る');
select is(
  (select array_length(p_bike_x1000, 1) from public.station_trend
    where system_id = 't-tr-a' and station_id = 'p1'),
  72, '72 個で入る');
select is(
  public.upsert_station_trend(
    pg_temp.trend('t-tr-a', 'p2', date '2026-10-31', array_fill(null::int, array[71]) || 900)),
  1, '**出せない時間（NULL の要素）があっても入る**');
select is(
  (select p_bike_x1000[1] from public.station_trend
    where system_id = 't-tr-a' and station_id = 'p2'),
  null::smallint, 'NULL の要素は NULL のまま');
select is(
  (select p_bike_x1000[72] from public.station_trend
    where system_id = 't-tr-a' and station_id = 'p2'),
  900::smallint, '値の要素は値のまま（並びが崩れない）');
select is(public.upsert_station_trend('[]'::jsonb), 0, '空でも落ちない');

-- ────────────────────────────────────────────────────────────────
-- 版の順序（冪等・巻き戻さない）
-- ────────────────────────────────────────────────────────────────
select is(
  public.upsert_station_trend(
    pg_temp.trend('t-tr-a', 'p1', date '2026-10-31', array_fill(810, array[72]))),
  1, '同じ版は置き直す（冪等）');
select is(
  (select p_bike_x1000[1] from public.station_trend
    where system_id = 't-tr-a' and station_id = 'p1'),
  810::smallint, '置き直した値になる');
select is(
  public.upsert_station_trend(
    pg_temp.trend('t-tr-a', 'p1', date '2026-10-25', array_fill(100, array[72]))),
  0, '**古い版は書かない**（数にも入らない）');
select is(
  (select (profile_date, p_bike_x1000[1])::text from public.station_trend
    where system_id = 't-tr-a' and station_id = 'p1'),
  '(2026-10-31,810)', '新しい行が残る');
select is(
  public.upsert_station_trend(
    pg_temp.trend('t-tr-a', 'p1', date '2026-11-01', array_fill(820, array[72]))),
  1, '新しい版は書く');
select is(
  (select count(*)::int from public.station_trend where system_id = 't-tr-a'),
  2, '何度書いても 1 ポート 1 行');

-- ────────────────────────────────────────────────────────────────
-- 値の妥当性
-- ────────────────────────────────────────────────────────────────
select throws_ok(
  $$select public.upsert_station_trend(
      pg_temp.trend('t-tr-a', 'p1', date '2026-11-02', array_fill(800, array[71])))$$,
  '23514', null, '**72 個でなければ入らない**（添字が別の時間を指す）');
select throws_ok(
  $$select public.upsert_station_trend(
      pg_temp.trend('t-tr-a', 'p1', date '2026-11-02', array_fill(800, array[2, 36])))$$,
  '23514', null, '**2 次元の配列は入らない**（要素の数が 72 でも）');
select throws_ok(
  $$select public.upsert_station_trend(
      pg_temp.trend('t-tr-a', 'p1', date '2026-11-02', array_fill(800, array[71]) || 1001))$$,
  '23514', null, '確率は 0〜1000（×1000 の整数）');
select throws_ok(
  $$select public.upsert_station_trend(
      pg_temp.trend('t-tr-a', 'unknown', date '2026-11-02', array_fill(800, array[72])))$$,
  '23503', null, '**台帳に無いポートは書けない**（外部キー）');

-- ────────────────────────────────────────────────────────────────
-- 公開ビュー
-- ────────────────────────────────────────────────────────────────
select lives_ok(
  $$select public.upsert_station_trend(
      pg_temp.trend('t-tr-x', 'halted', date '2026-11-01', array_fill(800, array[72])))$$,
  '停止中システムの行も基底表には書ける');
select is(
  (select count(*)::int from public.v1_station_trend where system_id = 't-tr-x'),
  0, '**停止中システムの行はビューに出ない**');
select is(
  (select p_bike_x1000[1]::int from public.v1_station_trend
    where system_id = 't-tr-a' and station_id = 'p1'),
  820, '稼働中のシステムの行はビューに出る');

-- 祝日のビューは表の中身をそのまま出す（**暦の規則は持たない**。年末年始とお盆は読む側の規則）
insert into public.jp_holidays (holiday_date, name) values (date '2099-11-03', '検査の祝日')
    on conflict (holiday_date) do nothing;
select is(
  (select count(*)::int from public.v1_holidays where holiday_date = date '2099-11-03'),
  1, '祝日はビューに出る');

-- ────────────────────────────────────────────────────────────────
-- 見張り
-- ────────────────────────────────────────────────────────────────
select ok(
  (select is_active and cron_job_name is null
      and expected_every = interval '1 day' and missing_after = interval '30 hours'
     from public.monitored_jobs where job_name = 'build_trend'),
  'build_trend は日次で見張る（GitHub Actions なので cron_job_name は NULL）');
select is(
  (select count(distinct (expected_every, missing_after))::int from public.monitored_jobs
    where job_name in ('build_profiles', 'build_trend')),
  1, 'プロファイルと同じ閾値（同じワークフローで続けて走る）');

select * from finish();
rollback;
