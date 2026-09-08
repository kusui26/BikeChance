-- pgTAP: バックアップ収集器の起動条件（W3 プラン §5.5、マイグレーション 0021）
--
-- ここで確かめられるのは**起動の判断まで**。`net.http_post` の送信はコミット後なので、
-- rollback するこのテストで実際に Edge Function が叩かれることはない（0007 と同じ）。
-- 到達の確認は本番での手動起動で行う。
--
-- ローカルでは pg_cron が動いていて job_runs に行が増え続けるので、件数を見る検査は
-- 自分で入れた行だけに閉じる。

begin;
select plan(50);

delete from public.job_runs;
delete from public.alert_state;
delete from vault.secrets where name in ('cron_secret', 'alert_webhook_url');
delete from public.app_config where key = 'functions_base_url';
update public.feed_state set last_success_at = now(), last_fetch_at = now();

create function pg_temp.alerts() returns text language sql as $$
  select coalesce(string_agg(alert_key, ',' order by alert_key), '(なし)') from public.alert_state;
$$;

create function pg_temp.has_alert(p_key text) returns boolean language sql as $$
  select exists (select 1 from public.alert_state where alert_key = p_key);
$$;

create function pg_temp.runs() returns integer language sql as $$
  select count(*)::int from public.job_runs where job_name = 'trigger_backup_collect';
$$;

create function pg_temp.last_status() returns text language sql as $$
  select status from public.job_runs where job_name = 'trigger_backup_collect' order by id desc limit 1;
$$;

create function pg_temp.last_detail(p_key text) returns text language sql as $$
  select detail->>p_key from public.job_runs
   where job_name = 'trigger_backup_collect' order by id desc limit 1;
$$;

-- 設定を入れる（値はこのテストが決める。rollback で戻る）
create function pg_temp.configure() returns void language sql as $$
  select vault.create_secret('dummy-not-a-real-secret', 'cron_secret', 'pgTAP 用');
  insert into public.app_config (key, value)
  values ('functions_base_url', 'http://127.0.0.1:54321/functions/v1')
  on conflict (key) do update set value = excluded.value;
$$;

create function pg_temp.stall(p_system text) returns void language sql as $$
  update public.feed_state set last_success_at = now() - interval '10 minutes'
   where system_id = p_system;
$$;

