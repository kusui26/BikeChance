-- 0021 バックアップ収集器の起動条件（W3 プラン §5.5、開発プラン R13・R18）
--
-- Vercel が丸ごと落ちているあいだ、収集も推論も API も止まる。**そのうち取り返しが
-- つかないのは GBFS だけ**なので（`/v1` は復旧すれば戻る）、生 JSON の保存だけを
-- Supabase 側から続ける。実体は Edge Function `collect-gbfs-backup`。
--
-- ここが持つのは「いつ起こすか」だけ。
--   * `feed_state.last_success_at` が **6 分**を超えて停滞したときだけ叩く。毎分
--     ポーリング＋ウォッチドッグ（1 分毎）でも復帰しない状態、つまり Vercel 側が
--     丸ごと死んでいる状態にだけ当たる（W3-05）
--   * **常時二重化はしない**（D-16）。ODPT を過負荷にしない（CLAUDE.md §2 の 8）
--   * **発火したときだけ `job_runs` に記録する。** 毎分「何も起きていない」を書くと
--     1 日 1,441 行になる。代わりに pg_cron への登録そのものを監視する（下記）
--
-- あわせて `monitor_jobs` に 5 つ目の検査を足す。**pg_cron のジョブが登録されていて
-- active か**を見る検査で、行を 1 つも増やさずに「誰かが unschedule した」を捕まえる。
-- 発火するまで記録を書かないジョブは、これでしか見張れない。

-- ────────────────────────────────────────────────────────────────
-- 監視対象に「pg_cron のジョブ名」を持たせる
-- ────────────────────────────────────────────────────────────────
alter table public.monitored_jobs add column cron_job_name text;

comment on column public.monitored_jobs.is_active is
  '**成功の有無を見るか。** pg_cron の登録の検査は cron_job_name が非 NULL かで決まり、この列とは独立している。';
comment on column public.monitored_jobs.cron_job_name is
  'pg_cron の cron.job.jobname。非 NULL なら「登録されていて active か」を検査する。Vercel Cron のジョブは NULL。';

update public.monitored_jobs set cron_job_name = job_name
 where job_name in ('watchdog_collect', 'monitor_feeds', 'monitor_jobs',
                    'maintain_partitions', 'refresh_station_activity');
update public.monitored_jobs set cron_job_name = 'daily_quality' where job_name = 'daily_quality';

-- 発火したときだけ記録するので、成功の有無では見ない（is_active = false）。
-- pg_cron に登録されていることだけを見る
insert into public.monitored_jobs
  (job_name, expected_every, missing_after, is_active, cron_job_name, note)
values
  ('trigger_backup_collect', interval '1 minute', interval '15 minutes', false, 'backup_collect',
   'pg_cron 毎分。発火したときだけ job_runs に記録するので、成功の有無では見ない');

-- ────────────────────────────────────────────────────────────────
-- 検査 5：pg_cron のジョブが登録されていて active か
-- ────────────────────────────────────────────────────────────────
-- **行を 1 つも増やさずに「静かに外された」を捕まえる。** `job_runs` を見る検査は
-- 「動いた記録」が要るが、こちらは登録そのものを見るので、滅多に発火しないジョブや
-- 日次のジョブでも即座に気づける。
create or replace function public.check_cron_jobs()
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_row     record;
  v_alerts  integer := 0;
  v_checked integer := 0;
begin
  for v_row in
    select m.job_name, m.cron_job_name,
           (select c.active from cron.job c where c.jobname = m.cron_job_name) as active
      from public.monitored_jobs m
     where m.cron_job_name is not null
  loop
    v_checked := v_checked + 1;
    if v_row.active is distinct from true then
      if public.send_alert('cron_job_missing:' || v_row.cron_job_name, jsonb_build_object(
           'message', 'pg_cron のジョブ ' || v_row.cron_job_name || ' が '
                      || case when v_row.active is null then '登録されていません' else '無効です' end,
           'job_name', v_row.job_name), interval '3 hours') then
        v_alerts := v_alerts + 1;
      end if;
    end if;
  end loop;
  return jsonb_build_object('checked', v_checked, 'alerts', v_alerts);
end;
$$;

comment on function public.check_cron_jobs() is
  'monitored_jobs.cron_job_name が指す pg_cron のジョブが登録されていて active かを見る。行を増やさずに「外された」を捕まえる。';

-- ────────────────────────────────────────────────────────────────
-- monitor_jobs に 5 つ目を足す（親の役目は変わらない）
-- ────────────────────────────────────────────────────────────────
create or replace function public.monitor_jobs()
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_run    bigint;
  v_names  text[] := array['check_jobs_missing', 'check_jobs_failed', 'check_cron_jobs',
                           'check_parquet_gap', 'check_reference_data'];
  v_name   text;
  v_one    jsonb;
  v_checks jsonb := '{}'::jsonb;
  v_alerts integer := 0;
  v_errors integer := 0;
