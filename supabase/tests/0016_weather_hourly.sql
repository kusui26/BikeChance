-- pgTAP: 天気の予報テーブル（W4 プラン §6.4、マイグレーション 0037）
--
-- ここで見るのは **DB 側の契約**——表の形・権限・冪等・「未処理」の判定・保守。
-- 値の意味（どの時刻の予報をどう引くか）は `apps/ml` の側で、
-- `tests/test_features_weather.py` が持つ。

begin;
select plan(37);

delete from public.weather_hourly;
delete from public.job_runs;

-- 予報ファイルを 1 つ「入手した」ことにする（`v_weather_files` はここから作られる）
create function pg_temp.archived(
  p_hour timestamptz, p_cells integer, p_status text default 'ok'
) returns void language sql as $$
  insert into public.job_runs (job_name, started_at, finished_at, status, detail)
  values ('archive_weather', p_hour, p_hour + interval '18 minutes', p_status,
          jsonb_build_object('hour_epoch_s', extract(epoch from p_hour)::bigint,
                             'n_cells', p_cells, 'n_saved', 6, 'n_failed', 0));
$$;

create function pg_temp.forecast_rows(p_hour timestamptz, p_cells integer) returns jsonb
language sql as $$
  select jsonb_agg(jsonb_build_object(
           'cell_lat_idx', 713, 'cell_lon_idx', 2200 + n,
           'issued_hour', p_hour, 'available_at', p_hour + interval '18 minutes',
           'precip_mm', array[0.0, 1.5], 'temp_c', array[24.0, 23.5],
           'wind_kmh', array[7.3, 8.1], 'weather_code', array[1, 61]))
    from generate_series(0, p_cells - 1) n;
$$;

-- ────────────────────────────────────────────────────────────────
-- 表の形と権限
-- ────────────────────────────────────────────────────────────────
select has_table('public', 'weather_hourly', 'weather_hourly がある');
select columns_are(
  'public', 'weather_hourly',
  array['cell_lat_idx', 'cell_lon_idx', 'issued_hour', 'available_at',
        'precip_mm', 'temp_c', 'wind_kmh', 'weather_code'],
  '列はこの 8 つだけ（増えたら特徴量の版も上げる）'
);
-- **常に NULL の列を作らない。** Open-Meteo の JMA モデルは降水確率を返さない（W4-05）
select hasnt_column('public', 'weather_hourly', 'precip_prob',
  '降水確率の列は持たない（jma_msm は返さない）');
select hasnt_column('public', 'weather_hourly', 'source',
  'source の列は持たない（入るのは jma_msm だけ）');
select col_is_pk('public', 'weather_hourly',
  array['cell_lat_idx', 'cell_lon_idx', 'issued_hour'],
  '主キーは「格子 × 発行」');
select is(
  (select relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relname = 'weather_hourly'),
  true, 'RLS 有効（CLAUDE.md §5）'
);
select is(has_table_privilege('anon', 'public.weather_hourly', 'select'), false, '匿名は読めない');
select is(has_table_privilege('service_role', 'public.weather_hourly', 'insert'), true,
  'サービスロールは書ける');
select has_index('public', 'weather_hourly', 'weather_hourly_issued_hour_idx',
  '発行時刻の索引がある（一覧・読み出し・削除がこれで切る）');

-- ────────────────────────────────────────────────────────────────
-- 制約
-- ────────────────────────────────────────────────────────────────
select throws_ok(
  $$insert into public.weather_hourly values
      (713, 2200, '2026-09-10T00:00Z', '2026-09-10T00:18Z',
       array[0.0, 1.0]::real[], array[24.0]::real[], array[7.3, 8.1]::real[],
       array[1, 61]::smallint[])$$,
  '23514', null, '配列の長さがそろわない行は入らない'
);
select throws_ok(
  $$insert into public.weather_hourly values
      (713, 2200, '2026-09-10T00:00Z', '2026-09-09T23:00Z',
       array[0.0]::real[], array[24.0]::real[], array[7.3]::real[], array[1]::smallint[])$$,
  '23514', null, '入手が発行より前の行は入らない'
);

