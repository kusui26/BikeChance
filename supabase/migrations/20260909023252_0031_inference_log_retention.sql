-- 0031 `inference_log` の保持と、閉じられなかった行の検知（W3 プラン §14.2 の 4）
--
-- **保持規則がどこにも無かった**（§13.6）。1 日 576 行（両系 × 288）で伸び続ける。
--
-- **容量が理由ではない。** 1 行 600 B ほどなので年 126 MB で、8 GB の目標に対して
-- 1.5%/年にすぎない。上限が読めない表を放っておかない、というだけである。
--
--   * **90 日で消す。** 直近 1 四半期の運転記録が残る。5.2 万行・約 31 MB で頭打ちになる
--   * **`ok` 以外は消さない。** 失敗・二重抑止・閉じられなかった行は数が極小で、
--     「いつ何が起きたか」の記録として長く価値がある。`job_runs` の W7 の方針
--     （消すのは「何も起きていない」行だけ）と同じ考え方
--
-- あわせて **`running` のまま残った行**を見張る（§12 の 111）。
--
-- `begin_inference` で掴んだあと `finish_inference` が失敗すると、行は `running` の
-- まま残る。**これ自体は正しい**：`finish_inference` が失敗しているのだから、同じ表に
-- 「失敗した」とも書けない。**問題は誰も見ていないことだった。** `check_inference` は
-- `station_forecasts` の鮮度を見るので、閉じられなかった行には気づかない。
--
-- **1 周期より十分に古い `running` は「閉じられなかった」を意味する。** 推論は 5 分
-- 周期で `maxDuration` が 120 秒なので、`infer_alert_s`（既定 900 秒）を過ぎて
-- `running` のままなら、正常な実行中ではあり得ない。

-- ────────────────────────────────────────────────────────────────
-- 保持
-- ────────────────────────────────────────────────────────────────
create or replace function public.run_maintenance(p_keep_days integer default 60)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_run       bigint;
  v_created   integer;
  v_dropped   integer;
  v_logs      integer;
  v_inference integer;
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

    -- **`ok` だけを消す。** 失敗・二重抑止・閉じられなかった行は残す（0031 の冒頭）
    delete from public.inference_log
     where status = 'ok' and generated_at < now() - interval '90 days';
    get diagnostics v_inference = row_count;

    -- cron.job_run_details は自動削除されない。毎分ジョブで月 4.3 万行たまる（§4.3 の 3）
    begin
      delete from cron.job_run_details where end_time < now() - interval '7 days';
      get diagnostics v_details = row_count;
    exception when insufficient_privilege then
      v_details := -1;  -- 権限が無い環境では諦める（ローカルなど）
    end;

    v_result := jsonb_build_object(
      'partitions_created', v_created, 'partitions_dropped', v_dropped,
      'fetch_logs_deleted', v_logs, 'inference_logs_deleted', v_inference,
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
  'パーティションの作成・削除と、取得ログ 30 日超・推論ログ（ok のみ）90 日超・cron.job_run_details 7 日超の削除。';

-- ────────────────────────────────────────────────────────────────
-- 検査 6 に「閉じられなかった行」を足す
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
  v_stuck   integer;
  v_systems text;
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

  -- **閉じられなかった行**（§12 の 111）。推論は 5 分周期で maxDuration が 120 秒なので、
  -- 閾値を過ぎて running のままなら、正常な実行中ではあり得ない
  select count(*), string_agg(distinct system_id, ',' order by system_id)
    into v_stuck, v_systems
    from public.inference_log
   where status = 'running' and generated_at < now() - make_interval(secs => v_limit);

  if v_stuck > 0 then
    if public.send_alert('inference_stuck', jsonb_build_object(
         'message', '推論の記録が ' || v_stuck || ' 件、running のまま閉じられていません',
         'systems', v_systems,
         'threshold_s', v_limit), interval '6 hours') then
      v_alerts := v_alerts + 1;
    end if;
  end if;

  return jsonb_build_object('checked', v_checked, 'alerts', v_alerts,
                            'oldest_age_s', v_oldest, 'stuck', v_stuck,
                            'threshold_s', v_limit);
end;
$$;

comment on function public.check_inference() is
  'station_forecasts の最終 generated_at が infer_alert_s より古いシステムと、閉じられずに running のまま残った inference_log の行を通知する。';

revoke all on function public.check_inference() from public, anon, authenticated;
