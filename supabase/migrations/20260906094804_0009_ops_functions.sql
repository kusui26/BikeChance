-- 0009 運用の関数（W1 プラン §6.7）
--
-- pg_cron から呼ぶ関数の共通の作法：
--   * `pg_try_advisory_xact_lock(8423, <ジョブ固有の番号>)` で自分自身の多重起動を防ぐ。
--     **ジョブごとに別の番号を使う**（W1-30）。1 つの番号を共有すると、毎分動く
--     ウォッチドッグが、数分かかる日次の保守にブロックされてしまう
--   * 開始と終了を `job_runs` に記録する。例外は握って `failed` として記録し、
--     関数自体は正常に返す。plpgsql の例外はトランザクションを巻き戻すため、
--     再送出すると記録ごと消えてしまい「失敗した事実」が残らない（W1-31）
--   * 時刻の表示は JST。人が読むものなので（保存は UTC のまま）

-- ────────────────────────────────────────────────────────────────
-- 補助：ジョブの記録
-- ────────────────────────────────────────────────────────────────
create or replace function public.job_started(p_job_name text)
returns bigint
language sql
security definer
set search_path = ''
as $$
  insert into public.job_runs (job_name, status) values (p_job_name, 'running') returning id;
$$;

create or replace function public.job_finished(p_id bigint, p_status text, p_detail jsonb)
returns void
language sql
security definer
set search_path = ''
as $$
  update public.job_runs
     set finished_at = now(), status = p_status, detail = p_detail
   where id = p_id;
$$;

comment on function public.job_started(text) is 'ジョブの開始を job_runs に記録し、行 id を返す。';
comment on function public.job_finished(bigint, text, jsonb) is 'ジョブの終了を job_runs に記録する。';

/** 設定値を整数で読む。無ければ既定値。 */
create or replace function public.config_int(p_key text, p_default integer)
returns integer
language sql
stable
security definer
set search_path = ''
as $$
  select coalesce(
    (select nullif(regexp_replace(value, '\D', '', 'g'), '')::integer
       from public.app_config where key = p_key),
    p_default
  );
$$;

comment on function public.config_int(text, integer) is 'app_config の値を整数で読む。未設定なら既定値。';

-- ────────────────────────────────────────────────────────────────
-- send_alert — 同じ事象を繰り返し通知しない
-- ────────────────────────────────────────────────────────────────
-- Webhook が未設定（Vault に alert_webhook_url が無い）でも `alert_state` には記録し、
-- ジョブは失敗させない。通知の宛先が無いことと、何も起きていないことは別である。
create or replace function public.send_alert(
  p_key      text,
  p_payload  jsonb,
  p_suppress interval default '60 minutes'
)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_last_sent timestamptz;
  v_url       text;
  v_kind      text;
  v_text      text;
  v_body      jsonb;
begin
  select last_sent_at into v_last_sent from public.alert_state where alert_key = p_key;

  -- 抑制中：内容だけ更新して送らない
  if v_last_sent is not null and v_last_sent > now() - p_suppress then
    update public.alert_state set last_value = p_payload where alert_key = p_key;
    return false;
  end if;

  insert into public.alert_state (alert_key, last_sent_at, last_value)
  values (p_key, now(), p_payload)
  on conflict (alert_key) do update set last_sent_at = now(), last_value = excluded.last_value;

  select decrypted_secret into v_url
    from vault.decrypted_secrets where name = 'alert_webhook_url';
  if v_url is null then
    return true;  -- 記録はした。宛先が無いだけ
  end if;

  -- 人が読む文面。時刻は JST で書く
  v_text := format(
    '[BikeChance] %s%s%s(%s JST)',
    coalesce(p_payload->>'message', p_key),
    chr(10),
    coalesce(p_payload::text || chr(10), ''),
    to_char(timezone('Asia/Tokyo', now()), 'YYYY-MM-DD HH24:MI')
  );

  v_kind := coalesce((select value from public.app_config where key = 'alert_webhook_kind'), 'generic');
  v_body := case v_kind
    when 'discord' then jsonb_build_object('content', v_text)
    when 'slack'   then jsonb_build_object('text', v_text)
    else p_payload
  end;

  -- 引数順は url, body, params, headers, timeout_milliseconds。既定タイムアウトは 2 秒（§4.3 の 7）
  perform net.http_post(
    url := v_url,
    body := v_body,
    headers := jsonb_build_object('Content-Type', 'application/json'),
    timeout_milliseconds := 10000
  );
  return true;