-- ────────────────────────────────────────────────────────────────
-- upsert_weather_hourly
-- ────────────────────────────────────────────────────────────────
select has_function('public', 'upsert_weather_hourly', 'upsert_weather_hourly がある');
select is(
  (select p.prosecdef from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'upsert_weather_hourly'),
  true, 'security definer'
);
select is(
  (select p.proconfig @> array['search_path=""'] from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'upsert_weather_hourly'),
  true, 'search_path = '''''
);
select is(
  has_function_privilege('anon', 'public.upsert_weather_hourly(jsonb)', 'execute'),
  false, '匿名は呼べない'
);
select is(
  has_function_privilege('service_role', 'public.upsert_weather_hourly(jsonb)', 'execute'),
  true, 'サービスロールは呼べる'
);

select is(
  public.upsert_weather_hourly(pg_temp.forecast_rows('2026-09-10T00:00Z', 3)),
  3, '3 格子ぶん入る'
);
select is((select count(*)::integer from public.weather_hourly), 3, '3 行になった');
select is(
  public.upsert_weather_hourly(pg_temp.forecast_rows('2026-09-10T00:00Z', 3)),
  3, '同じ発行を入れ直しても 3 行のまま（冪等）'
);
select is((select count(*)::integer from public.weather_hourly), 3, '増えていない');
select is(
  (select precip_mm from public.weather_hourly
    where cell_lat_idx = 713 and cell_lon_idx = 2200),
  array[0.0, 1.5]::real[], '配列がそのまま入る'
);
select is(
  (select array_length(weather_code, 1) from public.weather_hourly
    where cell_lat_idx = 713 and cell_lon_idx = 2200),
  2, '天気コードも配列で持つ'
);

-- ────────────────────────────────────────────────────────────────
-- v_weather_pending — 何が未処理か
-- ────────────────────────────────────────────────────────────────
select is(has_table_privilege('anon', 'public.v_weather_pending', 'select'), false,
  '匿名は未処理の一覧も読めない');

select pg_temp.archived('2026-09-10T00:00Z', 3);   -- 3 格子ぶん取り込み済み
select pg_temp.archived('2026-09-10T01:00Z', 3);   -- まだ 0 行
select pg_temp.archived('2026-09-10T02:00Z', 3, 'failed');  -- 失敗した取得

select is(
  (select count(*)::integer from public.v_weather_pending),
  1, '取り込み済みの発行は出ない。未処理の 1 件だけ'
);
select is(
  (select issued_hour from public.v_weather_pending),
  '2026-09-10T01:00Z'::timestamptz, '未処理なのは 01:00 の発行'
);
select is(
  (select n_loaded from public.v_weather_pending),
  0, 'まだ 1 行も入っていない'
);
-- **途中まで入った発行も未処理として出る**（6 分割のうち一部だけ入った、など）
select public.upsert_weather_hourly(pg_temp.forecast_rows('2026-09-10T01:00Z', 2));
select is(
  (select n_loaded from public.v_weather_pending where issued_hour = '2026-09-10T01:00Z'),
  2, '途中まで入った発行は「2/3」として残る'
);
select is(
  (select count(*)::integer from public.v_weather_pending
    where issued_hour = '2026-09-10T02:00Z'),
  0, '失敗した取得は未処理に出さない（ファイルが無い）'
);

-- ────────────────────────────────────────────────────────────────
-- 保守 — 30 日で消す
-- ────────────────────────────────────────────────────────────────
select public.upsert_weather_hourly(pg_temp.forecast_rows(now() - interval '31 days', 2));
select public.upsert_weather_hourly(pg_temp.forecast_rows(now() - interval '29 days', 2));
select is((select count(*)::integer from public.weather_hourly), 9, '古い行も含めて 9 行');

select is(
  (public.run_maintenance(60) ->> 'weather_rows_deleted')::integer,
  2, '30 日より古い発行だけが消える'
);
select is(
  (select count(*)::integer from public.weather_hourly
    where issued_hour < now() - interval '30 days'),
  0, '古い行は残っていない'
);
select cmp_ok(
  (select count(*)::integer from public.weather_hourly), '=', 7,
  '残るのは 29 日前の 2 行と今日の 5 行'
);

-- ────────────────────────────────────────────────────────────────
-- 保守 — **`job_runs` は消さない**（W4 プラン §6.8 の PR L）
-- ────────────────────────────────────────────────────────────────
-- 30 日で消えた発行を**生アーカイブから戻せる**のは、`v_weather_files` が
-- `job_runs` の `archive_weather` の記録を覚えているからである（0023）。
-- **ここを刈ると、生アーカイブ（無期限）が在っても `load_weather` が発行を
-- 見つけられなくなる。** `job_runs` はいま誰も消していないが、行は毎日 2,100 件
-- 増えるので、**刈りたくなる日が来る**。そのとき落ちるように、振る舞いで固定する。
select pg_temp.archived(now() - interval '400 days', 3);
select public.run_maintenance(60);

select is(
  (select count(*)::integer from public.job_runs
    where job_name = 'archive_weather' and started_at < now() - interval '365 days'),
  1, '**400 日前の archive_weather の記録は保守で消えない**（戻す入口が消える）'
);
select is(
  (select count(*)::integer from public.v_weather_files
    where forecast_hour < now() - interval '365 days'),
  1, '保持を過ぎた発行も v_weather_files からは見える'
);
select is(
  (select count(*)::integer from public.v_weather_pending
    where issued_hour < now() - interval '365 days'),
  1, '取り込まれていないので「未処理」に出る（load_weather --since で戻せる）'
);

-- ────────────────────────────────────────────────────────────────
-- 監視 — ジョブを足したら見張りにも足す（§12 の 116）
-- ────────────────────────────────────────────────────────────────
select is(
  (select is_active from public.monitored_jobs where job_name = 'load_weather'),
  true, 'load_weather が監視に入っている'
);
select is(
  (select cron_job_name from public.monitored_jobs where job_name = 'load_weather'),
  null, 'pg_cron ではないので cron_job_name は NULL'
);

select * from finish();
rollback;
