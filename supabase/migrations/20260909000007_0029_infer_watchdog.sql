-- 0029 推論のウォッチドッグ（W3 プラン §5.10 の手順 5、開発プラン §8.2・§5.5）
--
-- Vercel Cron は欠落・二重起動が起こり得て、**再試行が無い**（CLAUDE.md §2 の 2）。
-- 収集には `watchdog_collect`（毎分）、バックアップ収集器には `trigger_backup_collect`
-- （毎分）がある。**推論にだけ無かった。**
--
-- 開発プラン §5.5 の取り決めは「**12 分停滞で再起動、15 分で通知**」。半分ずつ置き場所を
-- 変える。
--   * **再起動** … `trigger_infer()`（pg_cron 5 分毎）。**行動**なので独立したジョブにする
--     （`trigger_backup_collect` と同じ形。発火したときだけ `job_runs` に記録する）
--   * **通知** … `check_inference()`。`monitor_jobs` の 6 つ目の検査として足す。あちらが
--     5 分毎に検査を束ねる係で、通知の抑制も `send_alert` に任せられる
--
-- **鮮度は `station_forecasts` で測る。`inference_log` では測らない。**
-- 推論が毎回失敗していても `inference_log` には行が増えるので、そちらを見ると「新しい」に
-- 見えてしまう。見たいのは「**配信できる予測が新しいか**」であって「ジョブが動いたか」では
-- ない。
--
-- **`generated_at` に索引を張らない。** `station_forecasts` は 5 分毎に全ポートを UPDATE し、
-- **HOT 更新率 100%** で回っている（§5.10 の実測。1,983,745 更新すべて HOT）。HOT が効くのは
-- **索引の付いた列が変わらない**ときだけで、`generated_at` は毎周期変わる。索引を足した瞬間に
-- 100% が崩れ、死亡行が溜まり始める。20,464 行（10 MB）の全走査を 5 分に 1 度払うほうが安い。
--
-- **叩くのは「投げれば進むとき」だけ。** 予測が古い理由が「収集が止まっている」ことなら、
-- `/ml/infer` を叩いても `begin_inference` が新しい `base_observed_at` を掴めず `skipped` を
-- 返すだけで、何も進まない。収集の停滞は `monitor_feeds` と `trigger_backup_collect` の
-- 持ち場である。そこで発火の条件に「**まだ推論していない観測がある**」を足す。
--
--   Vercel Cron の配信漏れ   新しい観測が在るのに推論が無い   → 叩く
--   収集の停滞               新しい観測が無い                 → 叩かない（あちらが鳴る）
--   推論そのものの故障       新しい観測が在る                 → 叩く（直らなければ 15 分で鳴る）
--
-- **通知のほうは理由を問わない。** 「予測が 15 分古い」は利用者から見た症状で、原因が収集でも
-- 推論でも配信は等しく劣化する。1 通に材料（最終予測時刻・最終観測時刻・直近の推論の状態）を
-- 詰めて、**どちらの持ち場かが読み取れる**ようにする。
--
-- **対象は `systems.is_active` の全システム**（`trigger_backup_collect` と同じ選び方）。
-- `apps/web/vercel-crons.test.ts` が「推論 Cron は全システムに 1 本ずつ」を固定しているので、
-- 台帳で活性なシステムは推論しているはず、が両側で保たれる。

-- ────────────────────────────────────────────────────────────────
-- 閾値
-- ────────────────────────────────────────────────────────────────
-- `collect_interval_s` と同じ作法で `app_config` に置く（W1-29）。推論は 5 分周期なので、
-- 12 分は「2 回続けて配信が漏れた」に当たる。1 回の取りこぼしでは叩かない。
insert into public.app_config (key, value) values
  ('infer_stall_s', '720'),
  ('infer_alert_s', '900')
on conflict (key) do nothing;

-- ────────────────────────────────────────────────────────────────
-- 検査 6：予測の鮮度
-- ────────────────────────────────────────────────────────────────
create or replace function public.check_inference()
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_row     record;
  v_limit   integer := public.config_int('infer_alert_s', 900);
  v_alerts  integer := 0;
  v_checked integer := 0;
  v_oldest  integer := null;
  v_age     integer;
