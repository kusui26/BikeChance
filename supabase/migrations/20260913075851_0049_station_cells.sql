-- 0049 低ズームの格子集約（W5 プラン §6.5、W5-12）
--
-- **東京駅中心 0.10 度（約 11 km）で 1,091 ポートになり、いまは 400 が返る**（§13.6）。
-- iPhone の画面で「都心ぜんぶ」を見た瞬間がこれである。**セルにまとめれば 100 個以下**に
-- なる（0.01 度セルは全国で 5,587 個・最大 31・平均 3.72）。
--
-- ────────────────────────────────────────────────────────────────
-- なぜ SQL でやるのか
-- ────────────────────────────────────────────────────────────────
-- **1,000 件の上限を超えた行を TypeScript まで持ってくると、上限を置いた意味が無い。**
-- 0.5 度の矩形は 8,805 ポートで、応答に載せないとしても**引くだけで 3 MB 動く**。
-- 集約は数える場所でやる。
--
-- ────────────────────────────────────────────────────────────────
-- 代表値は「最大」であって「平均」ではない（W5-12）
-- ────────────────────────────────────────────────────────────────
-- 利用者の問いは「このあたりで借りられるか」で、**1 台でもあれば答えは「借りられる」**。
-- 平均は「どのポートも五分五分」と「1 つは確実で残りは駄目」を**同じ色**にしてしまう。
--
-- **10 点それぞれの最大を取った曲線**を返し、補間は呼ぶ側（`interpolateForecast`）が
-- する。**補間をここで書かない**のが要点で、書けば同じ規則の 2 つ目の実装ができる
-- （W5 プラン §12 の 142 と同じ形）。
--
-- **`max` と補間は交換しない。** 返すのは真の最大の**上限**で、差が出るのは
-- 「隣り合う水平で別のポートが最大になる」セルだけである（実測は §13.8）。
--
-- ────────────────────────────────────────────────────────────────
-- 判断は呼ぶ側から渡す
-- ────────────────────────────────────────────────────────────────
-- **鮮度の閾値（`p_fresh_after`）も水平の並び（`p_horizons`）も引数で受ける。**
-- どちらも `packages/shared` に正がある規則で、ここで書き直すと 2 か所になる。
-- ここでやるのは**比較だけ**である（`v1_station_hourly` の `since` と同じ作法）。
--
-- **`p_horizons` と一致しない行は確率の集約から外す。** 版の入れ替えの最中に
-- 別の並びが混ざると、**添字がずれたまま最大を取る**ことになる。

