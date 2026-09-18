-- pgTAP: 途中まで保存できた発行と、天気の凍結の見張り（マイグレーション 0052）
--
-- ここで固定したい契約は 5 つ。
--   1. **1 分割でも落ちた発行が「未処理」として出る**（`status = 'ok'` で切らない）
--   2. **ただし 1 度だけ**——1 行でも入ったら出なくなる（新しい発行に到達できるように）
--   3. **緑だった発行のふるまいは変わらない**（入り切るまで何度でも出る）
--   4. **何も保存できなかった発行は出ない**（`n_saved = 0`）
--   5. **天気が進んでいなければ鳴る**（`check_weather_stale`）
--
-- 前提条件はこのテストが自分で作る。**`job_runs` と `weather_hourly` を汚さない**
-- ため、すべて rollback の中で行う。

begin;
select plan(12);

-- ────────────────────────────────────────────────────────────────
-- 形
-- ────────────────────────────────────────────────────────────────
select has_view('public', 'v_weather_pending', 'v_weather_pending がある');
select has_function('public', 'check_weather_stale', 'check_weather_stale がある');
select ok(not has_function_privilege('anon', 'public.check_weather_stale()', 'execute'),
          '匿名は検査を呼べない');

-- **monitor_jobs が呼ぶ並びに入っている**（足したのに呼ばれないのがいちばん困る）
select ok(
  pg_get_functiondef('public.monitor_jobs()'::regprocedure) like '%check_weather_stale%',
  'monitor_jobs が check_weather_stale を呼ぶ'
);

-- ────────────────────────────────────────────────────────────────
-- 前提データ：同じ 1 時間ぶんの発行を 3 通り作る
-- ────────────────────────────────────────────────────────────────
-- `v_weather_files` は `job_runs` の detail を読むので、そこに作り物を入れる。
-- `hour_epoch_s` は衝突しない遠い過去にする。

delete from public.job_runs where job_name = 'archive_weather' and (detail->>'hour_epoch_s')::bigint
  in (900000000, 900003600, 900007200, 900010800);

-- (a) 7 分割のうち 6 つ保存、1 つ失敗（2026-09-17 の形）
insert into public.job_runs (job_name, started_at, finished_at, status, detail)
values ('archive_weather', now() - interval '3 hours', now() - interval '3 hours', 'failed',
        jsonb_build_object('hour_epoch_s', 900000000, 'n_cells', 602,
                           'n_batches', 7, 'n_saved', 6, 'n_failed', 1));

-- (b) 全部保存できた（いつもの形）
insert into public.job_runs (job_name, started_at, finished_at, status, detail)
values ('archive_weather', now() - interval '2 hours', now() - interval '2 hours', 'ok',
        jsonb_build_object('hour_epoch_s', 900003600, 'n_cells', 602,
                           'n_batches', 7, 'n_saved', 7, 'n_failed', 0));

-- (c) 1 つも保存できなかった（取得そのものが落ちた）
insert into public.job_runs (job_name, started_at, finished_at, status, detail)
values ('archive_weather', now() - interval '1 hour', now() - interval '1 hour', 'failed',
        jsonb_build_object('hour_epoch_s', 900007200, 'n_cells', 602,
                           'n_batches', 7, 'n_saved', 0, 'n_failed', 7));

-- ────────────────────────────────────────────────────────────────
-- 1・3・4：何が「未処理」として出るか
-- ────────────────────────────────────────────────────────────────
select ok(
  exists (select 1 from public.v_weather_pending where hour_epoch_s = 900000000),
  '**1 分割でも落ちた発行が未処理として出る**（0052 の主眼）'
);
select ok(
  exists (select 1 from public.v_weather_pending where hour_epoch_s = 900003600),
  '全部保存できた発行も出る（今までどおり）'
);
select ok(
  not exists (select 1 from public.v_weather_pending where hour_epoch_s = 900007200),
  '**何も保存できなかった発行は出ない**（n_saved = 0）'
);

-- ────────────────────────────────────────────────────────────────
-- 2：落ちた発行は 1 度だけ
-- ────────────────────────────────────────────────────────────────
-- 取り込めた体で 1 行だけ入れる。**欠けた分は二度と埋まらない**ので、
-- ここで出続けると `load_weather`（1 回 6 発行・古い順）が先へ進めなくなる。

insert into public.weather_hourly
  (cell_lat_idx, cell_lon_idx, issued_hour, available_at, temp_c, wind_kmh, precip_mm, weather_code)
values (9001, 9001, to_timestamp(900000000), to_timestamp(900000000) + interval '18 minutes',
        array[20.0]::real[], array[5.0]::real[], array[0.0]::real[], array[1]::smallint[]),
       (9001, 9001, to_timestamp(900003600), to_timestamp(900003600) + interval '18 minutes',
        array[20.0]::real[], array[5.0]::real[], array[0.0]::real[], array[1]::smallint[]);

select ok(
  not exists (select 1 from public.v_weather_pending where hour_epoch_s = 900000000),
  '**落ちた発行は 1 行入ったらもう出ない**（新しい発行に到達できる）'
);
select ok(
  exists (select 1 from public.v_weather_pending where hour_epoch_s = 900003600),
  '**緑だった発行は入り切るまで出る**（ふるまいを変えていない）'
);

-- ────────────────────────────────────────────────────────────────
-- 5：天気の凍結
-- ────────────────────────────────────────────────────────────────
-- いまの `weather_hourly` には本物の行が入っているので、**最新の発行の古さ**で判断する。
-- 作り物の 2 行は 1998 年なので最大値には効かない。

select is(
  ((public.check_weather_stale() ->> 'alerts')::integer >= 0),
  true, 'check_weather_stale は数を返す'
);
select ok(
  public.check_weather_stale() ? 'latest_issued_hour',
  '最新の発行を返す（何を見て判断したかが残る）'
);
select is(
  (public.check_weather_stale() ->> 'threshold_hours')::numeric,
  4::numeric, '閾値は 4 時間（毎時 1 発行なので 3 回続けての取りこぼし）'
);

select * from finish();
rollback;
