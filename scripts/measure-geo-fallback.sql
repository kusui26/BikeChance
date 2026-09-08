-- 孤立ポート（1 km 以内に近傍が無い）の控えとして、pref_code と muni_code の
-- どちらが役に立つかを測る。W3 プラン §10.2a を実装後に測り直したもの。
--
-- **読み取りのみ。** 本番に対してそのまま流せる。一時表しか作らない。
--
--   psql "$SUPABASE_DB_URL" -f scripts/measure-geo-fallback.sql
--
-- 測り方（§10.2a と同じ）
--   * 対象は住所を持つ HELLO のポート
--   * 指標はポート毎の P(bikes = 0)（貸出可の観測のうち 0 台だった割合）
--   * **そのポート自身を使わずに**（leave-one-out）当て、MAE で比べる
--   * 観測が少ないポートは外す（既定 100 回以上）
--
-- 所見 93：計測はスクリプトに残す。数字が設計判断の根拠になる以上、コードと同じ資産である。

\set ON_ERROR_STOP on
\set day '2026-09-07'
\set min_obs 100

\echo '── 1. 住所から pref_code / muni_code を引く ──────────────────'

-- 行政区画コードは stations の列を読まず、住所から引き直す。
-- rebuild_station_geo() と同じ規則（muni_codes への前方一致・最長）。
-- こうしておくと **日次ジョブが動く前でも** 測れる。
create temp table geo as
with addressed as (
  select a.system_id, a.station_id, a.raw->>'address' as address
    from public.station_attributes a
   where a.valid_to is null and a.raw ? 'address'
     and length(btrim(coalesce(a.raw->>'address', ''))) > 0
),
with_pref as (
  select d.system_id, d.station_id, d.address, p.pref_name, p.pref_code
    from addressed d
    join (select distinct pref_name, pref_code from public.muni_codes) p
      on d.address like p.pref_name || '%'
),
matched as (
  select w.system_id, w.station_id, w.pref_code, m.muni_code,
         row_number() over (partition by w.system_id, w.station_id
                            order by length(m.muni_name) desc) as rank
    from with_pref w
    join public.muni_codes m
      on m.pref_code = w.pref_code
     and substr(w.address, length(w.pref_name) + 1) like m.muni_name || '%'
)
select system_id, station_id, pref_code, muni_code from matched where rank = 1;

select count(*) as addresses_matched,
       count(distinct muni_code) as municipalities from geo;

\echo ''
\echo '── 2. ポート毎の P(bikes = 0) を出す ─────────────────────────'

create temp table port_rate as
with snaps as (
  select bikes, flags
    from public.status_snapshots
   where system_id = 'hellocycling'
     and observed_at >= (:'day'::date)::timestamp at time zone 'Asia/Tokyo'
     and observed_at <  (:'day'::date + 1)::timestamp at time zone 'Asia/Tokyo'
     and not is_anomalous
),
obs as (
  select s.idx - 1 as idx, s.bikes, f.flags
    from snaps
    cross join lateral unnest(snaps.bikes) with ordinality as s(bikes, idx)
    cross join lateral (select snaps.flags[s.idx] as flags) f
   where s.bikes >= 0            -- -1 は「観測されなかった」
     and (f.flags & 2) = 2       -- is_renting。休止中は数えない（データ辞書 §10.1）
)
select st.system_id, st.station_id, g.pref_code, g.muni_code,
       count(*) as n_obs,
       avg((o.bikes = 0)::int::double precision) as p0
  from obs o
  join public.stations st on st.system_id = 'hellocycling' and st.idx = o.idx
  left join geo g on g.system_id = st.system_id and g.station_id = st.station_id
 group by 1, 2, 3, 4;

select count(*) as ports, sum(n_obs) as observations,
       count(*) filter (where n_obs >= :min_obs) as kept
  from port_rate;

\echo ''
\echo '── 3. 1 km 以内の近傍を数える（station_neighbors は 500 m までなので作り直す）'