create or replace function public.v1_station_cells(
  p_west         numeric,
  p_south        numeric,
  p_east         numeric,
  p_north        numeric,
  p_cell_deg     numeric,
  p_system       text        default null,
  p_fresh_after  timestamptz default null,
  p_horizons     smallint[]  default null
)
returns table (
  west          double precision,
  south         double precision,
  east          double precision,
  north         double precision,
  n_stations    integer,
  bikes         integer,
  docks         integer,
  stale         boolean,
  p_bike_x1000  smallint[],
  p_dock_x1000  smallint[],
  confidence    smallint,
  generated_at  timestamptz,
  n_forecast    integer
)
language sql
stable
security definer
set search_path = ''
as $$
  with inside as (
    select s.*,
           -- **`quantizeBbox` と同じ丸め方**（`packages/shared/src/bbox.ts` の `snap`）。
           -- 端数を 9 桁で落としてから床を取る——別の丸めを持ち込むと、セルの境界と
           -- 応答の実効矩形がずれる
           floor(round(s.lon::numeric / p_cell_deg, 9)) as ix,
           floor(round(s.lat::numeric / p_cell_deg, 9)) as iy
      from public.v1_stations_current s
     where s.lat is not null
       and s.lon is not null
       and s.lat between p_south and p_north
       and s.lon between p_west  and p_east
       and (p_system is null or s.system_id = p_system)
  ),
  -- 確率に寄与できる行。**欠けているもの・古いもの・並びの違うものは入れない**
  contributing as (
    select i.ix, i.iy,
           i.forecast_p_bike_x1000 as bike,
           i.forecast_p_dock_x1000 as dock,
           i.forecast_confidence   as confidence,
           i.forecast_generated_at as generated_at
      from inside i
     where i.forecast_p_bike_x1000 is not null
       and i.forecast_p_dock_x1000 is not null
       and i.forecast_confidence   is not null
       and i.forecast_generated_at is not null
       and (p_horizons    is null or i.forecast_horizons_min      =  p_horizons)
       and (p_fresh_after is null or i.forecast_base_observed_at >= p_fresh_after)
  ),
  -- **水平ごとの最大。** `unnest(a, b)` は 2 本を同じ添字で並べて開く
  peaks as (
    select c.ix, c.iy, t.pos,
           max(t.b)::smallint as b,
           max(t.d)::smallint as d
      from contributing c,
           lateral unnest(c.bike, c.dock) with ordinality as t(b, d, pos)
     group by c.ix, c.iy, t.pos
  ),
  curves as (
    select p.ix, p.iy,
           array_agg(p.b order by p.pos) as p_bike_x1000,
           array_agg(p.d order by p.pos) as p_dock_x1000
      from peaks p
     group by p.ix, p.iy
  ),
  forecasts as (
    select c.ix, c.iy,
           max(c.confidence)::smallint as confidence,
           -- **いちばん古い生成時刻。** 補間する位置は `in_min ＋（いま − これ）`なので、
           -- 古いほうを採ると水平が長くなる——**確率を大きく見せない側**に倒れる
           min(c.generated_at) as generated_at,
           count(*)::integer as n_forecast
      from contributing c
     group by c.ix, c.iy
  ),
  counts as (
    select i.ix, i.iy,
           count(*)::integer as n_stations,
           -- **観測のあるポートだけ足す。** `is_present` でない行は、値がいつのものか
           -- 分からない（`v1_stations_current` の `observed_at` を null にするのと同じ理由）
           sum(i.bikes) filter (where i.is_present)::integer as bikes,
           sum(i.docks) filter (where i.is_present)::integer as docks,
           bool_or(not i.is_present) as stale
      from inside i
     group by i.ix, i.iy
  )
  select round(n.ix * p_cell_deg, 6)::double precision as west,
         round(n.iy * p_cell_deg, 6)::double precision as south,
         round((n.ix + 1) * p_cell_deg, 6)::double precision as east,
         round((n.iy + 1) * p_cell_deg, 6)::double precision as north,
         n.n_stations,
         n.bikes,
         n.docks,
         n.stale,
         cu.p_bike_x1000,
         cu.p_dock_x1000,
         f.confidence,
         f.generated_at,
         coalesce(f.n_forecast, 0) as n_forecast
    from counts n
    left join curves    cu on cu.ix = n.ix and cu.iy = n.iy
    left join forecasts f  on f.ix  = n.ix and f.iy  = n.iy
   -- **並びを固定する。** 同じ要求が同じ応答になり、CDN も差分も効く（`STATION_ORDER` と同じ方針）
   order by n.iy, n.ix;
$$;

comment on function public.v1_station_cells(
  numeric, numeric, numeric, numeric, numeric, text, timestamptz, smallint[]
) is
  '低ズーム用の格子集約（W5 の PR E）。台数は合計、確率は水平ごとの最大の曲線。補間は呼ぶ側が行う。';

-- **`/v1` はサービスロールで読む**（`apps/web/lib/api/env.ts`）。匿名には渡さない。
revoke all on function public.v1_station_cells(
  numeric, numeric, numeric, numeric, numeric, text, timestamptz, smallint[]
) from public, anon, authenticated;
grant execute on function public.v1_station_cells(
  numeric, numeric, numeric, numeric, numeric, text, timestamptz, smallint[]
) to service_role;
