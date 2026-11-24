-- pgTAP: 天気の取り直しの回（毎時 47 分。W12 プランの W12-04、0063）
--
-- 取り直しの回も `archive_weather` として記録し、`detail.run_kind` で分ける。ここで見るのは
-- **DB 側の契約**（取るかどうかの判定は `apps/web/lib/jobs/archive-weather.test.ts`）：
--
--   1. 取り直しで保存した発行は `v_weather_files` に載り、取り込み（`v_weather_pending`）が 1 度だけ
--      拾う。入手した時刻（`available_at`）は**取り直しの回の終わり**で、落ちた定時の回の時刻は
--      混ざらない（W7-01）
--   2. 何もしなかった回（`skipped`。`hour_epoch_s` の鍵なし）は、発行として載らない
--   3. 通知の文が、取り直しを書き、取り直しも落ちたときに取り返せないことを残す

begin;
select plan(8);

delete from public.weather_hourly;
delete from public.job_runs;

create function pg_temp.hour_s(p_hour timestamptz) returns bigint language sql as $$
  select extract(epoch from p_hour)::bigint;
$$;

-- 05:00 の発行：定時の回（05:17）は 1 つも保存できず、取り直しの回（05:47）が 7 分割を保存した。
-- 06:00 の発行：定時の回が保存でき、取り直しの回は何もしなかった
insert into public.job_runs (job_name, started_at, finished_at, status, detail) values
  ('archive_weather', timestamptz '2026-11-25 05:17:30+00', timestamptz '2026-11-25 05:21:50+00',
   'failed', jsonb_build_object('run_kind', 'scheduled',
     'hour_epoch_s', pg_temp.hour_s('2026-11-25 05:00+00'), 'n_cells', 623, 'n_saved', 0,
     'n_failed', 7)),
  ('archive_weather', timestamptz '2026-11-25 05:47:30+00', timestamptz '2026-11-25 05:48:40+00',
   'ok', jsonb_build_object('run_kind', 'catch_up',
     'hour_epoch_s', pg_temp.hour_s('2026-11-25 05:00+00'), 'n_cells', 623, 'n_saved', 7,
     'n_failed', 0)),
  ('archive_weather', timestamptz '2026-11-25 06:17:30+00', timestamptz '2026-11-25 06:18:40+00',
   'ok', jsonb_build_object('run_kind', 'scheduled',
     'hour_epoch_s', pg_temp.hour_s('2026-11-25 06:00+00'), 'n_cells', 623, 'n_saved', 7,
     'n_failed', 0)),
  ('archive_weather', timestamptz '2026-11-25 06:47:30+00', timestamptz '2026-11-25 06:47:31+00',
   'skipped', jsonb_build_object('run_kind', 'catch_up', 'reason', 'already_saved',
     'checked_hour_epoch_s', pg_temp.hour_s('2026-11-25 06:00+00')));

-- ────────────────────────────────────────────────────────────────
-- 1・2. 発行の一覧と、取り込みが拾うもの
-- ────────────────────────────────────────────────────────────────
select is((select count(*)::int from public.v_weather_files), 3,
  '発行として載るのは hour_epoch_s の鍵がある 3 行（何もしなかった回は載らない）');
select is(
  (select count(*)::int from public.v_weather_files
    where hour_epoch_s = pg_temp.hour_s('2026-11-25 06:00+00')),
  1, '06:00 は定時の回の 1 行だけ（取り直しの回の skipped は数えない）');
select is(
  (select count(*)::int from public.v_weather_pending
    where hour_epoch_s = pg_temp.hour_s('2026-11-25 05:00+00')),
  1, '05:00 は取り込みが 1 度だけ拾う（1 つも保存できなかった定時の回は拾わない）');
select is(
  (select available_at from public.v_weather_pending
    where hour_epoch_s = pg_temp.hour_s('2026-11-25 05:00+00')),
  timestamptz '2026-11-25 05:48:40+00',
  '**入手した時刻は取り直しの回の終わり**（1 つの発行に 1 つの時刻。W7-01）');
select is(
  (select available_at from public.v_weather_pending
    where hour_epoch_s = pg_temp.hour_s('2026-11-25 06:00+00')),
  timestamptz '2026-11-25 06:18:40+00', '保存できた時間は、いままでどおり定時の回の時刻');

-- ────────────────────────────────────────────────────────────────
-- 3. 通知の文（0041 の failure_note。0063 で直した）
-- ────────────────────────────────────────────────────────────────
select alike(
  (select failure_note from public.monitored_jobs where job_name = 'archive_weather'),
  '%47 分の回が 1 度だけ取り直します%',
  '定時の回の失敗の通知に、取り直しの回があることを書く');
select alike(
  (select failure_note from public.monitored_jobs where job_name = 'archive_weather'),
  '%取り直しも落ちた時刻の予報は二度と取れません%',
  '取り直しも落ちたときに取り返せないことは残す');
select alike(
  (select note from public.monitored_jobs where job_name = 'archive_weather'),
  '%:47%', '説明の欄にも 47 分の回を書く');

select * from finish();
rollback;
