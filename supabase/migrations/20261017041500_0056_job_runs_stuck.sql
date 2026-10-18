-- ────────────────────────────────────────────────────────────────
-- 0056 — running のまま残った job_runs を、見張りが知らせ、日次の保守が閉じる
-- ────────────────────────────────────────────────────────────────
-- W7 プランの W7-02、W6 プランの所見 220 の ③・223。
--
-- 2026-10-09〜10-16 に、天気の取り込み（`archive_weather`）が Vercel の時間の上限で
-- 6 回止められ、`job_runs` の行が `running` のまま残った。**警報は 1 度も出なかった**
-- ——`job_failed` は `failed` の行しか見ず、`check_weather_stale` は最新の発行が 4 時間
-- 進まないときだけ鳴る。点検の SQL を足して初めて気づいた（10/16）。`evaluate_daily` にも
-- 9/17〜9/18 の 5 行が残っている（Vercel の関数がメモリで死んだ回）。
--
-- 推論の記録（`inference_log`）には同じ形の手当てがすでにある（0031 が知らせ、0032 が
-- 閉じる）。**`job_runs` にも同じ形を足す。**
--
--   * 見張り：`check_jobs_stuck`。`running` のまま閾値を過ぎた行を、ジョブ名ごとに
--     6 時間に 1 回知らせる。閾値は `app_config` の `job_stuck_s`（既定 3600 秒）
--   * 閉じる：`run_maintenance`（日次 03:00 JST）が `failed` にし、`detail` に
--     `error = never_finished` を足す。**消さない**——「失敗した」と「閉じられなかった」を
--     区別する（0032 と同じ）
--   * **`detail` に `hour_epoch_s` を足さない。** `v_weather_files` は `hour_epoch_s` のある
--     `archive_weather` の行だけを「発行」として読むので、閉じた行は天気の「使える時刻」を
--     作らない
--   * **閾値 1 時間**は、30 日でいちばん長いジョブ（`evaluate_daily` の 6.6 分）の 9 倍
--   * **いま残っている行は、この移行で 1 度閉じる。** 閉じないと、新しい見張りが古い行で
--     次の保守まで鳴り続ける
-- ────────────────────────────────────────────────────────────────

insert into public.app_config (key, value) values
  ('job_stuck_s', '3600')
on conflict (key) do nothing;

-- ────────────────────────────────────────────────────────────────
-- 検査 9：running のまま閉じられないジョブの記録
-- ────────────────────────────────────────────────────────────────
create or replace function public.check_jobs_stuck()
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_row    record;
  v_limit  integer := public.config_int('job_stuck_s', 3600);
  v_alerts integer := 0;
  v_jobs   integer := 0;
begin
  for v_row in
    select job_name, count(*) as n, min(started_at) as oldest, max(started_at) as newest
      from public.job_runs
     where status = 'running' and started_at < now() - make_interval(secs => v_limit)
     group by job_name
  loop
    v_jobs := v_jobs + 1;
    -- **ジョブ名ごとに鍵を分ける。** 別のジョブが続けて止まっても、抑制で隠れない
    if public.send_alert('job_stuck:' || v_row.job_name, jsonb_build_object(
         'message', v_row.job_name || ' の記録が ' || v_row.n
                    || ' 件、running のまま閉じられていません（時間の上限で止められたか、記録の前に落ちた）',
         'count', v_row.n,
         'oldest', v_row.oldest,
         'newest', v_row.newest,
         'threshold_s', v_limit), interval '6 hours') then
      v_alerts := v_alerts + 1;
    end if;
  end loop;
  return jsonb_build_object('jobs', v_jobs, 'alerts', v_alerts, 'threshold_s', v_limit);
end;
$$;

comment on function public.check_jobs_stuck() is
  'job_runs で running のまま job_stuck_s（既定 1 時間）を過ぎた行を、ジョブ名ごとに 6 時間に 1 回知らせる（0056、W7-02）。閉じるのは run_maintenance。';

revoke all on function public.check_jobs_stuck() from public, anon, authenticated;

-- ────────────────────────────────────────────────────────────────
-- run_maintenance — 閉じられなかったジョブの記録も閉じる
-- ────────────────────────────────────────────────────────────────
-- 本体は 0037 のまま。`job_runs` を閉じる段と、結果の `job_runs_closed` を足すだけ。
create or replace function public.run_maintenance(p_keep_days integer default 60)
returns jsonb
language plpgsql
security definer
set search_path = ''
set statement_timeout = '10min'
as $$
declare
  v_run       bigint;
  v_created   integer;
  v_dropped   integer;
  v_logs      integer;
  v_closed    integer;
  v_jobs      integer;
  v_inference integer;
  v_weather   integer;
  v_details   integer := 0;
  v_result    jsonb;
