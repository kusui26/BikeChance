-- 0057 `job_runs` の保持（W7-09・W8 プランの W8-07。W8 の PR D）
--
-- **消すのは「何も起きていない」見張りの行だけ**：`watchdog_collect`（pg_cron 毎分）と
-- `monitor_feeds`（5 分毎）の `ok` で、**30 日より古いもの**。開発プランのロードマップ W7 の
-- 行（W3 から移した）と W2 プラン §14.5 のとおり。
--
--   * **2026-10-26 の本番**：`job_runs` は 48 MB・104,772 行。30 日より古い 40,987 行の
--     うち、この 2 つは 33,957 行（`watchdog_collect` 28,296・`monitor_feeds` 5,661。
--     どちらも 30 日より古い行はすべて `ok`）。1 日に約 1,700 行（毎分 1,440 ＋ 5 分毎 288）が
--     この 2 つで、消した後の伸びは 1 日 約 400 行になる
--   * **`archive_weather` は消さない**：天気の「使える時刻」（`v_weather_files.available_at`
--     ＝ `job_runs.finished_at`）の記録そのもので、消すと生アーカイブが在っても「どの発行が
--     在るか」を DB が忘れる（開発プラン §5.5 の「`job_runs` は刈らない」の理由。
--     `0016_weather_hourly.sql` の pgTAP が守る）
--   * **失敗・`skipped`・閉じた行は消さない**（0031 の `inference_log` と同じ考え方：
--     「いつ何が起きたか」の記録として長く価値がある）
--   * **`monitor_jobs` は残す**（1 日 288 行）。鮮度の見張り（`check_model_freshness`）などの
--     履歴を後から読むため（W8-07）
--   * **見張りが読むのは直近の行だけ**（`monitored_jobs` の `missing_after` は 15〜30 分）なので、
--     30 日より古い行を消しても検知は変わらない
--
-- **初回は約 3.4 万行を消す**（日次の保守 03:00 JST の最初の回）。`db push` の前に数を出して
-- 利用者の確認を取る（CLAUDE.md §6）。

-- ────────────────────────────────────────────────────────────────
-- run_maintenance — 見張りの ok の行を 30 日で消す
-- ────────────────────────────────────────────────────────────────
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
  v_job_logs  integer;
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

    -- **見張りの `ok` の行だけを 30 日で消す**（0057）。`archive_weather`・失敗・`skipped`・
    -- 閉じた行・ほかのジョブは消さない（冒頭）
    delete from public.job_runs
     where job_name in ('watchdog_collect', 'monitor_feeds')
       and status = 'ok'
       and started_at < now() - interval '30 days';
    get diagnostics v_job_logs = row_count;

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
      'job_runs_closed', v_jobs, 'job_runs_deleted', v_job_logs,
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
  'パーティションの作成・削除、取得ログ 30 日超・推論ログ（ok のみ）90 日超・天気 30 日超・cron.job_run_details 7 日超・見張り（watchdog_collect と monitor_feeds）の ok の job_runs 30 日超（0057）の削除、閉じられなかった推論（1 時間超の running）とジョブの記録（job_stuck_s 超の running。0056）を failed にする。';

revoke all on function public.run_maintenance(integer) from public, anon, authenticated;

notify pgrst, 'reload schema';
