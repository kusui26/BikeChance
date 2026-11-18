-- ────────────────────────────────────────────────────────────────
-- 0061 — 収集の見張りの閾値を「収集の周期 ＋ 10 秒」にする（W11 プランの W11-04、PR W）
-- ────────────────────────────────────────────────────────────────
-- **Vercel Cron が 1 分欠けても、見張りは動いていなかった**（W10 プランの所見 241）。0009 の
-- 閾値は「収集の周期 × 2 ＋ 30 秒」で、毎分の収集では 150 秒。見張り（pg_cron 毎分の 0 秒）の
-- 時点の取得の古さは、直近 7 日の実測で：
--
--   * ふだん                       18〜29 秒（Vercel Cron は分の 31〜42 秒に収集を呼ぶ）
--   * Vercel Cron が 1 分欠けた後   78〜89 秒 → 150 秒に届かず、叩き直さない
--   * 2 分続けて欠けた後           138〜149 秒 → まだ届かない。取得の間隔が最大 180 秒に開いた
--
-- **70 秒（周期 ＋ 10 秒）にする**と、1 分の欠落の後の最初の 0 秒で叩き直し、ふだんの取得
-- （最大 29 秒）では動かない（41 秒の余裕）。
--
-- **ODPT への取得を常に二重にはしない**（CLAUDE.md §2 の 8）：
--   * 見張りが叩き直すのは、Vercel Cron が欠けたとき（7 日で 24 回）だけ。欠落 1 回につき取得が
--     1 つ増えるだけで、取得は `last_updated` で冪等なので二重でも無害
--   * `last_fetch_at` は取得を**始めるとき**に `begin_fetch` が書く（0007）。ODPT が止まって
--     取得が失敗し続けても、Vercel Cron が届いていれば古くならず、見張りは動かない
--   * 叩き直した直後に遅れた Vercel Cron が届いても、`begin_fetch` の 30 秒の間隔で取らない
--
-- 5 分ごとの非常時の形（E1、`collect_interval_s` 300）では 310 秒になり、0009 の 630 秒より
-- 早く気づく。**中身は 0009 と同じで、閾値の式と注記だけを変える**（`create or replace` は
-- 持ち主と権限を変えない。`security definer` と `search_path` は定義に含まれるので書き直す）。
--
-- あわせて、`monitored_jobs.note` の時刻を今に合わせる（警報の文には使わない説明の欄。
-- 警報は `failure_note` を使う。0041）：Parquet 化は毎時 9 分・参照は 05:03 JST（W11 の PR P、
-- D-48）、天気の取り込みは毎時 28 分（W9-14 から。注記だけ 25 分のままだった）。

create or replace function public.watchdog_collect()
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_run       bigint;
  v_secret    text;
  v_base      text;
  v_threshold integer;
  v_fired     integer := 0;
  v_system    text;
begin
  if not pg_try_advisory_xact_lock(8423, 1) then
    return -1;
  end if;
  v_run := public.job_started('watchdog_collect');

  begin
    -- 収集の周期 ＋ 10 秒。毎分（E2）で 70 秒、5 分ごと（E1）で 310 秒（0061。0009 は周期 × 2 ＋ 30）
    v_threshold := public.config_int('collect_interval_s', 60) + 10;

    select decrypted_secret into v_secret from vault.decrypted_secrets where name = 'cron_secret';
    select value into v_base from public.app_config where key = 'project_base_url';

    if v_secret is null or v_base is null then
      perform public.job_finished(v_run, 'failed',
        jsonb_build_object('reason', 'cron_secret か project_base_url が未設定'));
      return 0;
    end if;

    for v_system in
      select fs.system_id from public.feed_state fs
       where fs.last_fetch_at is null
          or fs.last_fetch_at < now() - make_interval(secs => v_threshold)
    loop
      perform net.http_get(
        url := v_base || '/api/jobs/collect/' || v_system || '?source=watchdog',
        headers := jsonb_build_object('Authorization', 'Bearer ' || v_secret),
        timeout_milliseconds := 10000
      );
      v_fired := v_fired + 1;
    end loop;

    perform public.job_finished(v_run, 'ok',
      jsonb_build_object('fired', v_fired, 'threshold_s', v_threshold));
    return v_fired;
  exception when others then
    perform public.job_finished(v_run, 'failed',
      jsonb_build_object('error', sqlerrm, 'code', sqlstate));
    return 0;
  end;
end;
$$;

comment on function public.watchdog_collect() is
  'feed_state.last_fetch_at が閾値（collect_interval_s ＋ 10 秒。毎分の収集で 70 秒）より古いシステムの収集を叩き直す（0061。Vercel Cron が 1 分欠けた後の最初の 0 秒で動く）。';

update public.monitored_jobs
   set note = 'pg_cron 毎分。Vercel Cron の配信漏れを補う（取得が収集の周期 ＋ 10 秒より古ければ叩き直す。0061）'
 where job_name = 'watchdog_collect';

update public.monitored_jobs
   set note = 'Vercel Cron 毎時 :09（COMPACT_CRON。2026-11-17 まで :07）。2 回落ちたら鳴る'
 where job_name = 'compact_parquet';

update public.monitored_jobs
   set note = 'Vercel Cron 05:03 JST（REFERENCE_CRON）。前日ぶんの参照スナップショット（reference/date=YYYY-MM-DD/）。止まると翌日の学習・推論が前日の版を読めなくなる'
 where job_name = 'build_reference';

update public.monitored_jobs
   set note = 'Vercel Cron 毎時 :28（LOAD_WEATHER_CRON）。weather-raw の gzip JSON を weather_hourly に取り込む。止まると天気の特徴量が古い発行のままになる（v_weather_pending に溜まる）'
 where job_name = 'load_weather';
