-- 0015 天気予報の生アーカイブ（W2 プラン §5.3、PR A）
--
-- 学習で使うのは「`t` 時点で入手できた**予報**」で、これは過去に遡って観測できない
-- 唯一の入力である（開発プラン §6.7、R16）。ODPT のデータは生 JSON からいくらでも
-- 作り直せるが、予報だけは違う。**保存だけを先に始め、解釈は W4 で行う。**
--
-- 実測（2026-09-07）：最新の予報と 1 日前の予報は降雨フラグが 15% 食い違う。
-- 過去 API のどの近似にも同規模の誤差があり、ライブのアーカイブだけが厳密に正しい。

-- ────────────────────────────────────────────────────────────────
-- weather-raw バケット
-- ────────────────────────────────────────────────────────────────
-- `supabase db diff` は Storage バケットを検出しないため手書きで管理する
-- （W1 プラン §4.3 の 17）。0001 と同じ形にしてある。
do $$
begin
  if not exists (
    select 1 from information_schema.schemata where schema_name = 'storage'
  ) then
    raise notice 'storage スキーマが無いためバケット作成をスキップした';
    return;
  end if;

  insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
  values (
    'weather-raw',
    'weather-raw',
    false,               -- 非公開。読み書きはサービスロールのみ
    52428800,            -- 50 MiB。実測は 100 地点 48 時間で 214 KB（gzip 前）
    array['application/gzip']
  )
  on conflict (id) do nothing;
end
$$;

-- ────────────────────────────────────────────────────────────────
-- weather_grid_cells — ポートが分布する気象格子を返す
-- ────────────────────────────────────────────────────────────────
-- jma_msm は約 5 km の格子（緯度 0.05°・経度 0.0625°）で、要求した座標は格子に
-- 丸められて返る（実測：35.681 → 35.7、139.767 → 139.75）。同じ格子のポートは
-- 同じ予報になるので、格子ごとに 1 回だけ取ればよい。
--
-- **刻みは引数で受け取る。** 値の出どころを `packages/shared` の定数 1 箇所にして、
-- SQL と TypeScript に二重化しないため（W1 プラン §5.7 の geo_suspect と同じ方針）。
--
-- 実測（2026-09-07）：HELLO 463・ドコモ 218、重複を除いて **595 格子**。
-- 開発プラン §6.4 の「約 50 クラスタ」は実態より 1 桁少ない見積りだった。
create or replace function public.weather_grid_cells(
  p_lat_step double precision,
  p_lon_step double precision
)
returns table (lat double precision, lon double precision, n_ports integer)
language sql
stable
security definer
set search_path = ''
as $$
  select round(a.lat / p_lat_step) * p_lat_step as lat,
         round(a.lon / p_lon_step) * p_lon_step as lon,
         count(*)::integer                      as n_ports
    from public.station_attributes a
   where a.valid_to is null
     -- 壊れた座標を予報の取得先にしない（開発プラン §14。ドコモの 4826）
     and not a.geo_suspect
   group by 1, 2
   order by 1, 2;
$$;

comment on function public.weather_grid_cells(double precision, double precision) is
  'ポートが分布する気象格子（緯度・経度・ポート数）。刻みは呼び出し側の定数を渡す。geo_suspect は除く。';

revoke all on function public.weather_grid_cells(double precision, double precision)
  from public, anon, authenticated;
grant execute on function public.weather_grid_cells(double precision, double precision) to service_role;

notify pgrst, 'reload schema';