end;
$$;

comment on function public.send_alert(text, jsonb, interval) is
  '通知を送る。同じ alert_key は抑制間隔の内側では 1 回だけ。送ったかどうかを返す。';

-- ────────────────────────────────────────────────────────────────
-- watchdog_collect — Vercel Cron の配信漏れを補う
-- ────────────────────────────────────────────────────────────────
-- Vercel Cron が正常なら `last_fetch_at` は収集周期ごとに更新されるため発火しない。
-- 発火するのは Cron が 2 回以上欠けたとき。`net.http_get` は応答を待たない
-- （fire-and-forget）ので、結果は次の分の `feed_state` で判断する。
--
-- 閾値は収集周期から導く（W1-29）。`*/5` の E1 では 630 秒、毎分の E2 では 150 秒になる。
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
    -- 収集周期 2 回分＋30 秒。E1（300 秒）で 630 秒、E2（60 秒）で 150 秒
    v_threshold := public.config_int('collect_interval_s', 60) * 2 + 30;

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
  'feed_state.last_fetch_at が閾値より古いシステムの収集を叩き直す。閾値は collect_interval_s から導く。';

-- ────────────────────────────────────────────────────────────────
-- monitor_feeds — 異常を検知して通知する
-- ────────────────────────────────────────────────────────────────
create or replace function public.monitor_feeds()
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_run      bigint;
  v_interval integer;
  v_alerts   integer := 0;
  v_row      record;
  v_db_bytes bigint;