create temp table nb_count as
with pts as materialized (
  select s.system_id, s.station_id, a.lat, a.lon,
         floor(a.lat / 0.01)::integer as gy, floor(a.lon / 0.01)::integer as gx
    from public.stations s
    join public.station_attributes a
      on a.system_id = s.system_id and a.station_id = s.station_id and a.valid_to is null
   where a.lat is not null and a.lon is not null and coalesce(a.geo_suspect, false) = false
),
-- 1 km は 0.01 度の 3 x 3 では覆いきれない（§10.2）。5 x 5 を使う
probes as (
  select p.*, p.gy + dy as ky, p.gx + dx as kx
    from pts p, generate_series(-2, 2) dy, generate_series(-2, 2) dx
)
select l.system_id, l.station_id, count(*) as n_nb_1km
  from probes l
  join pts r on r.gy = l.ky and r.gx = l.kx
 where (r.system_id, r.station_id) is distinct from (l.system_id, l.station_id)
   and 2 * 6371000 * asin(sqrt(
         power(sin(radians(r.lat - l.lat) / 2), 2)
         + cos(radians(l.lat)) * cos(radians(r.lat))
           * power(sin(radians(r.lon - l.lon) / 2), 2))) <= 1000
 group by 1, 2;

\echo ''
\echo '── 4. leave-one-out で当てる ─────────────────────────────────'

create temp table loo as
with base as (
  select p.*, coalesce(n.n_nb_1km, 0) as n_nb_1km
    from port_rate p
    left join nb_count n on n.system_id = p.system_id and n.station_id = p.station_id
   where p.n_obs >= :min_obs
),
agg as (
  select b.*,
         sum(p0) over ()                     as g_sum, count(*) over ()                     as g_n,
         sum(p0) over (partition by pref_code) as p_sum, count(*) over (partition by pref_code) as p_n,
         sum(p0) over (partition by muni_code) as m_sum, count(*) over (partition by muni_code) as m_n
    from base b
)
select station_id, p0, n_nb_1km, pref_code, muni_code,
       (g_sum - p0) / nullif(g_n - 1, 0) as pred_global,
       (p_sum - p0) / nullif(p_n - 1, 0) as pred_pref,
       (m_sum - p0) / nullif(m_n - 1, 0) as pred_muni
  from agg;

\echo ''
\echo '全ポート（観測 :min_obs 回以上）'
select count(*) as ports,
       round(avg(abs(p0 - pred_global))::numeric, 4) as mae_global,
       round(avg(abs(p0 - pred_pref))::numeric,   4) as mae_pref,
       round(avg(abs(p0 - pred_muni))::numeric,   4) as mae_muni
  from loo;

\echo ''
\echo '**1 km 以内に近傍が 1 つも無いポートだけ**（行政区画コードを実際に使う場面）'
select count(*) as ports,
       round(avg(abs(p0 - pred_global))::numeric, 4) as mae_global,
       round(avg(abs(p0 - pred_pref))::numeric,   4) as mae_pref,
       round(avg(abs(p0 - pred_muni))::numeric,   4) as mae_muni,
       round((avg(abs(p0 - pred_global)) - avg(abs(p0 - pred_pref)))::numeric, 4)  as pref_の改善,
       round((avg(abs(p0 - pred_global)) - avg(abs(p0 - pred_muni)))::numeric,  4) as muni_の改善
  from loo where n_nb_1km = 0;

\echo ''
\echo '近傍数の帯ごと'
select case when n_nb_1km = 0 then '0 件'
            when n_nb_1km <= 2 then '1-2 件'
            when n_nb_1km <= 4 then '3-4 件'
            when n_nb_1km <= 9 then '5-9 件'
            else '10 件以上' end as 帯,
       count(*) as ports,
       round(avg(abs(p0 - pred_global))::numeric, 4) as mae_global,
       round(avg(abs(p0 - pred_pref))::numeric,   4) as mae_pref,
       round(avg(abs(p0 - pred_muni))::numeric,   4) as mae_muni
  from loo group by 1 order by min(n_nb_1km);

drop table loo, nb_count, port_rate, geo;