begin
  if not pg_try_advisory_xact_lock(8423, 3) then
    return jsonb_build_object('status', 'locked');
  end if;
  v_run := public.job_started('maintain_partitions');

  begin
    v_created := public.ensure_snapshot_partitions(2);
    v_dropped := public.drop_expired_snapshot_partitions(p_keep_days);

    delete from public.feed_fetch_log where fetched_at < now() - interval '30 days';
    get diagnostics v_logs = row_count;

    -- **閉じられなかった行を閉じる**（0032）。消すのではなく `failed` にする
    update public.inference_log
       set status = 'failed',
           finished_at = clock_timestamp(),
           error = 'never_finished'
     where status = 'running' and generated_at < now() - interval '1 hour';
    get diagnostics v_closed = row_count;

    -- **閉じられなかったジョブの記録も閉じる**（0056）。`hour_epoch_s` を足さないので、
    -- 天気の「使える時刻」（v_weather_files）には現れない。この回の自分の行は新しいので閉じない
    update public.job_runs
       set status = 'failed',
           finished_at = clock_timestamp(),
           detail = coalesce(detail, '{}'::jsonb) || jsonb_build_object('error', 'never_finished')
     where status = 'running'
       and started_at < now() - make_interval(secs => public.config_int('job_stuck_s', 3600));
    get diagnostics v_jobs = row_count;

    -- **`ok` だけを消す。** 失敗・二重抑止・閉じられなかった行は残す（0031 の冒頭）
    delete from public.inference_log
     where status = 'ok' and generated_at < now() - interval '90 days';
    get diagnostics v_inference = row_count;

    -- 天気は派生物。生 gzip JSON から作り直せる（0037）
    delete from public.weather_hourly where issued_hour < now() - interval '30 days';
    get diagnostics v_weather = row_count;

    -- cron.job_run_details は自動削除されない。毎分ジョブで月 4.3 万行たまる（§4.3 の 3）
    begin
      delete from cron.job_run_details where end_time < now() - interval '7 days';
      get diagnostics v_details = row_count;
    exception when insufficient_privilege then
      v_details := -1;  -- 権限が無い環境では諦める（ローカルなど）
    end;

    v_result := jsonb_build_object(
      'partitions_created', v_created, 'partitions_dropped', v_dropped,
      'fetch_logs_deleted', v_logs, 'inference_runs_closed', v_closed,
      'job_runs_closed', v_jobs,
      'inference_logs_deleted', v_inference, 'weather_rows_deleted', v_weather,
      'cron_details_deleted', v_details);
    perform public.job_finished(v_run, 'ok', v_result);
    return v_result;
  exception when others then
    perform public.job_finished(v_run, 'failed',
      jsonb_build_object('error', sqlerrm, 'code', sqlstate));
    return jsonb_build_object('status', 'failed', 'error', sqlerrm);
  end;
end;
$$;

comment on function public.run_maintenance(integer) is
  'パーティションの作成・削除、取得ログ 30 日超・推論ログ（ok のみ）90 日超・天気 30 日超・cron.job_run_details 7 日超の削除、閉じられなかった推論（1 時間超の running）とジョブの記録（job_stuck_s 超の running。0056）を failed にする。';

revoke all on function public.run_maintenance(integer) from public, anon, authenticated;

-- ────────────────────────────────────────────────────────────────
-- monitor_jobs に 9 つ目として組み込む
-- ────────────────────────────────────────────────────────────────
-- 本体は 0052 のまま。**並びの末尾に足すだけ**（検査どうしは独立していて、
-- 1 つが落ちても他は走る）。
create or replace function public.monitor_jobs()
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_run    bigint;
  v_names  text[] := array['check_jobs_missing', 'check_jobs_failed', 'check_cron_jobs',
                           'check_parquet_gap', 'check_reference_data', 'check_inference',
                           'check_model_freshness', 'check_weather_stale', 'check_jobs_stuck'];
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

comment on function public.monitor_jobs() is
  '9 つの検査（欠落・失敗・pg_cron・Parquet の穴・参照データ・推論・モデルの鮮度・天気の凍結・閉じられないジョブの記録）を順に呼ぶ。';

revoke all on function public.monitor_jobs() from public, anon, authenticated;

-- ────────────────────────────────────────────────────────────────
-- いま残っている行を 1 度閉じる
-- ────────────────────────────────────────────────────────────────
-- 2026-10-17 の時点で `archive_weather` 6 行・`evaluate_daily` 5 行。閉じないと、
-- 上の見張りが古い行で次の保守（03:00）まで鳴り続ける
update public.job_runs
   set status = 'failed',
       finished_at = clock_timestamp(),
       detail = coalesce(detail, '{}'::jsonb) || jsonb_build_object('error', 'never_finished')
 where status = 'running'
   and started_at < now() - make_interval(secs => public.config_int('job_stuck_s', 3600));

notify pgrst, 'reload schema';
