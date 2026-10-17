-- pgTAP: running のまま残った job_runs の見張りと、保守が閉じること（マイグレーション 0056）
--
-- ここで固定したい契約は 5 つ（W7 プランの W7-02、W6 プランの所見 220・223）。
--   1. **閾値（1 時間）を過ぎた running だけを知らせる**——新しい running は実行中かもしれない
--   2. **ジョブ名ごとに鍵を分け、6 時間に 1 回**（別のジョブが続けて止まっても隠れない）
--   3. **保守が failed・never_finished で閉じる。消さない**（0032 と同じ）
--   4. **閉じた archive_weather の行は、天気の「使える時刻」（v_weather_files）に現れない**
--   5. **保守は、いま走っている自分の行を閉じない**
--
-- `now()` はトランザクションの中で固定される（0011 と同じ前提）。前提はこのテストが
-- 自分で作り、すべて rollback の中で行う。

begin;
select plan(20);

delete from public.alert_state;
delete from public.job_runs;
delete from vault.secrets where name in ('cron_secret', 'alert_webhook_url');

-- `p_age` 前に始まった記録を 1 行作る。running 以外は 1 分後に終わったことにする
create function pg_temp.run(p_job text, p_status text, p_age interval, p_detail jsonb default null)
returns void language sql as $$
  insert into public.job_runs (job_name, status, started_at, finished_at, detail)
  values (p_job, p_status, now() - p_age,
          case when p_status = 'running' then null else now() - p_age + interval '1 minute' end,
          p_detail);
$$;

create function pg_temp.stuck() returns jsonb language sql as $$
  select public.check_jobs_stuck();
$$;

-- ────────────────────────────────────────────────────────────────
-- 関数の形と権限（0011 の作法）
-- ────────────────────────────────────────────────────────────────
select has_function('public', 'check_jobs_stuck', 'check_jobs_stuck がある');
select is(
  (select p.prosecdef from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'check_jobs_stuck'),
  true, 'security definer'
);
select is(
  (select p.proconfig @> array['search_path=""'] from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'check_jobs_stuck'),
  true, 'search_path = ''''（スキーマ付きで書く前提）'
);
select is(
  has_function_privilege('anon', 'public.check_jobs_stuck()', 'execute'),
  false, '匿名は実行できない'
);
select is(
  (select value from public.app_config where key = 'job_stuck_s'),
  '3600', '既定の閾値は 1 時間（いちばん長いジョブ 6.6 分の 9 倍）'
);

-- ────────────────────────────────────────────────────────────────
-- 1・2：閾値を過ぎた running だけを、ジョブ名ごとに知らせる
-- ────────────────────────────────────────────────────────────────
select pg_temp.run('archive_weather', 'running', interval '30 minutes');
select is(pg_temp.stuck() -> 'alerts', '0'::jsonb, '30 分前の running は鳴らない（実行中かもしれない）');

select pg_temp.run('archive_weather', 'running', interval '2 hours');
select pg_temp.run('archive_weather', 'running', interval '26 hours');
select pg_temp.run('evaluate_daily', 'running', interval '30 days');
select pg_temp.run('archive_weather', 'ok', interval '3 hours', '{"hour_epoch_s": 1}');
select pg_temp.run('archive_weather', 'failed', interval '3 hours');

select is(pg_temp.stuck() -> 'jobs', '2'::jsonb, '止まったジョブは 2 つ（ok と failed の行は数えない）');
select is(pg_temp.stuck() -> 'alerts', '0'::jsonb, '続けて呼んでも 2 通目は出ない（6 時間の抑制）');
select is(
  (select array_agg(alert_key order by alert_key) from public.alert_state),
  array['job_stuck:archive_weather', 'job_stuck:evaluate_daily'],
  '鍵はジョブ名ごと'
);
select is(
  (select last_value ->> 'count' from public.alert_state where alert_key = 'job_stuck:archive_weather'),
  '2', '通知に件数が入る（30 分前の行は入らない）'
);

update public.alert_state set last_sent_at = now() - interval '7 hours';
select is(pg_temp.stuck() -> 'alerts', '2'::jsonb, '6 時間を越えれば また鳴る');

-- 閾値は app_config から読む
update public.app_config set value = '600' where key = 'job_stuck_s';
delete from public.alert_state;
select is(pg_temp.stuck() ->> 'threshold_s', '600', '閾値を 10 分に縮めると、その値で数える');
select is(
  (select last_value ->> 'count' from public.alert_state where alert_key = 'job_stuck:archive_weather'),
  '3', '10 分なら 30 分前の行も止まったと数える'
);
update public.app_config set value = '3600' where key = 'job_stuck_s';

-- ────────────────────────────────────────────────────────────────
-- 3・4・5：保守が閉じる
-- ────────────────────────────────────────────────────────────────
select lives_ok('select public.run_maintenance()', 'run_maintenance は例外を投げない');
select is(
  (select (detail ->> 'job_runs_closed')::int from public.job_runs
    where job_name = 'maintain_partitions' order by id desc limit 1),
  3, '閉じたのは 1 時間を過ぎた running の 3 行'
);
select is(
  (select count(*)::int from public.job_runs
    where status = 'failed' and detail ->> 'error' = 'never_finished' and finished_at is not null),
  3, 'failed・never_finished・終わった時刻つきで閉じる（消さない）'
);
select is(
  (select count(*)::int from public.job_runs
    where job_name = 'archive_weather' and status = 'running'),
  1, '30 分前の running は閉じない'
);
select is(
  (select status from public.job_runs where job_name = 'maintain_partitions' order by id desc limit 1),
  'ok', '保守は、いま走っている自分の行を閉じない'
);
select is(
  (select count(*)::int from public.v_weather_files),
  1, '閉じた archive_weather の行は「発行」に現れない（hour_epoch_s のある ok の 1 行だけ）'
);
select is(pg_temp.stuck() -> 'jobs', '0'::jsonb, '閉じたあとは鳴らない');

select * from finish();
rollback;