begin
  if not pg_try_advisory_xact_lock(8423, 2) then
    return -1;
  end if;
  v_run := public.job_started('monitor_feeds');

  begin
    v_interval := public.config_int('collect_interval_s', 60);

    -- フィードの停滞。閾値は「期待周期の 3 倍」だが、収集周期の 3 倍を下回らせない。
    -- これにより E1（収集 5 分）ではドコモの 4 分閾値が 15 分に緩み、誤報が出ない（W1-29）
    for v_row in
      select f.system_id, f.last_observed_at, f.consecutive_errors, f.last_fetch_at,
             greatest(s.expected_cadence_s * 3, v_interval * 3) as stale_after_s
        from public.feed_state f join public.systems s using (system_id)
       where s.is_active
    loop
      if v_row.last_observed_at is not null
         and v_row.last_observed_at < now() - make_interval(secs => v_row.stale_after_s) then
        if public.send_alert('feed_stalled:' || v_row.system_id, jsonb_build_object(
             'message', v_row.system_id || ' のフィードが停滞しています',
             'last_observed_at', v_row.last_observed_at,
             'threshold_s', v_row.stale_after_s)) then
          v_alerts := v_alerts + 1;
        end if;
      end if;

      if v_row.consecutive_errors >= 5 then
        if public.send_alert('collector_errors:' || v_row.system_id, jsonb_build_object(
             'message', v_row.system_id || ' の収集が連続で失敗しています',
             'consecutive_errors', v_row.consecutive_errors)) then
          v_alerts := v_alerts + 1;
        end if;
      end if;

      if v_row.last_fetch_at is not null and v_row.last_fetch_at < now() - interval '10 minutes' then
        if public.send_alert('collector_silent:' || v_row.system_id, jsonb_build_object(
             'message', v_row.system_id || ' の収集器が反応しません（ウォッチドッグでも復帰せず）',
             'last_fetch_at', v_row.last_fetch_at)) then
          v_alerts := v_alerts + 1;
        end if;
      end if;
    end loop;

    -- 異常スナップショット
    if exists (
      select 1 from public.status_snapshots
       where is_anomalous and observed_at > now() - interval '1 hour'
    ) then
      if public.send_alert('anomalous_snapshot', jsonb_build_object(
           'message', '直近 1 時間に異常なスナップショットがあります',
           'count', (select count(*) from public.status_snapshots
                      where is_anomalous and observed_at > now() - interval '1 hour'))) then
        v_alerts := v_alerts + 1;
      end if;
    end if;

    -- DEFAULT パーティションに行が入った（保守ジョブが止まっている印。W1-15）
    if exists (select 1 from public.status_snapshots_default) then
      if public.send_alert('default_partition_used', jsonb_build_object(
           'message', 'DEFAULT パーティションに行が入りました。ensure_snapshot_partitions を確認してください',
           'rows', (select count(*) from public.status_snapshots_default))) then
        v_alerts := v_alerts + 1;
      end if;
    end if;

    -- ポート数の急変（24 時間前の中央値から ±5%）
    for v_row in
      with latest as (
        select distinct on (system_id) system_id, n_stations
          from public.status_snapshots order by system_id, observed_at desc
      ),
      baseline as (
        select system_id, percentile_cont(0.5) within group (order by n_stations) as median
          from public.status_snapshots
         where observed_at between now() - interval '25 hours' and now() - interval '23 hours'
         group by system_id
      )
      select l.system_id, l.n_stations, b.median
        from latest l join baseline b using (system_id)
       where b.median > 0 and abs(l.n_stations - b.median) > b.median * 0.05
    loop
      if public.send_alert('station_count_shift:' || v_row.system_id, jsonb_build_object(
           'message', v_row.system_id || ' のポート数が 24 時間前から 5% 以上変化しました',
           'now', v_row.n_stations, 'median_24h_ago', v_row.median)) then
        v_alerts := v_alerts + 1;
      end if;
    end loop;

    -- DB 容量（Pro の 8 GB に対する早期警告）。日 1 回でよい
    select pg_database_size(current_database()) into v_db_bytes;
    if v_db_bytes > 6 * 1024::bigint * 1024 * 1024 then
      if public.send_alert('db_size', jsonb_build_object(
           'message', 'DB が 6 GB を超えました。保持日数の見直しを検討してください',
           'bytes', v_db_bytes), interval '24 hours') then
        v_alerts := v_alerts + 1;
      end if;
    end if;

    perform public.job_finished(v_run, 'ok', jsonb_build_object('alerts', v_alerts));
    return v_alerts;
  exception when others then
    perform public.job_finished(v_run, 'failed',
      jsonb_build_object('error', sqlerrm, 'code', sqlstate));
    return 0;
  end;
end;
$$;

comment on function public.monitor_feeds() is
  '停滞・連続失敗・無反応・異常スナップショット・DEFAULT パーティション・ポート数の急変・DB 容量を検知する。';

-- ────────────────────────────────────────────────────────────────
-- run_maintenance — パーティションとログの保守
-- ────────────────────────────────────────────────────────────────
create or replace function public.run_maintenance(p_keep_days integer default 60)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_run     bigint;
  v_created integer;
  v_dropped integer;
  v_logs    integer;
  v_details integer := 0;
  v_result  jsonb;
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

    -- cron.job_run_details は自動削除されない。毎分ジョブで月 4.3 万行たまる（§4.3 の 3）
    begin
      delete from cron.job_run_details where end_time < now() - interval '7 days';
      get diagnostics v_details = row_count;
    exception when insufficient_privilege then
      v_details := -1;  -- 権限が無い環境では諦める（ローカルなど）
    end;

    v_result := jsonb_build_object(
      'partitions_created', v_created, 'partitions_dropped', v_dropped,
      'fetch_logs_deleted', v_logs, 'cron_details_deleted', v_details);
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
  'パーティションの作成・削除と、取得ログ 30 日超・cron.job_run_details 7 日超の削除。';

