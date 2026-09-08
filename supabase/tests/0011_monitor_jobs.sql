-- pgTAP: ジョブの監視（W3 プラン §5.3、マイグレーション 0020）
--
-- **このテストは自分の前提を自分で作る。** ローカルでは pg_cron が動いていて
-- `job_runs` に行が増え続けるので、件数を数える検査は「自分で入れた行」だけに
-- 閉じる（`monitored_jobs` を全部 is_active = false にしてから必要な行を足す）。
--
-- pg_net の送信はコミット後なので、rollback するここでは Webhook に届かない。
-- 確かめられるのは記録と抑制の論理まで（0007 と同じ）。

begin;
select plan(61);

-- 自分の前提を作る
delete from public.status_snapshots;
delete from public.job_runs;
delete from public.alert_state;
delete from vault.secrets where name in ('cron_secret', 'alert_webhook_url');

-- **`storage.objects` からは直接 DELETE できない**（`storage.protect_delete()` が止める。
-- 「Storage API を使え」というトリガで、孤児オブジェクトを防ぐためのもの）。したがって
-- ここは「消してから始める」ができない。前提を**表明**して始める（W3 プラン §12 の 81）。
select is(
  (select count(*)::int from storage.objects where bucket_id = 'gbfs-parquet'),
  0, '前提：ローカルの gbfs-parquet は空（storage.objects は削除できないので表明で始める）'
);

create function pg_temp.alerts() returns text language sql as $$
  select coalesce(string_agg(alert_key, ',' order by alert_key), '(なし)') from public.alert_state;
$$;

create function pg_temp.has_alert(p_key text) returns boolean language sql as $$
  select exists (select 1 from public.alert_state where alert_key = p_key);
$$;

create function pg_temp.job_status(p_name text) returns text language sql as $$
  select status from public.job_runs where job_name = p_name order by id desc limit 1;
$$;

-- UTC の「時」に丸めた時刻から、Parquet のオブジェクト名を組み立てる
create function pg_temp.parquet_name(p_system text, p_hour timestamp) returns text language sql as $$
  select p_system || '/date=' || to_char(p_hour, 'YYYY-MM-DD')
                  || '/hour=' || to_char(p_hour, 'HH24') || '/part.parquet';
$$;

-- 直近 6 時間の窓に確実に入る時刻（UTC の時に丸めた値）
create function pg_temp.window_hour() returns timestamp language sql stable as $$
  select date_trunc('hour', (now() at time zone 'UTC')) - interval '3 hours';
$$;

create function pg_temp.put_snapshot(p_system text, p_hour timestamp) returns void language sql as $$
  insert into public.status_snapshots
    (system_id, observed_at, fetched_at, n_stations, is_anomalous, bikes, docks, flags, reported_age_s, raw_path)
  values (p_system, (p_hour at time zone 'UTC') + interval '10 minutes',
          (p_hour at time zone 'UTC') + interval '10 minutes',
          1, false, '{1}'::smallint[], '{1}'::smallint[], '{7}'::smallint[], '{0}'::smallint[], 'test');
$$;

-- ────────────────────────────────────────────────────────────────
-- 構造
-- ────────────────────────────────────────────────────────────────
select has_table('public', 'monitored_jobs', 'monitored_jobs がある');
select col_is_pk('public', 'monitored_jobs', 'job_name', '主キーはジョブ名');
select is(
  (select relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relname = 'monitored_jobs'),
  true, 'monitored_jobs は RLS 有効（CLAUDE.md §5）'
);
select is(
  has_table_privilege('anon', 'public.monitored_jobs', 'select'),
  false, '匿名は monitored_jobs を読めない'
);

select has_function('public', 'check_jobs_missing', 'check_jobs_missing がある');
select has_function('public', 'check_jobs_failed', 'check_jobs_failed がある');
select has_function('public', 'check_parquet_gap', 'check_parquet_gap がある');
select has_function('public', 'check_reference_data', 'check_reference_data がある');
select has_function('public', 'monitor_jobs', 'monitor_jobs がある');

