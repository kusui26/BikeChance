-- 0035 `capacity` は「固定のラック数」を意味する列にする（W4 プラン §12 の 115、W4-09）
--
-- ドコモの `capacity` は**日次同期の瞬間の `bikes + docks` が凍結された値**である
-- （データ辞書 §4.3。同期の瞬間ですら一致するのは 49.7%）。ラック数ではないので、
-- **そのまま渡すと読む側が誤読する**。実測（2026-09-09）で、いま台数のほうが多いポートが
-- **628 件（ドコモの 10.8%）**あり、「容量 5・借りられる 12」という矛盾が画面に出ていた。
--
-- ビューは `-1` を NULL に、`flags` を真偽値に開いて**内部の約束を外に出さない**設計に
-- してある（データ辞書 §5.1）。`capacity` だけが素通ししていたので、同じ場所で塞ぐ。
-- **iOS だけを直すと、W10 のスマホ Web が同じ誤りを繰り返す。**
--
-- **NULL は「分からない」**という既存の意味そのままである（属性が未取得のポート・
-- 未観測の台数と同じ）。**作り話をしない**：`bikes + docks` を「容量」として返す案は、
-- 上に並んでいる 2 つの数を足しただけで「このポートの大きさ」には答えていない。
-- 大きさは W5 に `capacity_est`（前日までの 7 日の `max(bikes + docks)`。参照スナップ
-- ショットに既に在る）を**別の列として**足して答える。
--
-- **列は増えも減りもしない**ので、デプロイの順序はどちらでもよい（§12 の 112）。
-- 古いコードは NULL を「未取得」として描くだけで壊れない。

create or replace view public.v1_stations_current as
select l.system_id,
       l.station_id,
       a.name,
       a.lat,
       a.lon,
       -- **固定のラック数を持つシステムだけ返す**（0035）。動的な系統は NULL
       case when sy.capacity_is_dynamic then null else a.capacity end as capacity,
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
  '公開 API 用。ポートの現在値と先回り予測。-1 は NULL に、flags は真偽値に開いてある。capacity は固定のラック数を持つシステムだけ（動的な系統は NULL）。geo_suspect と停止中システムは除く。予測の鮮度は forecast_base_observed_at、水平の起点は forecast_generated_at。';

grant select on public.v1_stations_current to anon;
grant select on public.v1_stations_current to service_role;

notify pgrst, 'reload schema';