-- ────────────────────────────────────────────────────────────────
-- refresh_station_activity — 台帳の最終観測と活性を更新する
-- ────────────────────────────────────────────────────────────────
-- 収集の最短経路では `stations` に登録以外を書かない（W1-13）。ここが唯一の書き手。
--
-- **1 時間ごとに 1 スナップショットだけを見る。** 直近 25 時間の全スナップショットを
-- 展開すると 1,000 万行を超え、Micro インスタンスには重すぎる。`is_active` の判定は
-- 72 時間の窓なので、1 時間粒度で十分。
create or replace function public.refresh_station_activity()
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_run      bigint;
  v_seen     integer := 0;
  v_inactive integer := 0;
  v_active   integer := 0;
begin
  if not pg_try_advisory_xact_lock(8423, 4) then
    return -1;
  end if;
  v_run := public.job_started('refresh_station_activity');

  begin
    with sampled as (
      -- 1 時間ごとに最新の 1 件だけを選ぶ。異常なスナップショットは使わない
      select distinct on (system_id, date_trunc('hour', observed_at))
             system_id, observed_at, bikes
        from public.status_snapshots
       where observed_at >= now() - interval '25 hours' and not is_anomalous
       order by system_id, date_trunc('hour', observed_at), observed_at desc
    ),
    seen as (
      select s.system_id, u.ordinality - 1 as idx, max(s.observed_at) as last_seen
        from sampled s
        cross join lateral unnest(s.bikes) with ordinality as u(value, ordinality)
       where u.value <> -1
       group by 1, 2
    )
    update public.stations st
       set last_seen_at = greatest(coalesce(st.last_seen_at, '-infinity'::timestamptz), seen.last_seen)
      from seen
     where st.system_id = seen.system_id and st.idx = seen.idx;
    get diagnostics v_seen = row_count;

    -- 72 時間見えなければ非活性、再出現で活性（§3.6 の「72 時間未観測で非アクティブ化」）
    update public.stations
       set is_active = false
     where is_active
       and (last_seen_at is null or last_seen_at < now() - interval '72 hours')
       and first_seen_at < now() - interval '72 hours';
    get diagnostics v_inactive = row_count;

    update public.stations
       set is_active = true
     where not is_active and last_seen_at >= now() - interval '72 hours';
    get diagnostics v_active = row_count;

    perform public.job_finished(v_run, 'ok', jsonb_build_object(
      'seen', v_seen, 'deactivated', v_inactive, 'reactivated', v_active));
    return v_seen;
  exception when others then
    perform public.job_finished(v_run, 'failed',
      jsonb_build_object('error', sqlerrm, 'code', sqlstate));
    return 0;
  end;
end;
$$;

comment on function public.refresh_station_activity() is
  '直近 25 時間を 1 時間おきに見て stations.last_seen_at を更新し、72 時間未観測を非活性にする。';

-- ────────────────────────────────────────────────────────────────
-- compute_daily_quality — 日次 QA（JST の日付で集計する）
-- ────────────────────────────────────────────────────────────────
create or replace function public.compute_daily_quality(p_date date default null)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_run       bigint;
  v_date      date;
  v_from      timestamptz;
  v_to        timestamptz;
  v_rows      integer := 0;
  v_db_bytes  bigint;
  v_prev      bigint;
  v_summary   text := '';
  v_row       record;
