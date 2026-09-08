-- 0020 ジョブの監視（W3 プラン §5.3、R19）
--
-- **`monitor_feeds` には足さない**（W3-02）。あちらは全検査を 1 つの
-- `begin ... exception when others` で包んでいるので、検査を 1 つ足すと、それが
-- 落ちたときに既存の監視まで止まる。508 回連続で成功している仕組みを増築で壊さない。
--
-- `monitor_feeds` が見るのは**フィード**（停滞・連続失敗・ポート数の急変・DB 容量）。
-- ここが見るのは**ジョブそのもの**——動いているか、失敗していないか、Parquet が
-- 欠けていないか、参照データの期限が近くないか。
--
-- 設計の要点
--   * **「失敗が在る」ではなく「直近 N 時間に成功が無い」を見る**（W3-01）。
--     cron が 1 度も起動しなければ `job_runs` に行が無く、`status = 'failed'` を
--     探しても何も見つからない。収集のウォッチドッグが `last_fetch_at` の古さを
--     見ているのと同じ考え方（W1-29）
--   * **検査を 4 つの小さな関数に分け、親が 1 つずつ例外を切り分けて呼ぶ。**
--     1 つが落ちても他は走る。単体でも呼べるのでテストが書ける
--   * 対象ジョブは表で持つ。ジョブが増えても関数を書き換えない
--   * `search_path = ''` なので `storage.objects` はスキーマ付きで書く。`postgres` は
--     **BYPASSRLS** を持つので `security definer` の中から全行が見える（本番・ローカル
--     とも 2026-09-08 に実測で確認。W3 プラン §4.1 の 2）
--
-- **この関数自身の停止は、この関数では検知できない。** pg_cron ごと止まれば
-- ウォッチドッグも監視も通知も止まる。`monitored_jobs` に自分を入れてあるのは、
-- **復旧したときに空白があったことを検出できる**ようにするため。

-- ────────────────────────────────────────────────────────────────
-- 監視対象のジョブ
-- ────────────────────────────────────────────────────────────────
create table public.monitored_jobs (
  job_name       text primary key,
  -- 期待する起動周期。人が読むためと、閾値の妥当性を制約で縛るために持つ
  expected_every interval not null,
  -- これだけの間 status='ok' が 1 件も無ければ通知する
  missing_after  interval not null,
  -- 登録した時刻。**登録直後は通知しない**（まだ 1 度も回っていないだけかもしれない）
  added_at       timestamptz not null default now(),
  is_active      boolean not null default true,
  note           text,
  -- 閾値が周期より短いと必ず鳴り続ける。設定ミスを制約で止める
  constraint monitored_jobs_window_sane check (missing_after > expected_every)
);

comment on table public.monitored_jobs is
  'monitor_jobs() が「直近 N 時間に成功が無い」を見る対象。行を足せば関数を変えずに監視が増える。';
comment on column public.monitored_jobs.added_at is
  '登録時刻。now() <= added_at + missing_after のあいだは通知しない（日次ジョブは 1 巡するまで判断できない）。';

alter table public.monitored_jobs enable row level security;
revoke all on table public.monitored_jobs from anon, authenticated;

-- 現在 pg_cron と Vercel Cron で動いているジョブをすべて入れる。
-- **毎分・5 分毎のものも入れる**：pg_cron が丸ごと止まれば検知できないが、
-- 個別のジョブが外された場合は検知できるし、復旧後に空白を残せる。
insert into public.monitored_jobs (job_name, expected_every, missing_after, note) values
  ('watchdog_collect',           interval '1 minute', interval '15 minutes', 'pg_cron 毎分。Vercel Cron の配信漏れを補う'),
  ('monitor_feeds',              interval '5 minutes', interval '30 minutes', 'pg_cron 5 分毎。フィードの停滞と連続失敗'),
  ('monitor_jobs',               interval '5 minutes', interval '30 minutes', 'pg_cron 5 分毎。自分自身。復旧後に空白を検出するため'),
  ('compact_parquet',            interval '1 hour',   interval '3 hours',    'Vercel Cron 毎時 :07。2 回落ちたら鳴る'),
  ('archive_weather',            interval '1 hour',   interval '3 hours',    'Vercel Cron 毎時 :17。落ちた時刻の予報は永久に失われる'),
  ('maintain_partitions',        interval '1 day',    interval '30 hours',   'pg_cron 03:00 JST。パーティションの作成と掃除'),
  ('refresh_station_activity',   interval '1 day',    interval '30 hours',   'pg_cron 03:30 JST'),
  ('sync_stations:hellocycling', interval '1 day',    interval '30 hours',   'Vercel Cron 04:00 JST'),
  ('sync_stations:docomo-cycle', interval '1 day',    interval '30 hours',   'Vercel Cron 04:00 JST');

-- ────────────────────────────────────────────────────────────────
-- 検査 1：直近 N 時間に成功が無いジョブ
-- ────────────────────────────────────────────────────────────────
-- **`exists` で書く。** `max(started_at)` を取ると job_runs をジョブ分だけ全走査するが、
-- `exists` なら (job_name, started_at desc) の索引を先頭で止められる。最後に成功した
-- 時刻は、通知を出すときだけ引けばよい（滅多に起きない）。
create or replace function public.check_jobs_missing()
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_row     record;
  v_alerts  integer := 0;
  v_checked integer := 0;
  v_last    timestamptz;
