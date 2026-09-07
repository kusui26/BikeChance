-- 0013 停滞の閾値に「検知の遅れ」を足し、ドコモの期待周期を実測値に直す
--     （W1 プラン §5.6 の 42・43。24 時間観測の実測にもとづく）
--
-- ## なぜ直すか
--
-- W1-29 の式 `max(expected_cadence_s * 3, collect_interval_s * 3)` は、**フィードが
-- 何秒おきに公開するか**しか見ていない。しかし `monitor_feeds` が実際に比べるのは
-- `now() - last_observed_at` で、これは
--
--     公開の間隔 ＋ その公開を我々が取りに行くまでの時間
--
-- になる。後半は最大で収集周期ぶんある（毎分収集なら 60 秒）。式にこの項が無いため、
-- 閾値が構造的に足りていなかった。
--
-- 24 時間の実測（2026-09-06 20:56 〜 2026-09-07 20:58 JST、ドコモ 1,071 区間）：
--
--     観測の遅れ   中央値 111 秒 / p95 139 秒 / p99 194 秒 / **最大 244 秒**
--     旧しきい値   240 秒 → **1 回超えていた**（20:19:27〜20:23:31 JST）
--
-- 通知が出なかったのは `monitor_feeds` が 5 分おきで、その瞬間を偶然またいだだけ。
-- 放置すればいずれ誤報が出る。式に `+ collect_interval_s` を足すと 300 秒になり、
-- 実測の最大に対して 23% の余裕ができる。
--
--     ドコモ  E2: max(243, 180) + 60 = 303 秒 ／ E1: max(243, 900) + 300 = 1,200 秒
--     HELLO   E2: max(900, 180) + 60 = 960 秒 ／ E1: max(900, 900) + 300 = 1,200 秒
--
-- ## ドコモの期待周期を 80 → 81 秒に
--
-- 24 時間で 1,068 回公開された（86,400 ÷ 1,068 ＝ 80.9 秒）。中央値は 80 秒だが、
-- 1 周期とばす回が 16 回あり、平均は 81 秒になる。`compute_daily_quality` の
-- `n_expected` は 86400 ÷ 期待周期なので、80 のままだと毎日 1,080 と出て、
-- 取りこぼしが無くても永久に 98.9% と表示される。**鳴りっぱなしの警報は無視される。**
--
-- 停滞の閾値への影響は 300 → 303 秒でほぼ無い。日数を重ねて系統的にずれるようなら
-- また測り直す。

update public.systems set expected_cadence_s = 81 where system_id = 'docomo-cycle';

comment on column public.systems.expected_cadence_s is
  'フィードの実測更新周期（秒）。HELLO 300 / ドコモ 81（24 時間の実測 86400÷1068）。監視の期待値に使う。';

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

    -- フィードの停滞。「公開の間隔」に加えて「その公開を取りに行くまでの時間」も
    -- 待たなければならない（後者は最大で収集周期ぶん）。収集周期の 3 倍で下支えするのは
    -- E1 でドコモの誤報を防ぐため（W1-29）
    for v_row in
      select f.system_id, f.last_observed_at, f.consecutive_errors, f.last_fetch_at,
             greatest(s.expected_cadence_s * 3, v_interval * 3) + v_interval as stale_after_s
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

revoke all on function public.monitor_feeds() from public, anon, authenticated;

notify pgrst, 'reload schema';