begin
  if not pg_try_advisory_xact_lock(8423, 5) then
    return -1;
  end if;
  v_run := public.job_started('daily_quality');

  begin
    -- 既定は「JST の昨日」。人が読むレポートなので日付は JST（§5 の 17）
    v_date := coalesce(p_date, (timezone('Asia/Tokyo', now()))::date - 1);
    v_from := (v_date::timestamp) at time zone 'Asia/Tokyo';
    v_to   := ((v_date + 1)::timestamp) at time zone 'Asia/Tokyo';

    select pg_database_size(current_database()) into v_db_bytes;
    select nullif(regexp_replace(value, '\D', '', 'g'), '')::bigint into v_prev
      from public.app_config where key = 'db_size_bytes_at_last_quality';

    for v_row in
      select sy.system_id,
             sy.expected_cadence_s,
             count(s.observed_at) as n_snapshots,
             (86400 / sy.expected_cadence_s) as n_expected,
             coalesce(max(gap.gap_s), 0) as max_gap_s,
             count(*) filter (where s.is_anomalous) as n_anomalous
        from public.systems sy
        left join public.status_snapshots s
          on s.system_id = sy.system_id and s.observed_at >= v_from and s.observed_at < v_to
        left join lateral (
          select extract(epoch from (
                   lead(x.observed_at) over (order by x.observed_at) - x.observed_at))::integer as gap_s
            from public.status_snapshots x
           where x.system_id = sy.system_id and x.observed_at >= v_from and x.observed_at < v_to
        ) gap on true
       group by sy.system_id, sy.expected_cadence_s
    loop
      insert into public.daily_quality
        (system_id, quality_date, n_snapshots, n_expected, max_gap_s, n_errors, n_anomalous, db_bytes_delta)
      values (
        v_row.system_id, v_date, v_row.n_snapshots, v_row.n_expected, v_row.max_gap_s,
        (select count(*) from public.feed_fetch_log
          where system_id = v_row.system_id and not ok and fetched_at >= v_from and fetched_at < v_to),
        v_row.n_anomalous,
        case when v_prev is null then null else v_db_bytes - v_prev end
      )
      on conflict (system_id, quality_date) do update
        set n_snapshots = excluded.n_snapshots, n_expected = excluded.n_expected,
            max_gap_s = excluded.max_gap_s, n_errors = excluded.n_errors,
            n_anomalous = excluded.n_anomalous, db_bytes_delta = excluded.db_bytes_delta,
            computed_at = now();
      v_rows := v_rows + 1;
      v_summary := v_summary || format('%s %s/%s件 最大欠損%s秒 異常%s件%s',
        v_row.system_id, v_row.n_snapshots, v_row.n_expected, v_row.max_gap_s, v_row.n_anomalous, chr(10));
    end loop;

    insert into public.app_config (key, value)
    values ('db_size_bytes_at_last_quality', v_db_bytes::text)
    on conflict (key) do update set value = excluded.value, updated_at = now();

    -- 日次のダイジェストは抑制しない（毎日 1 回届いてよい）
    perform public.send_alert('daily_quality:' || v_date::text, jsonb_build_object(
      'message', format('%s の収集品質', v_date), 'summary', v_summary,
      'db_bytes', v_db_bytes), interval '1 second');

    perform public.job_finished(v_run, 'ok',
      jsonb_build_object('date', v_date, 'systems', v_rows, 'db_bytes', v_db_bytes));
    return v_rows;
  exception when others then
    perform public.job_finished(v_run, 'failed',
      jsonb_build_object('error', sqlerrm, 'code', sqlstate));
    return 0;
  end;
end;
$$;

comment on function public.compute_daily_quality(date) is
  '前日（JST）の取得数・期待数・最大欠損・エラー数・異常数・DB 増分を daily_quality に書き、要約を通知する。';

-- ────────────────────────────────────────────────────────────────
-- 権限：pg_cron は postgres として実行するため、明示的な grant は要らない。
-- 匿名から呼べないことは 0006 の既定権限で担保される（pgTAP が不変条件を見張る）。
-- ────────────────────────────────────────────────────────────────
revoke all on function public.job_started(text) from public, anon, authenticated;
revoke all on function public.job_finished(bigint, text, jsonb) from public, anon, authenticated;
revoke all on function public.config_int(text, integer) from public, anon, authenticated;
revoke all on function public.send_alert(text, jsonb, interval) from public, anon, authenticated;
revoke all on function public.watchdog_collect() from public, anon, authenticated;
revoke all on function public.monitor_feeds() from public, anon, authenticated;
revoke all on function public.run_maintenance(integer) from public, anon, authenticated;
revoke all on function public.refresh_station_activity() from public, anon, authenticated;
revoke all on function public.compute_daily_quality(date) from public, anon, authenticated;

notify pgrst, 'reload schema';