begin
  for v_row in
    select sy.system_id,
           f.last_generated_at,
           f.last_base_observed_at,
           fs.last_observed_at as feed_observed_at,
           (select l.status from public.inference_log l
             where l.system_id = sy.system_id order by l.id desc limit 1) as last_status,
           (select l.error from public.inference_log l
             where l.system_id = sy.system_id order by l.id desc limit 1) as last_error
      from public.systems sy
      left join public.feed_state fs on fs.system_id = sy.system_id
      left join (
        select system_id,
               max(generated_at)      as last_generated_at,
               max(base_observed_at)  as last_base_observed_at
          from public.station_forecasts group by system_id
      ) f on f.system_id = sy.system_id
     where sy.is_active
  loop
    v_checked := v_checked + 1;
    v_age := case
               when v_row.last_generated_at is null then null
               else floor(extract(epoch from now() - v_row.last_generated_at))::integer
             end;
    if v_age is not null and (v_oldest is null or v_age > v_oldest) then
      v_oldest := v_age;
    end if;

    -- **1 度も動いていない**か、**閾値より古い**。前者は null なので比較では拾えない
    if v_row.last_generated_at is null or v_age > v_limit then
      if public.send_alert('inference_stale:' || v_row.system_id, jsonb_build_object(
           'message', v_row.system_id || ' の予測が '
                      || coalesce(round(v_age / 60.0, 1)::text || ' 分', '一度も出ていません')
                      || ' 古くなっています',
           -- **原因の当たりを付ける材料を一緒に送る。**
           --   feed_observed_at が古い     → 収集の持ち場（monitor_feeds）
           --   feed_observed_at だけ新しい → 推論の持ち場
           'last_generated_at', v_row.last_generated_at,
           'last_base_observed_at', v_row.last_base_observed_at,
           'feed_observed_at', v_row.feed_observed_at,
           'last_status', v_row.last_status,
           'last_error', v_row.last_error,
           'threshold_s', v_limit), interval '1 hour') then
        v_alerts := v_alerts + 1;
      end if;
    end if;
  end loop;
  return jsonb_build_object('checked', v_checked, 'alerts', v_alerts,
                            'oldest_age_s', v_oldest, 'threshold_s', v_limit);
end;
$$;

comment on function public.check_inference() is
  'station_forecasts の最終 generated_at が infer_alert_s より古いシステムを通知する。原因の判別材料（最終観測・直近の推論の状態）を payload に入れる。';

-- ────────────────────────────────────────────────────────────────
-- monitor_jobs に 6 つ目として組み込む
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
                           'check_parquet_gap', 'check_reference_data', 'check_inference'];
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
-- 再起動：まだ推論していない観測があり、かつ予測が停滞しているとき
-- ────────────────────────────────────────────────────────────────
create or replace function public.trigger_infer()
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_run     bigint;
  v_secret  text;
  v_base    text;
  v_stall   integer := public.config_int('infer_stall_s', 720);
  v_fired   integer := 0;
  v_system  text;
  v_systems text[] := array[]::text[];
