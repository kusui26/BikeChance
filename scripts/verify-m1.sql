-- M1（24 時間 QA）の合格判定（W1 プラン §1 の合格基準表、§8 の各節）
--
-- 使い方:
--   psql "$SUPABASE_DB_URL" -f scripts/verify-m1.sql
--   psql "$SUPABASE_DB_URL" -v from_at='2026-09-06T11:10:00Z' \
--                           -v baseline_bytes=22000000 -f scripts/verify-m1.sql
--
--   from_at        判定する窓の開始。省略すると「今から 24 時間前」
--   baseline_bytes 窓の開始時点の pg_database_size。渡すと DB 増分を判定する
--
-- **時刻の基準を混ぜないこと。** スナップショットの件数は `observed_at`（フィードの
-- last_updated）で数えるが、Storage との突き合わせは `fetched_at` と `created_at` で
-- 数える。基準を混ぜると、デプロイ直後の 1 件がずれて「不一致」に見える。
--
-- ここで判定できないのは 2 つ。
--   * Active CPU        … Vercel Observability で見る（§8.4）
--   * 配列と生 JSON の照合 … scripts/reconcile-snapshot.mjs を各システム 3 回（§8.6）

\if :{?from_at}
\else
  \set from_at ''
\endif
\if :{?baseline_bytes}
\else
  \set baseline_bytes ''
\endif

\pset border 2
\pset title 'M1 合格判定'

with w as (
  select coalesce(nullif(:'from_at', '')::timestamptz, now() - interval '24 hours') as from_at,
         now() as to_at,
         nullif(:'baseline_bytes', '')::bigint as baseline_bytes
),
-- 件数と欠損は観測時刻で見る（フィードが何回更新されたか）
snap as (
  select s.system_id, s.observed_at
    from public.status_snapshots s, w
   where s.observed_at >= w.from_at and s.observed_at < w.to_at
),
-- Storage との突き合わせは取得時刻で見る（同じ 1 回の取得で両方に書くため）
snap_fetched as (
  select s.system_id, count(*) as n
    from public.status_snapshots s, w
   where s.fetched_at >= w.from_at and s.fetched_at < w.to_at
   group by 1
),
objects as (
  select split_part(o.name, '/', 1) as system_id, count(*) as n
    from storage.objects o, w
   where o.bucket_id = 'gbfs-raw' and o.name like '%station_status_%'
     and o.created_at >= w.from_at and o.created_at < w.to_at
   group by 1
),
fetches as (
  select f.ok, f.result
    from public.feed_fetch_log f, w
   where f.fetched_at >= w.from_at and f.fetched_at < w.to_at
),
gaps as (
  select system_id,
         lead(observed_at) over (partition by system_id order by observed_at) - observed_at as gap
    from snap
),
counted as (
  select
    (select count(*) from snap where system_id = 'hellocycling') as hello_n,
    (select count(*) from snap where system_id = 'docomo-cycle') as docomo_n,
    (select coalesce(max(gap), interval '0') from gaps) as max_gap,
    (select count(*) from (select system_id, observed_at from snap
                            group by 1, 2 having count(*) > 1) d) as dup_n,
    (select count(*) from public.status_snapshots_default) as default_n,
    (select count(*) from fetches) as calls,
    (select count(*) filter (where not ok) from fetches) as errors,
    (select count(*) from objects o full join snap_fetched s using (system_id)
      where coalesce(o.n, -1) <> coalesce(s.n, -1)) as storage_mismatch,
    (select coalesce(string_agg(coalesce(o.system_id, s.system_id) || ' '
                                || coalesce(o.n, 0) || '/' || coalesce(s.n, 0), '、'
                                order by coalesce(o.system_id, s.system_id)), '対象なし')
       from objects o full join snap_fetched s using (system_id)) as storage_pairs,
    pg_database_size(current_database()) as now_bytes
),
verdict as (
  select 1 as ord, 'HELLO のスナップショット取得数' as 指標,
         hello_n || ' 件' as 実測, '≥ 287 件' as 合格ライン,
         case when hello_n >= 287 then 'PASS' else 'FAIL' end as 判定 from counted
  union all
  select 2, 'ドコモのスナップショット取得数', docomo_n || ' 件', '≥ 1,074 件',
         case when docomo_n >= 1074 then 'PASS' else 'FAIL' end from counted
  union all
  select 3, '連続欠損の最大長',
         to_char(max_gap, 'HH24:MI:SS'), '< 30 分',
         case when max_gap < interval '30 minutes' then 'PASS' else 'FAIL' end from counted
  union all
  select 4, '重複行（同一 system_id, observed_at）', dup_n || ' 行', '0 行',
         case when dup_n = 0 then 'PASS' else 'FAIL' end from counted
  union all
  select 5, 'Storage の生 JSON 件数とスナップショット数（生/DB）',
         storage_pairs, '完全一致',
         case when storage_mismatch = 0 then 'PASS' else 'FAIL' end from counted
  union all
  select 6, 'DB サイズの増分',
         case when (select baseline_bytes from w) is null
              then pg_size_pretty(now_bytes) || '（現在値のみ）'
              else pg_size_pretty(now_bytes - (select baseline_bytes from w)) end,
         '≤ 40 MB/日',
         case when (select baseline_bytes from w) is null then '要ベースライン'
              when now_bytes - (select baseline_bytes from w) <= 40 * 1024 * 1024 then 'PASS'
              else 'FAIL' end from counted
  union all
  select 7, '収集の Active CPU', '—', '≤ 0.3 秒/回', '手動（Vercel）'
  union all
  select 8, '収集エンドポイントの失敗率',
         case when calls = 0 then '呼び出しなし'
              else round(100.0 * errors / calls, 2) || '%（' || errors || '/' || calls || '）' end,
         '< 1%',
         case when calls = 0 then 'FAIL'
              when 100.0 * errors / calls < 1 then 'PASS' else 'FAIL' end from counted
  union all
  select 9, '配列と生 JSON の照合（§8.6）', '—', '各システム 3 件で不一致 0', '別スクリプト'
  union all
  select 10, 'DEFAULT パーティションの行数', default_n || ' 行', '0 行',
         case when default_n = 0 then 'PASS' else 'FAIL' end from counted
)
select ord as "#", 指標, 実測, 合格ライン, 判定 from verdict order by ord;