-- ────────────────────────────────────────────────────────────────
-- 構造
-- ────────────────────────────────────────────────────────────────
select has_function('public', 'trigger_backup_collect', 'trigger_backup_collect がある');
select has_function('public', 'check_cron_jobs', 'check_cron_jobs がある');
select is(
  (select bool_and(p.prosecdef) from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname in ('trigger_backup_collect', 'check_cron_jobs')),
  true, '2 つとも security definer'
);
select is(
  (select bool_and(p.proconfig @> array['search_path=""']) from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname in ('trigger_backup_collect', 'check_cron_jobs')),
  true, '2 つとも search_path = ''''（スキーマ付きで書く前提）'
);
select is(
  has_function_privilege('anon', 'public.trigger_backup_collect()', 'execute'),
  false, '匿名は trigger_backup_collect を実行できない'
);
select has_column('public', 'monitored_jobs', 'cron_job_name', 'monitored_jobs に cron_job_name がある');

select is((select schedule from cron.job where jobname = 'backup_collect'), '* * * * *',
  'backup_collect は毎分');
select is((select active from cron.job where jobname = 'backup_collect'), true, 'backup_collect は有効');

select is(
  (select is_active from public.monitored_jobs where job_name = 'trigger_backup_collect'),
  false, '成功の有無では見ない（発火したときだけ記録するため）'
);
select is(
  (select cron_job_name from public.monitored_jobs where job_name = 'trigger_backup_collect'),
  'backup_collect', 'pg_cron の登録だけを見る'
);
select is(
  (select count(*)::int from public.monitored_jobs where cron_job_name is not null),
  8, 'pg_cron のジョブ 8 本が登録の検査対象（0022 で daily_quality、0025 で rebuild_geo）'
);
select is(
  (select count(*)::int from cron.job c
    where not exists (select 1 from public.monitored_jobs m where m.cron_job_name = c.jobname)),
  0, '**pg_cron のジョブに監視の抜けが無い**（0022 の逆向きの検査が守る）'
);

-- ────────────────────────────────────────────────────────────────
-- 起動しない場合
-- ────────────────────────────────────────────────────────────────
select pg_temp.configure();

select is(public.trigger_backup_collect(), 0, '停滞していなければ発火しない');
select is(pg_temp.runs(), 0, '**発火しなければ job_runs に何も書かない**（毎分 1,441 行を作らない）');
select is(pg_temp.alerts(), '(なし)', '発火しなければ通知もしない');

-- 5 分の停滞では動かない（閾値は 6 分）
update public.feed_state set last_success_at = now() - interval '5 minutes';
select is(public.trigger_backup_collect(), 0, '5 分の停滞では発火しない（閾値は 6 分）');
select is(pg_temp.runs(), 0, '同上（記録も残らない）');

-- 止まっているシステムは対象外
update public.feed_state set last_success_at = now() - interval '10 minutes';
update public.systems set is_active = false;
select is(public.trigger_backup_collect(), 0, '停止中のシステムは起こさない');
update public.systems set is_active = true;

-- ────────────────────────────────────────────────────────────────
-- 起動する場合
-- ────────────────────────────────────────────────────────────────
update public.feed_state set last_success_at = now();
select pg_temp.stall('hellocycling');
select is(public.trigger_backup_collect(), 1, '6 分超の停滞で 1 システムだけ発火する');
select is(pg_temp.runs(), 1, '発火したときは job_runs に記録する');
select is(pg_temp.last_status(), 'ok', '記録は ok');
select is(pg_temp.last_detail('fired'), '1', 'detail に発火数が入る');
select is(
  (select detail->'systems'->>0 from public.job_runs
    where job_name = 'trigger_backup_collect' order by id desc limit 1),
  'hellocycling', 'detail にどのシステムかが入る'
);
select ok(pg_temp.has_alert('backup_collect_fired'), '**冗長系が動いたこと自体を通知する**');

-- 抑制（1 時間）。連続で鳴らさない
delete from public.job_runs;
select is(public.trigger_backup_collect(), 1, '2 回目も発火はする（収集はまだ止まっている）');
select is(
  (select count(*)::int from public.alert_state where alert_key = 'backup_collect_fired'), 1,
  '通知は抑制されて増えない'
);

-- 両系が止まれば両方
delete from public.job_runs; delete from public.alert_state;
update public.feed_state set last_success_at = now() - interval '10 minutes';
select is(public.trigger_backup_collect(), 2, '両系が停滞すれば 2 つ起こす');
select is(pg_temp.last_detail('fired'), '2', 'detail の発火数も 2');

-- last_success_at が一度も無い（初回起動直後）
delete from public.job_runs; delete from public.alert_state;
update public.feed_state set last_success_at = null;
select is(public.trigger_backup_collect(), 2, 'last_success_at が null なら停滞とみなす');

-- ────────────────────────────────────────────────────────────────
-- 設定漏れ
-- ────────────────────────────────────────────────────────────────
delete from public.job_runs; delete from public.alert_state;
delete from vault.secrets where name = 'cron_secret';
select is(public.trigger_backup_collect(), 0, 'cron_secret が無ければ起こさない');
select is(pg_temp.last_status(), 'failed', '設定漏れは failed として記録する');
select is(pg_temp.last_detail('reason'), 'cron_secret が未設定', 'detail に理由が入る');
select ok(
  pg_temp.has_alert('backup_collect_misconfigured'),
  '**設定漏れを通知する。** 冗長系が無い状態が静かに続くのが一番まずい'
);

delete from public.job_runs; delete from public.alert_state;
select pg_temp.configure();
delete from public.app_config where key = 'functions_base_url';
select is(public.trigger_backup_collect(), 0, 'functions_base_url が無ければ起こさない');
select is(pg_temp.last_detail('reason'), 'functions_base_url が未設定', 'どちらが欠けたか分かる');

-- ────────────────────────────────────────────────────────────────
-- 検査 5：pg_cron の登録
-- ────────────────────────────────────────────────────────────────
delete from public.alert_state;
select is(public.check_cron_jobs() -> 'checked', '8'::jsonb, '8 本を見る');
select is(public.check_cron_jobs() -> 'alerts', '0'::jsonb, '全部登録されていれば鳴らない');

-- 登録されていない場合（cron.job を触らずに、指す名前を変えて確かめる）
update public.monitored_jobs set cron_job_name = 'not_scheduled_at_all'
 where job_name = 'trigger_backup_collect';
-- **2 件鳴るのが正しい。** 存在しないジョブを指した（①）ことで、本物の
-- backup_collect が誰からも参照されなくなる（②）。両方向が同時に効いている
select is(public.check_cron_jobs() -> 'alerts', '2'::jsonb, '登録が無ければ鳴る（両方向で 2 件）');
select ok(pg_temp.has_alert('cron_job_missing:not_scheduled_at_all'), '鍵にジョブ名が入る');
select ok(
  pg_temp.has_alert('cron_job_unmonitored:backup_collect'),
  '参照されなくなった本物のジョブも「知らないジョブ」として拾う'
);
select alike(
  (select last_value->>'message' from public.alert_state
    where alert_key = 'cron_job_missing:not_scheduled_at_all'),
  '%登録されていません%', '「登録されていない」と「無効」を書き分ける'
);
update public.monitored_jobs set cron_job_name = 'backup_collect'
 where job_name = 'trigger_backup_collect';

-- **逆向き：登録されているのに monitored_jobs に無いジョブ**（0022）。
-- 表への入れ忘れがそのまま盲点になっていたので、両方向を見るようにした
delete from public.alert_state;
select is(public.check_cron_jobs() -> 'unmanaged', '0'::jsonb, '普段は知らないジョブが無い');
delete from public.monitored_jobs where job_name = 'daily_quality';
select is(public.check_cron_jobs() -> 'unmanaged', '1'::jsonb, '表から消すと「知らないジョブ」として数える');
select ok(pg_temp.has_alert('cron_job_unmonitored:daily_quality'), '入れ忘れを通知する');
insert into public.monitored_jobs (job_name, expected_every, missing_after, cron_job_name)
values ('daily_quality', interval '1 day', interval '30 hours', 'daily_quality');

-- 無効にされた場合
delete from public.alert_state;
select cron.alter_job((select jobid from cron.job where jobname = 'backup_collect'), active := false);
select is(public.check_cron_jobs() -> 'alerts', '1'::jsonb, '無効にされていても鳴る');
select alike(
  (select last_value->>'message' from public.alert_state
    where alert_key = 'cron_job_missing:backup_collect'),
  '%無効です%', '無効のときは文面が変わる'
);
select cron.alter_job((select jobid from cron.job where jobname = 'backup_collect'), active := true);

-- ────────────────────────────────────────────────────────────────
-- 親
-- ────────────────────────────────────────────────────────────────
delete from public.job_runs; delete from public.alert_state;
select lives_ok('select public.monitor_jobs()', 'monitor_jobs は例外を投げない');
select is(
  (select count(*)::int
     from jsonb_object_keys((select detail->'checks' from public.job_runs
                              where job_name = 'monitor_jobs' order by id desc limit 1))),
  5, '検査は 5 つになった'
);
select ok(
  (select detail->'checks' ? 'check_cron_jobs' from public.job_runs
    where job_name = 'monitor_jobs' order by id desc limit 1),
  'check_cron_jobs が detail に出る'
);

-- アドバイザリロックの番号（1〜6 は 0009・0011・0013・0020 が使っている。W1-30）
select is(
  (select count(*)::int from pg_locks
    where locktype = 'advisory' and classid = 8423 and objid = 7 and pid = pg_backend_pid()),
  1, 'trigger_backup_collect は (8423, 7) を取る'
);

select * from finish();
rollback;