begin
  -- ジョブごとに別の object_id を使う（W1-30）。1〜8 は 0009・0011・0013・0020・0021・0025
  if not pg_try_advisory_xact_lock(8423, 9) then
    return -1;
  end if;

  -- **叩く相手を先に数える。** 何も無ければ記録を残さずに戻る
  -- （5 分毎の「何も起きていない」で job_runs を埋めない。1 日 288 行になる）
  select coalesce(array_agg(sy.system_id order by sy.system_id), array[]::text[])
    into v_systems
    from public.systems sy
    join public.feed_state fs on fs.system_id = sy.system_id
    left join (
      select system_id,
             max(generated_at)     as last_generated_at,
             max(base_observed_at) as last_base_observed_at
        from public.station_forecasts group by system_id
    ) f on f.system_id = sy.system_id
   where sy.is_active
     -- 予測が停滞している（一度も出ていない場合を含む）
     and (f.last_generated_at is null
          or f.last_generated_at < now() - make_interval(secs => v_stall))
     -- **かつ、投げれば進む**（まだ推論していない観測がある）
     and fs.last_observed_at is not null
     and (f.last_base_observed_at is null or fs.last_observed_at > f.last_base_observed_at);

  if cardinality(v_systems) = 0 then
    return 0;
  end if;

  v_run := public.job_started('trigger_infer');
  begin
    select decrypted_secret into v_secret from vault.decrypted_secrets where name = 'cron_secret';
    select value into v_base from public.app_config where key = 'project_base_url';

    if v_secret is null or v_base is null then
      perform public.job_finished(v_run, 'failed',
        jsonb_build_object('reason', 'cron_secret か project_base_url が未設定',
                           'stalled', to_jsonb(v_systems)));
      return 0;
    end if;

    foreach v_system in array v_systems loop
      -- **応答を待たない。** 結果は inference_log と station_forecasts で見る
      perform net.http_get(
        url := v_base || '/ml/infer/' || v_system,
        headers := jsonb_build_object('Authorization', 'Bearer ' || v_secret),
        timeout_milliseconds := 10000
      );
      v_fired := v_fired + 1;
    end loop;

    -- **叩いたこと自体を知らせる。** 12 分の停滞は「2 回続けて配信が漏れた」に当たり、
    -- 滅多に起きない。毎回鳴らすとうるさいので抑制は長め（6 時間）にし、**慢性的な
    -- 配信漏れがウォッチドッグで埋め合わされたまま気づかれない**のを防ぐ
    perform public.send_alert('infer_watchdog_fired', jsonb_build_object(
      'message', '推論が ' || v_stall || ' 秒停滞したため、/ml/infer を起こしました',
      'systems', to_jsonb(v_systems)), interval '6 hours');

    perform public.job_finished(v_run, 'ok',
      jsonb_build_object('fired', v_fired, 'systems', to_jsonb(v_systems),
                         'stall_threshold_s', v_stall));
    return v_fired;
  exception when others then
    perform public.job_finished(v_run, 'failed',
      jsonb_build_object('error', sqlerrm, 'code', sqlstate));
    return 0;
  end;
end;
$$;

comment on function public.trigger_infer() is
  '予測が infer_stall_s より古く、かつ未推論の観測があるシステムについて /ml/infer を起こす。発火したときだけ job_runs に記録する。';

-- ────────────────────────────────────────────────────────────────
-- 監視対象に入れる
-- ────────────────────────────────────────────────────────────────
-- 発火したときだけ記録するので、成功の有無では見ない（`is_active = false`）。
-- **pg_cron に登録されていることは見る**（0022 の逆向きの検査に引っかからないよう、
-- ここに入れるのは必須。入れ忘れると 5 分後に「知らないジョブが増えた」と鳴る）
insert into public.monitored_jobs
  (job_name, expected_every, missing_after, is_active, cron_job_name, note)
values
  ('trigger_infer', interval '5 minutes', interval '30 minutes', false, 'infer_watchdog',
   'pg_cron 5 分毎。発火したときだけ job_runs に記録するので、成功の有無では見ない')
on conflict (job_name) do update
  set cron_job_name = excluded.cron_job_name,
      is_active     = excluded.is_active,
      note          = excluded.note;

revoke all on function public.check_inference() from public, anon, authenticated;
revoke all on function public.trigger_infer() from public, anon, authenticated;

-- ────────────────────────────────────────────────────────────────
-- pg_cron
-- ────────────────────────────────────────────────────────────────
-- 5 分毎。**ほとんどの回は 1 クエリで戻る**（停滞しているシステムが無ければ記録もしない）。
-- 名前を固定しておけば、何度流しても増えない（0010 の冒頭）。
select cron.schedule('infer_watchdog', '*/5 * * * *', $$select public.trigger_infer()$$);