\pset title '内訳（判定の読み解きと、§8.4 の表に書き足す値）'

with w as (
  select coalesce(nullif(:'from_at', '')::timestamptz, now() - interval '24 hours') as from_at,
         now() as to_at
),
f as (
  select f.system_id, f.result, f.ok, f.source, f.duration_ms, f.bytes
    from public.feed_fetch_log f, w
   where f.fetched_at >= w.from_at and f.fetched_at < w.to_at
),
s as (
  select s.system_id, s.observed_at,
         pg_column_size(s.bikes) + pg_column_size(s.docks)
       + pg_column_size(s.flags) + pg_column_size(s.reported_age_s) as arr_bytes
    from public.status_snapshots s, w
   where s.observed_at >= w.from_at and s.observed_at < w.to_at
),
g as (
  select system_id,
         extract(epoch from (lead(observed_at) over (partition by system_id order by observed_at)
                             - observed_at))::int as gap_s
    from s
)
select 1 as ord, '窓' as 項目, '—' as システム,
       to_char(timezone('Asia/Tokyo', (select from_at from w)), 'MM/DD HH24:MI')
       || ' 〜 ' || to_char(timezone('Asia/Tokyo', (select to_at from w)), 'MM/DD HH24:MI')
       || ' JST（' || round(extract(epoch from ((select to_at from w) - (select from_at from w))) / 3600, 1)
       || ' 時間）' as 値
union all
select 2, '取得の内訳', system_id,
       string_agg(result || ' ' || n, '、' order by result)
  from (select system_id, result, count(*) as n from f group by 1, 2) t group by system_id
union all
select 3, '304 の比率', system_id,
       round(100.0 * count(*) filter (where result = 'unchanged') / nullif(count(*), 0)) || '%'
  from f group by system_id
union all
select 4, 'ウォッチドッグの発火', '—', count(*) || ' 回' from f where source = 'watchdog'
union all
select 5, '所要時間（中央値／最大）', system_id,
       string_agg(result || ' ' || med || '／' || mx || ' ms', '、' order by result)
  from (select system_id, result,
               (percentile_cont(0.5) within group (order by duration_ms))::int as med,
               max(duration_ms) as mx
          from f where duration_ms is not null group by 1, 2) t group by system_id
union all
select 6, 'フィードの更新間隔（中央値／p95／最大）', system_id,
       (percentile_cont(0.5) within group (order by gap_s))::int || '／'
       || (percentile_cont(0.95) within group (order by gap_s))::int || '／'
       || max(gap_s) || ' 秒'
  from g where gap_s is not null group by system_id
union all
select 7, '配列 4 本のサイズ（1 行の中央値）', system_id,
       pg_size_pretty((percentile_cont(0.5) within group (order by arr_bytes))::bigint)
  from s group by system_id
union all
select 8, '配列の合計（窓の間に書いた分）', system_id, pg_size_pretty(sum(arr_bytes)::bigint)
  from s group by system_id
union all
select 9, '生 gzip の合計（窓の間に保存した分）', split_part(o.name, '/', 1),
       count(*) || ' 個 ／ ' || pg_size_pretty(sum((o.metadata->>'size')::bigint))
  from storage.objects o, w
 where o.bucket_id = 'gbfs-raw' and o.name like '%station_status_%'
   and o.created_at >= w.from_at and o.created_at < w.to_at
 group by 3
union all
select 10, '次回のベースラインに使う値', '—',
       pg_database_size(current_database()) || ' バイト（-v baseline_bytes= に渡す）'
order by ord, システム;