begin
  -- ジョブごとに別の object_id を使う（W1-30）。1〜5 は 0009・0011・0013 が使っている
  if not pg_try_advisory_xact_lock(8423, 6) then
    return jsonb_build_object('status', 'locked');
  end if;
  v_run := public.job_started('monitor_jobs');

  foreach v_name in array v_names loop
    begin
      execute format('select public.%I()', v_name) into v_one;
      v_alerts := v_alerts + coalesce((v_one->>'alerts')::integer, 0);
    exception when others then
      v_errors := v_errors + 1;
      v_one := jsonb_build_object('error', sqlerrm, 'code', sqlstate);
      -- 通知の失敗でこの関数を落とさない。記録のほうが確実に残る
      begin
        perform public.send_alert('monitor_check_failed:' || v_name, jsonb_build_object(
          'message', '監視の検査 ' || v_name || ' が失敗しました',
          'error', sqlerrm, 'code', sqlstate), interval '1 hour');
      exception when others then
        null;
      end;
    end;
    v_checks := v_checks || jsonb_build_object(v_name, v_one);
  end loop;

  v_one := jsonb_build_object('alerts', v_alerts, 'errors', v_errors, 'checks', v_checks);
  perform public.job_finished(v_run, case when v_errors = 0 then 'ok' else 'failed' end, v_one);
  return v_one;
end;
$$;

-- ────────────────────────────────────────────────────────────────
-- Edge Function の置き場所
-- ────────────────────────────────────────────────────────────────
-- **値をここに書かない。** `project_base_url`（0004）は利用者が叩く Vercel の URL で
-- 公開されているが、Supabase のプロジェクト URL はどこにも公開していない。
-- 秘密ではないが、わざわざリポジトリに置く理由も無い。
-- 設定は `scripts/setup-app-config.sh`（`.env` の SUPABASE_URL から導く）で入れる。
comment on table public.app_config is
  '秘密でない設定値。秘密は Vault に置く。project_base_url（ウォッチドッグの宛先）と functions_base_url（Edge Function の宛先）は scripts/setup-app-config.sh で入れる。';

-- ────────────────────────────────────────────────────────────────
-- 起動条件
-- ────────────────────────────────────────────────────────────────
create or replace function public.trigger_backup_collect()
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_run       bigint;
  v_secret    text;
  v_base      text;
  v_stall     interval := interval '6 minutes';
  v_fired     integer := 0;
  v_system    text;
  v_systems   text[] := array[]::text[];
  v_reason    text;
begin
  if not pg_try_advisory_xact_lock(8423, 7) then
    return -1;
  end if;

  -- **停滞しているシステムを先に数える。** 何も無ければ記録を残さずに戻る
  -- （毎分の「何も起きていない」で job_runs を埋めない）
  select coalesce(array_agg(fs.system_id order by fs.system_id), array[]::text[])
    into v_systems
    from public.feed_state fs
    join public.systems sy using (system_id)
   where sy.is_active
     and (fs.last_success_at is null or fs.last_success_at < now() - v_stall);

  if cardinality(v_systems) = 0 then
    return 0;
  end if;

  v_run := public.job_started('trigger_backup_collect');
  begin
    select decrypted_secret into v_secret from vault.decrypted_secrets where name = 'cron_secret';
    select value into v_base from public.app_config where key = 'functions_base_url';

    if v_secret is null or v_base is null then
      v_reason := case when v_secret is null then 'cron_secret' else 'functions_base_url' end;
      perform public.job_finished(v_run, 'failed',
        jsonb_build_object('reason', v_reason || ' が未設定', 'stalled', to_jsonb(v_systems)));
      -- **設定漏れは通知する。** 気づかないまま冗長系が無い状態が続くのが一番まずい
      perform public.send_alert('backup_collect_misconfigured', jsonb_build_object(
        'message', 'バックアップ収集器を起こせません（' || v_reason || ' が未設定）',
        'stalled', to_jsonb(v_systems)), interval '6 hours');
      return 0;
    end if;

    foreach v_system in array v_systems loop
      -- **応答を待たない。** 結果は job_runs（backup_collect:<system>）で見る。
      -- 既定のタイムアウトは 2 秒で Edge Function の起動には足りない（W1 §4.3 の 7）
      perform net.http_post(
        url := v_base || '/collect-gbfs-backup?system=' || v_system,
        headers := jsonb_build_object(
          'Authorization', 'Bearer ' || v_secret,
          'Content-Type', 'application/json'),
        body := '{}'::jsonb,
        timeout_milliseconds := 30000
      );
      v_fired := v_fired + 1;
    end loop;

    -- **収集が止まっていること自体を通知する。** monitor_feeds の feed_stalled とは
    -- 別の鍵にして、「冗長系が動いた」という事実を独立に残す
    perform public.send_alert('backup_collect_fired', jsonb_build_object(
      'message', 'Vercel の収集が ' || v_stall || ' 停滞したため、バックアップ収集器を起こしました',
      'systems', to_jsonb(v_systems)), interval '1 hour');

    perform public.job_finished(v_run, 'ok',
      jsonb_build_object('fired', v_fired, 'systems', to_jsonb(v_systems),
                         'stall_threshold', v_stall::text));
    return v_fired;
  exception when others then
    perform public.job_finished(v_run, 'failed',
      jsonb_build_object('error', sqlerrm, 'code', sqlstate));
    return 0;
  end;
end;
$$;

comment on function public.trigger_backup_collect() is
  'feed_state.last_success_at が 6 分超で停滞したシステムについて、Edge Function collect-gbfs-backup を起こす。発火したときだけ job_runs に記録する。';

revoke all on function public.check_cron_jobs() from public, anon, authenticated;
revoke all on function public.trigger_backup_collect() from public, anon, authenticated;

-- ────────────────────────────────────────────────────────────────
-- pg_cron
-- ────────────────────────────────────────────────────────────────
-- 毎分。**ほとんどの回は 1 クエリで戻る**（停滞しているシステムが無ければ記録もしない）
select cron.schedule('backup_collect', '* * * * *', $$select public.trigger_backup_collect()$$);

notify pgrst, 'reload schema';
