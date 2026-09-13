-- ────────────────────────────────────────────────────────────────
-- 0045 — ポートの「大きさ」を公開する（`capacity_est` / `capacity_days`）
-- ────────────────────────────────────────────────────────────────
-- **0035 が残した宿題を閉じる**（W4-09、W5 プラン §4 の W5-14）。あちらは `capacity` を
-- 「固定のラック数」に限り、**容量が動的な系統（ドコモの 5,837 ポート）は NULL** にした。
-- そのとき「大きさは W5 に `capacity_est` を**別の列として**足して答える」と書いた。
--
-- `capacity_est` は **前日までの 7 日の `max(bikes + docks)`**、`capacity_days` は
-- **その値に寄与した日数**（1〜7）である。足りないことを隠さないための組で、
-- `capacity_days` が 7 未満なら「まだその日数ぶんしか見ていない」と読む。
--
-- ## なぜ表を足すのか（**SQL で数え直さない**）
--
-- 値は **Storage の参照スナップショットにしか無かった**。`build_reference`（毎日
-- 05:00 JST）が毎時 Parquet から計算して `reference/date=…/stations.parquet` に書いて
-- いる。**`/v1`（Next.js）は Storage を読む経路を持たない**——持たせると生 JSON への
-- 入口が増える（CLAUDE.md §5）。
--
-- `status_snapshots` から SQL で数え直す案は採らない。7 日ぶんの配列を毎回開くと
-- **900 万行の unnest** になり、しかも**同じ量を 2 つの実装が持つ**ことになる
-- （W5-01 が閉じたのと同じ誤り）。**定義は
-- `features/reference_snapshot.py::daily_capacity_max` の 1 つに保ち、同じジョブが
-- DB にも写す**（W5 プラン §12 の 150）。
--
-- ## 書き手と読み手
--
--   * 書く … `build_reference`（サービスロール。1 日 1 回・約 2 万行の upsert）
--   * 読む … `v1_stations_current` 経由だけ。**匿名ロールにこの表を渡さない**
--
-- **止まっても静かには壊れない。** 7 日の最大なので 1 日欠けても値はほとんど動かず、
-- `as_of_date` にどの版かが残る。ジョブ自体は `monitored_jobs` が見張っている（0036）。
-- ────────────────────────────────────────────────────────────────

create table if not exists public.station_capacity_est (
  system_id     text        not null,
  station_id    text        not null,
  -- 前日までの 7 日の max(bikes + docks)。**観測が 1 度も無いポートは行を作らない**
  capacity_est  smallint    not null,
  -- その値に寄与した日数（1〜7）。**足りないことを隠さない**
  capacity_days smallint    not null,
  -- どの日の参照スナップショットから写したか（JST の暦日）
  as_of_date    date        not null,
  updated_at    timestamptz not null default now(),
  primary key (system_id, station_id),
  constraint station_capacity_est_station_fkey
    foreign key (system_id, station_id) references public.stations (system_id, station_id),
  -- 0 は「大きさ 0」ではなく「分からない」なので、行ごと作らない側で弾く
  constraint station_capacity_est_positive check (capacity_est > 0),
  constraint station_capacity_est_days_range check (capacity_days between 1 and 7)
);

comment on table public.station_capacity_est is
  'ポートの大きさの推定（前日までの 7 日の max(bikes + docks)）。build_reference が毎日 1 回写す。定義は features/reference_snapshot.py の daily_capacity_max ひとつ（W5-14）。';
comment on column public.station_capacity_est.capacity_est is
  '前日までの 7 日の max(bikes + docks)。固定ラック数（station_attributes.capacity）とは別物。';
comment on column public.station_capacity_est.capacity_days is
  'capacity_est に寄与した日数（1〜7）。7 未満は「まだその日数ぶんしか見ていない」。';
comment on column public.station_capacity_est.as_of_date is
  '写した参照スナップショットの JST 暦日。止まったときに何日ぶん古いかが読める。';

alter table public.station_capacity_est enable row level security;
revoke all on table public.station_capacity_est from anon, authenticated;
grant select, insert, update, delete on table public.station_capacity_est to service_role;