begin
  for v_row in
    select m.job_name, m.missing_after
      from public.monitored_jobs m
     where m.is_active and now() > m.added_at + m.missing_after
  loop
    v_checked := v_checked + 1;
    if not exists (
      select 1 from public.job_runs r
       where r.job_name = v_row.job_name
         and r.status = 'ok'
         and r.started_at > now() - v_row.missing_after
    ) then
      select max(r.started_at) into v_last
        from public.job_runs r where r.job_name = v_row.job_name and r.status = 'ok';
      if public.send_alert('job_missing:' || v_row.job_name, jsonb_build_object(
           'message', v_row.job_name || ' が直近 '
                      || round(extract(epoch from v_row.missing_after) / 60)::text
                      || ' 分のあいだ 1 度も成功していません',
           'last_ok_at', v_last,
           'threshold', v_row.missing_after::text), interval '3 hours') then
        v_alerts := v_alerts + 1;
      end if;
    end if;
  end loop;
  return jsonb_build_object('checked', v_checked, 'alerts', v_alerts);
end;
$$;

comment on function public.check_jobs_missing() is
  '直近 missing_after のあいだ status=ok が 1 件も無いジョブを通知する。登録直後は見ない。';

-- ────────────────────────────────────────────────────────────────
-- 検査 2：失敗したジョブ
-- ────────────────────────────────────────────────────────────────
-- **`monitored_jobs` に限らず全ジョブを見る。** どこで失敗しても知りたい。
-- 検査 1 と役割が違う：あちらは「動いていない」、こちらは「動いたが落ちた」。
create or replace function public.check_jobs_failed()
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_row    record;
  v_alerts integer := 0;
  v_jobs   integer := 0;
begin
  for v_row in
    select r.job_name, count(*) as n, max(r.started_at) as last_at,
           (array_agg(r.detail->>'error' order by r.started_at desc)
              filter (where r.detail ? 'error'))[1] as last_error
      from public.job_runs r
     where r.status = 'failed' and r.started_at > now() - interval '3 hours'
     group by r.job_name
  loop
    v_jobs := v_jobs + 1;
    if public.send_alert('job_failed:' || v_row.job_name, jsonb_build_object(
         'message', v_row.job_name || ' が直近 3 時間に ' || v_row.n || ' 回失敗しました',
         'count', v_row.n,
         'last_at', v_row.last_at,
         'last_error', v_row.last_error), interval '1 hour') then
      v_alerts := v_alerts + 1;
    end if;
  end loop;
  return jsonb_build_object('failed_jobs', v_jobs, 'alerts', v_alerts);
end;
$$;

comment on function public.check_jobs_failed() is
  '直近 3 時間に status=failed があるジョブを通知する。monitored_jobs に限らず全ジョブを見る。';

-- ────────────────────────────────────────────────────────────────
-- 検査 3：Parquet の欠落
-- ────────────────────────────────────────────────────────────────
-- `status_snapshots` に行が在るのに、対応する Parquet が Storage に無い時間帯を探す。
--
-- **窓は「直近 6 時間」だが、末尾に 1 時間の猶予を置く。** 毎時ジョブは :07 に前 1 時間を
-- 畳むので、:00〜:07 のあいだは「直前の時間がまだ無い」のが正常である。猶予が無いと
-- 毎時 7 分間だけ誤報が出る。
--
-- **全期間を突き合わせない。** 5 分毎に走るので、古い欠落の埋め戻しは別の話
-- （データ辞書 §13 の SQL を手で回す）。
create or replace function public.check_parquet_gap()
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  -- **UTC の「時」を起点にする。** timestamptz のまま date_trunc すると
  -- セッションの TimeZone に依存する。Parquet のパスは UTC なので、ここも UTC で揃える
  v_hour_now timestamp := date_trunc('hour', (now() at time zone 'UTC'));
  v_from     timestamp := v_hour_now - interval '7 hours';
  v_to       timestamp := v_hour_now - interval '1 hour';
  v_checked  integer;
  v_missing  text[];
  v_alerts   integer := 0;
