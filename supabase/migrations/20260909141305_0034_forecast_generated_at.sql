-- 0034 `v1_stations_current` に `forecast_generated_at` を足す（W4 プラン §12 の 114）
--
-- **返している確率の起点は `generated_at` である。** `jobs/infer.py` は推論を回した時刻
-- （`now`）で標本を作り、水平 `h` は「その時刻から h 分後」を指す。`base_observed_at`
-- （台数の観測時刻）からでも、利用者が読んだ時刻からでもない。
--
-- PR A は `in_min` をそのまま水平として補間していたので、返していたのは
-- **「`generated_at` ＋ `in_min`」の確率**で、利用者の到着（読んだ時刻 ＋ `in_min`）
-- より予測行の年齢のぶん早い時刻を指していた。実測で中央 2.1 分・最大 5.3 分、
-- 確率にして 30 分先で 2.62% が表示の刻み（5%）を跨ぐ。
--
-- 直すには**読む側が行の年齢を知る必要がある**ので、`generated_at` を渡す。
-- **鮮度の判定は `base_observed_at` のまま**（0033）。2 つは別の役目である。
--
--   `base_observed_at` … その予測が**どの観測に基づくか**。古ければ出さない
--   `generated_at`     … 水平の**起点**。補間する位置を決める
--
-- 列は末尾に足す。`create or replace view` は既存の列の名前・型・順序を変えられない。

create or replace view public.v1_stations_current as
select l.system_id,
       l.station_id,
       a.name,
       a.lat,
       a.lon,
       a.capacity,
       -- -1 は「登録済みだが観測されていない」。0（本当に 0 台）と区別する
       nullif(l.bikes, -1) as bikes,
       nullif(l.docks, -1) as docks,
       case when l.flags < 0 then null else (l.flags & 1) > 0 end as is_installed,
       case when l.flags < 0 then null else (l.flags & 2) > 0 end as is_renting,
       case when l.flags < 0 then null else (l.flags & 4) > 0 end as is_returning,
       l.is_present,
       l.last_changed_at,
       -- ここから予測（0033）。**予測が無ければすべて NULL**
       f.horizons_min                as forecast_horizons_min,
       f.p_bike_x1000                as forecast_p_bike_x1000,
       f.p_dock_x1000                as forecast_p_dock_x1000,
       f.confidence                  as forecast_confidence,
       -- **鮮度はこれで測る。** 「いつ計算したか」ではなく「どの観測に基づくか」
       f.base_observed_at            as forecast_base_observed_at,
       f.model_version               as forecast_model_version,
       -- **水平の起点（0034）。** 補間する位置は in_min ＋（いま − これ）
       f.generated_at                as forecast_generated_at
  from public.station_status_latest l
  join public.systems sy
    on sy.system_id = l.system_id and sy.is_active
  left join public.station_attributes a
    on a.system_id = l.system_id
   and a.station_id = l.station_id
   and a.valid_to is null
  left join public.station_forecasts f
    on f.system_id = l.system_id
   and f.station_id = l.station_id
 where coalesce(a.geo_suspect, false) = false;

comment on view public.v1_stations_current is
  '公開 API 用。ポートの現在値と先回り予測。-1 は NULL に、flags は真偽値に開いてある。geo_suspect と停止中システムは除く。予測の鮮度は forecast_base_observed_at（FORECAST_STALE_AFTER_S）、水平の起点は forecast_generated_at で、読む側が判定する。';

grant select on public.v1_stations_current to anon;
grant select on public.v1_stations_current to service_role;

notify pgrst, 'reload schema';