-- ────────────────────────────────────────────────────────────────
-- まとめて書く（`upsert_capacity_est`）
-- ────────────────────────────────────────────────────────────────
-- **表に直接 POST させない。** 書き込みは RPC を通す（`upsert_forecasts` /
-- `upsert_weather_hourly` と同じ形）。約 2 万行を 1 日 1 回、`build_reference` が送る。
--
-- **消さない。** 7 日の観測が 1 度も無くなったポートは `p_rows` に入らないが、古い行は
-- 残す——`as_of_date` に「いつの版か」が出るので、**消すより古さが読めるほうがよい**。

create or replace function public.upsert_capacity_est(p_rows jsonb)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_count integer;
begin
  insert into public.station_capacity_est as c
    (system_id, station_id, capacity_est, capacity_days, as_of_date, updated_at)
  select r.system_id, r.station_id, r.capacity_est, r.capacity_days, r.as_of_date, now()
    from jsonb_to_recordset(p_rows) as r(
      system_id text, station_id text,
      capacity_est smallint, capacity_days smallint, as_of_date date
    )
  on conflict (system_id, station_id) do update
     set capacity_est  = excluded.capacity_est,
         capacity_days = excluded.capacity_days,
         as_of_date    = excluded.as_of_date,
         updated_at    = excluded.updated_at;
  get diagnostics v_count = row_count;
  return v_count;
end;
$$;

comment on function public.upsert_capacity_est(jsonb) is
  'ポートの大きさの推定をまとめて UPSERT する。build_reference が毎日 1 回送る。台帳に無いポートは外部キーで弾く。';

revoke all on function public.upsert_capacity_est(jsonb) from public, anon, authenticated;
-- **本番は「新規オブジェクトの自動公開」が OFF**（W3 プラン §12 の 90）。明示的に付ける
grant execute on function public.upsert_capacity_est(jsonb) to service_role;

-- ────────────────────────────────────────────────────────────────
-- 公開ビューに 2 列足す
-- ────────────────────────────────────────────────────────────────
-- **`capacity` には混ぜない。** 「事業者が宣言したラック数」と「7 日の最大の実測」は
-- 別のものである（W5-14）。読む側が 2 つを見分けられる形で渡す。
--
-- **列が増えるだけ**なので、デプロイの順序はどちらでもよい（§12 の 112）。古いコードは
-- 並べた列だけを読む（`view-query.ts` の `STATION_COLUMNS`）ので、増えても壊れない。
--
-- **足す位置は末尾。** `create or replace view` は**既存の列の間に挿せない**
-- （`cannot change name of view column`）。`drop view` すれば好きな位置に置けるが、
-- **権限が落ちる**（`grant` を付け直すまで匿名が読めなくなる）ので、素直に末尾へ足す。
-- 読む側は列を名前で並べるので、順序は契約に入っていない（pgTAP の `columns_are` も
-- 集合で比べる）。

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
       f.generated_at                as forecast_generated_at,
       -- **実測からの大きさ**（0045）。どちらのシステムでも出る。まだ写していなければ NULL。
       -- 位置が末尾なのは `create or replace view` の制約（上の注記）
       ce.capacity_est,
       ce.capacity_days
  from public.station_status_latest l
  join public.systems sy
    on sy.system_id = l.system_id and sy.is_active
  left join public.station_attributes a
    on a.system_id = l.system_id
   and a.station_id = l.station_id
   and a.valid_to is null
  left join public.station_capacity_est ce
    on ce.system_id = l.system_id
   and ce.station_id = l.station_id
  left join public.station_forecasts f
    on f.system_id = l.system_id
   and f.station_id = l.station_id
 where coalesce(a.geo_suspect, false) = false;

comment on view public.v1_stations_current is
  '公開 API 用。ポートの現在値と先回り予測。-1 は NULL に、flags は真偽値に開いてある。capacity は固定のラック数を持つシステムだけ（動的な系統は NULL）で、実測からの大きさは capacity_est / capacity_days（0045）。geo_suspect と停止中システムは除く。予測の鮮度は forecast_base_observed_at、水平の起点は forecast_generated_at。';

grant select on public.v1_stations_current to anon;
grant select on public.v1_stations_current to service_role;

notify pgrst, 'reload schema';