-- security definer かつ search_path='' でなければ、本番と権限の効き方が変わる
select is(
  (select bool_and(p.prosecdef) from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public'
      and p.proname in ('check_jobs_missing','check_jobs_failed','check_parquet_gap',
                        'check_reference_data','monitor_jobs')),
  true, '5 つとも security definer'
);
select is(
  (select bool_and(p.proconfig @> array['search_path=""']) from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public'
      and p.proname in ('check_jobs_missing','check_jobs_failed','check_parquet_gap',
                        'check_reference_data','monitor_jobs')),
  true, '5 つとも search_path = ''''（スキーマ付きで書く前提）'
);
select is(
  has_function_privilege('anon', 'public.monitor_jobs()', 'execute'),
  false, '匿名は monitor_jobs を実行できない'
);

-- pg_cron の登録。monitor_feeds（:00,:05…）・compact（:07）・weather（:17）と重ねない
select is(
  (select schedule from cron.job where jobname = 'monitor_jobs'),
  '4-59/5 * * * *', 'monitor_jobs は :04 起点の 5 分毎'
);
select is((select active from cron.job where jobname = 'monitor_jobs'), true, 'monitor_jobs は有効');

-- ────────────────────────────────────────────────────────────────
-- 設計が成り立つ前提
-- ────────────────────────────────────────────────────────────────
-- **これが読めないと Parquet の欠落検査は作れない。** postgres は BYPASSRLS を持つ
select lives_ok(
  'select count(*) from storage.objects',
  'postgres は storage.objects を読める（BYPASSRLS）'
);
select is(
  (select rolbypassrls from pg_roles where rolname = 'postgres'),
  true, 'postgres は BYPASSRLS（security definer の中から全行が見える）'
);

-- 0020 で 9 本、0021 が trigger_backup_collect を、0022 が daily_quality を足して 11 本
select is((select count(*)::int from public.monitored_jobs), 11, '監視対象は 11 ジョブ');
select is(
  (select array_agg(job_name order by job_name) from public.monitored_jobs where not is_active),
  array['trigger_backup_collect'],
  '成功の有無を見ないのは trigger_backup_collect だけ（発火したときしか記録しないため。0021）'
);
select is(
  (select bool_and(missing_after > expected_every) from public.monitored_jobs),
  true, '閾値は必ず周期より長い（短いと鳴り続ける）'
);
select throws_ok(
  $$insert into public.monitored_jobs (job_name, expected_every, missing_after)
    values ('bad', interval '1 hour', interval '10 minutes')$$,
  '23514', null, '閾値が周期より短い行は制約で弾かれる'
);

-- ────────────────────────────────────────────────────────────────
-- 検査 1：直近 N 時間に成功が無い
-- ────────────────────────────────────────────────────────────────
-- 以降は自分で入れた 1 行だけを見る（ローカルの pg_cron に影響されないため）
update public.monitored_jobs set is_active = false;
insert into public.monitored_jobs (job_name, expected_every, missing_after, note)
values ('pgtap_fake_job', interval '1 hour', interval '3 hours', 'テスト用');

select is(
  public.check_jobs_missing() -> 'checked', '0'::jsonb,
  '登録直後は見ない（まだ 1 度も回っていないだけかもしれない）'
);
select is(pg_temp.alerts(), '(なし)', '登録直後は通知しない');

update public.monitored_jobs set added_at = now() - interval '10 days';
select is(public.check_jobs_missing() -> 'checked', '1'::jsonb, '猶予を過ぎたジョブは見る');
select ok(pg_temp.has_alert('job_missing:pgtap_fake_job'), '成功が 1 件も無ければ通知する');

select is(public.check_jobs_missing() -> 'alerts', '0'::jsonb, '2 回目は抑制されて送らない');

delete from public.alert_state;
insert into public.job_runs (job_name, started_at, finished_at, status)
values ('pgtap_fake_job', now() - interval '1 minute', now(), 'ok');
select is(public.check_jobs_missing() -> 'alerts', '0'::jsonb, '直近に成功があれば鳴らない');
select is(pg_temp.alerts(), '(なし)', '同上（通知も残らない）');

delete from public.job_runs;
insert into public.job_runs (job_name, started_at, finished_at, status)
values ('pgtap_fake_job', now() - interval '4 hours', now() - interval '4 hours', 'ok');
select ok(
  public.check_jobs_missing() -> 'alerts' = '1'::jsonb,
  '閾値より古い成功しか無ければ鳴る（3 時間の閾値に対し 4 時間前）'
);
select is(
  (select last_value->>'last_ok_at' is not null from public.alert_state
    where alert_key = 'job_missing:pgtap_fake_job'),
  true, '通知に最後に成功した時刻が入る'
);

-- ────────────────────────────────────────────────────────────────
-- 検査 2：失敗したジョブ
-- ────────────────────────────────────────────────────────────────
delete from public.job_runs; delete from public.alert_state;
select is(public.check_jobs_failed() -> 'alerts', '0'::jsonb, '失敗が無ければ鳴らない');

insert into public.job_runs (job_name, started_at, finished_at, status, detail)
values ('pgtap_fake_job', now() - interval '10 minutes', now(), 'failed',
        jsonb_build_object('error', 'boom', 'code', 'XX000'));
select is(public.check_jobs_failed() -> 'alerts', '1'::jsonb, '直近 3 時間の失敗で鳴る');
select is(
  (select last_value->>'last_error' from public.alert_state where alert_key = 'job_failed:pgtap_fake_job'),
  'boom', '通知に最後のエラー文が入る'
);

delete from public.job_runs; delete from public.alert_state;
insert into public.job_runs (job_name, started_at, finished_at, status)
values ('pgtap_fake_job', now() - interval '5 hours', now() - interval '5 hours', 'failed');
select is(public.check_jobs_failed() -> 'alerts', '0'::jsonb, '3 時間より古い失敗では鳴らない');

-- **monitored_jobs に入っていないジョブの失敗も拾う**（どこで落ちても知りたい）
delete from public.job_runs; delete from public.alert_state;
insert into public.job_runs (job_name, started_at, finished_at, status)
values ('not_monitored_job', now() - interval '1 minute', now(), 'failed');
select is(public.check_jobs_failed() -> 'failed_jobs', '1'::jsonb, '監視対象表に無いジョブも数える');
select ok(pg_temp.has_alert('job_failed:not_monitored_job'), '監視対象表に無いジョブの失敗も拾う');

-- ────────────────────────────────────────────────────────────────
-- 検査 3：Parquet の欠落
-- ────────────────────────────────────────────────────────────────
delete from public.job_runs; delete from public.alert_state;
select is(public.check_parquet_gap() -> 'missing', '0'::jsonb, 'スナップショットが無ければ欠落も無い');

select pg_temp.put_snapshot('hellocycling', pg_temp.window_hour());
select pg_temp.put_snapshot('docomo-cycle', pg_temp.window_hour());
select is(public.check_parquet_gap() -> 'checked', '2'::jsonb, '2 系統 × 1 時間の組が対象になる');
select is(public.check_parquet_gap() -> 'missing', '2'::jsonb, 'Parquet が無ければ 2 件の欠落');
select ok(pg_temp.has_alert('parquet_gap'), '欠落があれば通知する');
select is(
  (select last_value->'missing'->>0 from public.alert_state where alert_key = 'parquet_gap'),
  'docomo-cycle ' || to_char(pg_temp.window_hour(), 'YYYY-MM-DD HH24') || 'Z',
  '通知に「どの系統のどの時間か」が入る'
);

delete from public.alert_state;
insert into storage.objects (bucket_id, name, metadata)
values ('gbfs-parquet', pg_temp.parquet_name('hellocycling', pg_temp.window_hour()), '{"size":1}'::jsonb);
select is(public.check_parquet_gap() -> 'missing', '1'::jsonb, '置いたぶんは欠落から外れる');

insert into storage.objects (bucket_id, name, metadata)
values ('gbfs-parquet', pg_temp.parquet_name('docomo-cycle', pg_temp.window_hour()), '{"size":1}'::jsonb);
delete from public.alert_state;
select is(public.check_parquet_gap() -> 'missing', '0'::jsonb, '両方置けば欠落なし');
select is(pg_temp.alerts(), '(なし)', '同上（通知も出ない）');

-- **直前の 1 時間は猶予**：毎時ジョブは :07 に前 1 時間を畳むので、:00〜:07 は
-- 「まだ無い」のが正常。猶予が無いと毎時 7 分間だけ誤報が出る
delete from public.status_snapshots; delete from public.alert_state;
select pg_temp.put_snapshot('hellocycling',
  date_trunc('hour', (now() at time zone 'UTC')) - interval '30 minutes');
select is(
  public.check_parquet_gap() -> 'checked', '0'::jsonb,
  '直前の 1 時間は対象にしない（畳む前でも鳴らない）'
);

-- 7 時間より古いものも見ない（全期間の突き合わせは重い）
delete from public.status_snapshots;
select pg_temp.put_snapshot('hellocycling',
  date_trunc('hour', (now() at time zone 'UTC')) - interval '10 hours');
select is(public.check_parquet_gap() -> 'checked', '0'::jsonb, '窓より古い時間は見ない');

-- ────────────────────────────────────────────────────────────────
-- 検査 4：参照データの期限
-- ────────────────────────────────────────────────────────────────
delete from public.status_snapshots; delete from public.alert_state;
select is(
  public.check_reference_data() -> 'skipped', 'true'::jsonb,
  'jp_holidays がまだ無ければ飛ばす（PR D で作る）'
);
select is(pg_temp.alerts(), '(なし)', '飛ばしたときは通知しない');

create table public.jp_holidays (holiday_date date primary key, name text not null);
select is(
  public.check_reference_data() -> 'skipped', 'true'::jsonb,
  'jp_holidays が空でも飛ばす'
);

insert into public.jp_holidays values (current_date + 30, 'テスト');
select is(public.check_reference_data() -> 'days_left', '30'::jsonb, '残り日数を数える');
select ok(pg_temp.has_alert('holidays_expiring'), '残り 90 日を切ったら通知する');

delete from public.alert_state;
insert into public.jp_holidays values (current_date + 400, 'テスト');
select is(public.check_reference_data() -> 'alerts', '0'::jsonb, '十分先まであれば鳴らない');
drop table public.jp_holidays;

-- ────────────────────────────────────────────────────────────────
-- 親：検査どうしが独立していること
-- ────────────────────────────────────────────────────────────────
delete from public.job_runs; delete from public.alert_state;
select lives_ok('select public.monitor_jobs()', '正常時は例外を投げない');
select is(pg_temp.job_status('monitor_jobs'), 'ok', '正常時の status は ok');
select is(
  (select jsonb_object_keys_count from (
     select count(*)::int as jsonb_object_keys_count
       from jsonb_object_keys((select detail->'checks' from public.job_runs
                                where job_name='monitor_jobs' order by id desc limit 1))) t),
  5, '5 つの検査すべてが detail に残る（0021 で check_cron_jobs を足した）'
);

-- **1 つ壊しても他は走る。** 0009 の monitor_feeds は全検査を 1 つの exception で
-- 包んでいるため、この性質を持たない（だから別関数にした。W3-02）
delete from public.job_runs; delete from public.alert_state;
alter table public.monitored_jobs rename to monitored_jobs_hidden;
select lives_ok('select public.monitor_jobs()', '検査が 1 つ壊れても親は例外を投げない');
select is(
  (select detail->'checks'->'check_jobs_missing'->>'code' from public.job_runs
    where job_name='monitor_jobs' order by id desc limit 1),
  '42P01', '壊れた検査の SQLSTATE が detail に残る'
);
select is(
  (select detail->'checks'->'check_parquet_gap'->>'missing' from public.job_runs
    where job_name='monitor_jobs' order by id desc limit 1),
  '0', '壊れていない検査は走り続ける'
);
select is(pg_temp.job_status('monitor_jobs'), 'failed', '1 つでも壊れたら status は failed');
select ok(
  pg_temp.has_alert('monitor_check_failed:check_jobs_missing'),
  '壊れた検査そのものを通知する'
);
alter table public.monitored_jobs_hidden rename to monitored_jobs;

-- アドバイザリロックの番号を固定する。0009・0011・0013 が 1〜5 を使っている（W1-30）
select is(
  (select count(*)::int from pg_locks
    where locktype = 'advisory' and classid = 8423 and objid = 6 and pid = pg_backend_pid()),
  1, 'monitor_jobs は (8423, 6) を取る（他のジョブとぶつからない）'
);

select * from finish();
rollback;
