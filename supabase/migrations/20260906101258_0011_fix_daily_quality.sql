-- 0011 compute_daily_quality の集計が二乗になっていたのを直す（W1 プラン §5.5 の 41）
--
-- 0009 では、欠損時間を出す副問い合わせを `left join lateral (...) on true` で足していた。
-- この横結合はスナップショット 1 件につき 1 行を返すため、既にスナップショットと結合した
-- 行と**直積**になり、`count(s.observed_at)` が M 件ではなく M² 件になっていた。
-- 本番の実測で 6 件が 36 件と数えられることを確認している。`n_anomalous` も同じだけ膨らむ。
--
-- 0009 は既に本番へ適用済みで、適用済みのマイグレーションを書き換えても流れ直さない。
-- 本番へ手動 SQL を当てるのは禁じているため（CLAUDE.md §6）、追補として関数を置き換える。
--
-- 直し方は「1 つの CTE に 1 つの仕事」。範囲の切り出し・欠損の計算・件数の集計を分け、
-- 結合はシステムごとに 1 行になるところまで畳んでから行う。
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
      with in_range as (
        select system_id, observed_at, is_anomalous
          from public.status_snapshots
         where observed_at >= v_from and observed_at < v_to
      ),
      counted as (
        select system_id,
               count(*) as n_snapshots,
               count(*) filter (where is_anomalous) as n_anomalous
          from in_range
         group by system_id
      ),
      gaps as (
        select system_id,
               extract(epoch from (
                 lead(observed_at) over (partition by system_id order by observed_at) - observed_at
               ))::integer as gap_s
          from in_range
      ),
      widest_gap as (
        select system_id, max(gap_s) as max_gap_s from gaps group by system_id
      )
      select sy.system_id,
             sy.expected_cadence_s,
             coalesce(c.n_snapshots, 0) as n_snapshots,
             (86400 / sy.expected_cadence_s) as n_expected,
             coalesce(g.max_gap_s, 0) as max_gap_s,
             coalesce(c.n_anomalous, 0) as n_anomalous
        from public.systems sy
        left join counted c using (system_id)
        left join widest_gap g using (system_id)
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

revoke all on function public.compute_daily_quality(date) from public, anon, authenticated;

notify pgrst, 'reload schema';
