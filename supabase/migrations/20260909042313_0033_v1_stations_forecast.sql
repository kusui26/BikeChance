-- 0033 `v1_stations_current` に予測を足す（W4 プラン §6.1 の PR A）
--
-- 予測は 5 分毎に `station_forecasts` へ書けているが、**`/v1` が読んでいない**。
-- 公開経路が触れてよいのは `v1_` で始まるビューだけ（0018、pgTAP 0010）なので、
-- 見えるようにするにはここに足すしかない。
--
-- **1 本のビューに足す。別のビューを作らない。** 地図は「現在値と予測」を同時に要る。
-- 分けると 1 回の描画で 2 往復になり、クライアント側で結合することになる。予測は
-- `station_forecasts` の主キー `(system_id, station_id)` で引けるので、上限 1,000 件に
-- 対する入れ子ループで済む。
--
-- **`left join` にする。** 予測の無いポートがある：貸出も返却も止まっている（HELLO の
-- 2.75%・ドコモの 0.38%）、観測が `-1`、成果物に無い（実測 6 件）。**行は落とさない**：
-- 台数は出せるので、予測だけを NULL にする（属性の無いポートと同じ扱い。開発プラン §8.3）。
--
-- **鮮度の判定はここでやらない。** 「何分より古ければ出さないか」は `FORECAST_STALE_AFTER_S`
-- （`packages/shared/src/constants.ts`。900 秒）が持っており、**SQL に 900 を書くと正が
-- 2 つになる**。現在値の `stale` 判定も TypeScript 側（`freshness.ts`）にあって iOS と
-- 共有しているので、揃える。ビューは素直な射影に留め、**`base_observed_at` をそのまま
-- 渡して、切るかどうかは読む側が決める**。
--
-- 列は末尾に足す。`create or replace view` は**既存の列の名前・型・順序を変えられない**が、
-- 末尾への追加はできる。

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
       -- **鮮度はこれで測る。** 「いつ計算したか」（generated_at）ではなく
       -- 「どの観測に基づくか」。現在値の stale 判定と同じ物差しになる
       f.base_observed_at            as forecast_base_observed_at,
       f.model_version               as forecast_model_version
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
  '公開 API 用。ポートの現在値と先回り予測。-1 は NULL に、flags は真偽値に開いてある。geo_suspect と停止中システムは除く。予測の鮮度は読む側が forecast_base_observed_at で判定する（FORECAST_STALE_AFTER_S）。';

-- ────────────────────────────────────────────────────────────────
-- v1_feeds — 「いまどの版が配信しているか」
-- ────────────────────────────────────────────────────────────────
-- `/v1/meta` は `model_version` を返す約束になっている（開発プラン §8.3）が、W2 から
-- **ずっと null** だった。返す材料が無かったためである。
--
-- **`station_forecasts` からは取らない。** あちらは 1 ポート 1 行なので、版を知るのに
-- 2 万行を走査することになる。`inference_log` なら 1 システムあたり 1 行を後ろから
-- 引くだけで済む（`id` の降順に見て、最初に見つかった `ok` が最新）。
--
-- **`ok` に限る。** 失敗した回の版を「配信中の版」として出さない。
create or replace view public.v1_feeds as
select s.system_id,
       s.display_name,
       s.expected_cadence_s,
       s.poll_interval_s,
       s.capacity_is_dynamic,
       f.last_observed_at,
       fc.model_version as forecast_model_version,
       fc.generated_at  as forecast_generated_at
  from public.systems s
  join public.feed_state f on f.system_id = s.system_id
  left join lateral (
    select il.model_version, il.generated_at
      from public.inference_log il
     where il.system_id = s.system_id and il.status = 'ok'
     order by il.id desc
     limit 1
  ) fc on true
 where s.is_active;

comment on view public.v1_feeds is
  '公開 API 用。フィードの鮮度（last_observed_at）と、鮮度の判定に要る周期、いま配信している予測の版を返す。稼働中のシステムのみ。';

-- `create or replace view` は権限を保つが、**毎回明示的に書く**のが本リポジトリの約束
-- （W3 プラン §12 の 90）。見張りは pgTAP 0010。
grant select on public.v1_feeds to anon;
grant select on public.v1_feeds to service_role;
grant select on public.v1_stations_current to anon;
grant select on public.v1_stations_current to service_role;

notify pgrst, 'reload schema';