begin
  with want as (
    select s.system_id,
           date_trunc('hour', s.observed_at at time zone 'UTC') as hour_utc
      from public.status_snapshots s
     where s.observed_at >= (v_from at time zone 'UTC')
       and s.observed_at <  (v_to   at time zone 'UTC')
     group by 1, 2
  )
  select count(*),
         array_agg(w.system_id || ' ' || to_char(w.hour_utc, 'YYYY-MM-DD HH24') || 'Z'
                   order by w.hour_utc, w.system_id)
           filter (where not exists (
             select 1 from storage.objects o
              where o.bucket_id = 'gbfs-parquet'
                and o.name = w.system_id
                          || '/date=' || to_char(w.hour_utc, 'YYYY-MM-DD')
                          || '/hour=' || to_char(w.hour_utc, 'HH24')
                          || '/part.parquet'))
    into v_checked, v_missing
    from want w;

  if v_missing is not null and cardinality(v_missing) > 0 then
    if public.send_alert('parquet_gap', jsonb_build_object(
         'message', '直近 6 時間の Parquet に ' || cardinality(v_missing) || ' 件の欠落があります',
         'missing', to_jsonb(v_missing),
         'window_utc', to_char(v_from, 'YYYY-MM-DD HH24') || 'Z 〜 ' || to_char(v_to, 'YYYY-MM-DD HH24') || 'Z'),
         interval '6 hours') then
      v_alerts := 1;
    end if;
  end if;

  return jsonb_build_object(
    'checked', coalesce(v_checked, 0),
    'missing', coalesce(cardinality(v_missing), 0),
    'alerts', v_alerts,
    'window_end_utc', to_char(v_to, 'YYYY-MM-DD HH24') || 'Z',
    'now_hour_utc', to_char(v_hour_now, 'YYYY-MM-DD HH24') || 'Z');
end;
$$;

comment on function public.check_parquet_gap() is
  'status_snapshots に行が在るのに Parquet が無い時間帯を直近 6 時間で探す（末尾 1 時間は猶予）。';

-- ────────────────────────────────────────────────────────────────
-- 検査 4：参照データの期限
-- ────────────────────────────────────────────────────────────────
-- いまのところ祝日だけ。内閣府の CSV は 2027-11-23 までしか収録しておらず、
-- **静かに祝日が消える**のが一番まずい（`day_type` が全部「平日」になる）。
--
-- `jp_holidays` は PR D で作る。**まだ無い状態でもこの関数は動く**：plpgsql は
-- 分岐に入らないかぎり SQL を計画しないので、`to_regclass` で存在を確かめてから読む。
create or replace function public.check_reference_data()
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_max_holiday date;
  v_alerts      integer := 0;
  v_days        integer;
begin
  if to_regclass('public.jp_holidays') is null then
    return jsonb_build_object('skipped', true, 'reason', 'jp_holidays がまだ無い（PR D で作る）');
  end if;

  select max(holiday_date) into v_max_holiday from public.jp_holidays;
  if v_max_holiday is null then
    return jsonb_build_object('skipped', true, 'reason', 'jp_holidays が空');
  end if;

  v_days := v_max_holiday - current_date;
  if v_days < 90 then
    if public.send_alert('holidays_expiring', jsonb_build_object(
         'message', '祝日データの収録が残り ' || v_days || ' 日です。内閣府の CSV を取り直してください',
         'max_holiday_date', v_max_holiday,
         'days_left', v_days), interval '7 days') then
      v_alerts := 1;
    end if;
  end if;

  return jsonb_build_object('max_holiday_date', v_max_holiday, 'days_left', v_days, 'alerts', v_alerts);
end;
$$;

comment on function public.check_reference_data() is
  '参照データの期限を見る。いまは祝日のみ。jp_holidays が無ければ skipped を返す。';

-- ────────────────────────────────────────────────────────────────
-- 親：4 つの検査を、1 つずつ例外を切り分けて呼ぶ
-- ────────────────────────────────────────────────────────────────
-- **検査どうしを独立させることがこの関数の唯一の仕事である。**
-- 1 つが落ちても残りは走り、落ちたことは (1) detail に記録し (2) 通知し
-- (3) 全体の status を failed にする、の 3 通りで残す。
--
-- 失敗を `failed` として残すので、**次の実行の検査 2 がそれを拾う**（自分自身の
-- 失敗も監視対象になる）。通知と二重になるが、通知が落ちても記録は残る。
create or replace function public.monitor_jobs()
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_run    bigint;
  v_names  text[] := array['check_jobs_missing', 'check_jobs_failed',
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

comment on function public.monitor_jobs() is
  '4 つの検査を例外を切り分けて呼ぶ。1 つ落ちても他は走り、落ちた事実は detail・通知・status の 3 つに残る。';

-- ────────────────────────────────────────────────────────────────
-- 権限：pg_cron は postgres として実行するので grant は要らない。
-- 匿名から呼べないことを明示的に閉じる（0009 と同じ作法）
-- ────────────────────────────────────────────────────────────────
revoke all on function public.check_jobs_missing() from public, anon, authenticated;
revoke all on function public.check_jobs_failed() from public, anon, authenticated;
revoke all on function public.check_parquet_gap() from public, anon, authenticated;
revoke all on function public.check_reference_data() from public, anon, authenticated;
revoke all on function public.monitor_jobs() from public, anon, authenticated;

-- ────────────────────────────────────────────────────────────────
-- pg_cron
-- ────────────────────────────────────────────────────────────────
-- **:04, :09, :14 … に置く。** monitor_feeds（:00, :05 …）、compact_parquet（:07）、
-- archive_weather（:17）と重ならない分を選ぶ。同時に走っても壊れはしないが、
-- 監視が「監視対象が動いている最中」を覗く必要は無い。
select cron.schedule('monitor_jobs', '4-59/5 * * * *', $$select public.monitor_jobs()$$);

notify pgrst, 'reload schema';
