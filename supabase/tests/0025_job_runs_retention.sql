-- pgTAP: job_runs の保持（マイグレーション 0057、W8 プランの W8-07）
--
-- ここで固定したい契約は 4 つ。
--   1. **消すのは `watchdog_collect`・`monitor_feeds` の `ok` で 30 日より古い行だけ**
--   2. **`archive_weather` は古くても消さない**（天気の「使える時刻」の記録。v_weather_files）
--   3. **失敗・skipped・ほかのジョブ（`monitor_jobs` など）は消さない**
--   4. **消した数を保守の記録（`job_runs_deleted`）に残す**
--
-- **日が経っても落ちない形にする**（W7 プランの所見 225）：取引の中で表を空にし、`now()` からの
-- 相対の時刻で自分が入れた行だけを数える。`now()` は取引の中で固定される（0011 と同じ前提）。

begin;
select plan(11);

delete from public.job_runs;

-- `p_age` 前に始まり、1 分後に終わった記録を 1 行作る
create function pg_temp.run(p_job text, p_status text, p_age interval, p_detail jsonb default null)
returns void language sql as $$
  insert into public.job_runs (job_name, status, started_at, finished_at, detail)
  values (p_job, p_status, now() - p_age, now() - p_age + interval '1 minute', p_detail);
$$;

create function pg_temp.left_of(p_job text, p_status text) returns integer language sql as $$
  select count(*)::int from public.job_runs where job_name = p_job and status = p_status;
$$;

-- 消える 3 行
select pg_temp.run('watchdog_collect', 'ok', interval '31 days');
select pg_temp.run('watchdog_collect', 'ok', interval '45 days');
select pg_temp.run('monitor_feeds', 'ok', interval '31 days');
-- 残る行：30 日より新しい・失敗・skipped・天気の発行・ほかの見張り
select pg_temp.run('watchdog_collect', 'ok', interval '29 days');
select pg_temp.run('monitor_feeds', 'ok', interval '1 day');
select pg_temp.run('watchdog_collect', 'failed', interval '31 days', '{"error": "x"}');
select pg_temp.run('monitor_feeds', 'skipped', interval '31 days');
select pg_temp.run('archive_weather', 'ok', interval '31 days', '{"hour_epoch_s": 1}');
select pg_temp.run('monitor_jobs', 'ok', interval '31 days');

select lives_ok('select public.run_maintenance()', 'run_maintenance は例外を投げない');
select is(
  (select (detail ->> 'job_runs_deleted')::int from public.job_runs
    where job_name = 'maintain_partitions' order by id desc limit 1),
  3, '保守の記録に、消した数（30 日より古い見張りの ok の 3 行）が残る'
);
select is(pg_temp.left_of('watchdog_collect', 'ok'), 1, 'watchdog_collect の ok は 29 日前の 1 行だけ残る');
select is(pg_temp.left_of('monitor_feeds', 'ok'), 1, 'monitor_feeds の ok は 1 日前の 1 行だけ残る');
select is(pg_temp.left_of('watchdog_collect', 'failed'), 1, '失敗の行は古くても残る');
select is(pg_temp.left_of('monitor_feeds', 'skipped'), 1, 'skipped の行は古くても残る');
select is(pg_temp.left_of('archive_weather', 'ok'), 1, 'archive_weather は古くても残る（天気の「使える時刻」の記録）');
select is(
  (select count(*)::int from public.v_weather_files),
  1, '残した archive_weather の行は「発行」として読める'
);
select is(pg_temp.left_of('monitor_jobs', 'ok'), 1, 'monitor_jobs は消さない（W8-07）');
select is(
  (select status from public.job_runs where job_name = 'maintain_partitions' order by id desc limit 1),
  'ok', '保守の自分の行は ok で閉じる'
);
select lives_ok('select public.run_maintenance()', '2 回目も例外を投げない（消すものが無くても）');

select * from finish();
rollback;
