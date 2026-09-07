-- pgTAP: 天気アーカイブの DB 側（W2 プラン §5.3、PR A）
--
-- 気象格子は「同じ格子のポートは同じ予報になる」ことを利用して要求数を減らす仕掛け。
-- 丸めが狂うと、要求する地点が増えたり（無駄）減ったり（欠測）する。

begin;
select plan(14);

delete from public.station_status_latest;
delete from public.status_snapshots;
delete from public.station_attributes;
delete from public.stations;

-- ポートを 1 件つくるヘルパ（台帳 → 属性の順。FK があるため）
create function pg_temp.put(
  p_id text, p_lat double precision, p_lon double precision,
  p_geo_suspect boolean default false, p_system text default 'hellocycling'
) returns void language sql as $$
  insert into public.stations (system_id, station_id, idx)
  values (p_system, p_id,
          coalesce((select max(idx) + 1 from public.stations where system_id = p_system), 0));
  insert into public.station_attributes
    (system_id, station_id, valid_from, name, lat, lon, geo_suspect, raw)
  values (p_system, p_id, now(), p_id, p_lat, p_lon, p_geo_suspect, '{}'::jsonb);
$$;

-- 刻みは呼び出し側の定数を渡す契約（W2 プラン §9.1）。
-- 関数は TABLE を返すので、名前付きの合成型では受けられない。列を明示する
create function pg_temp.cells()
returns table (lat double precision, lon double precision, n_ports integer)
language sql as $$
  select * from public.weather_grid_cells(0.05, 0.0625);
$$;

-- ────────────────────────────────────────────────────────────────
-- 丸め
-- ────────────────────────────────────────────────────────────────
select pg_temp.put('a', 35.681, 139.767);
select is((select count(*)::int from pg_temp.cells()), 1, '1 ポートなら 1 格子');
select is(
  (select round(lat::numeric, 4) from pg_temp.cells()), 35.7000::numeric,
  '緯度は 0.05 刻みに丸める（35.681 → 35.7。Open-Meteo の実測と一致）'
);
select is(
  (select round(lon::numeric, 4) from pg_temp.cells()), 139.7500::numeric,
  '経度は 0.0625 刻みに丸める（139.767 → 139.75。同上）'
);

-- 同じ格子に落ちる 2 つ目のポート
select pg_temp.put('b', 35.69, 139.76);
select is((select count(*)::int from pg_temp.cells()), 1, '同じ格子のポートはまとめる');
select is((select n_ports from pg_temp.cells()), 2, 'ポート数を数える');

-- 別の格子
select pg_temp.put('c', 34.702, 135.495);
select is((select count(*)::int from pg_temp.cells()), 2, '離れたポートは別の格子');
select is(
  (select round(lat::numeric, 4) from pg_temp.cells() where lat < 35), 34.7000::numeric,
  '大阪も同じ規則で丸まる（34.702 → 34.7）'
);

-- ────────────────────────────────────────────────────────────────
-- 除外
-- ────────────────────────────────────────────────────────────────
-- 壊れた座標を予報の取得先にしない（開発プラン §14。ドコモの 4826 が該当）
select pg_temp.put('broken', 35.565, 39.553, true);
select is((select count(*)::int from pg_temp.cells()), 2, 'geo_suspect の格子は作らない');

-- 閉じた属性行は見ない（現在有効な座標だけを使う）。
-- `now()` はトランザクション内で固定されるので、そのまま入れると
-- valid_to > valid_from の制約に触れる（W1 プラン §5 の 35）
update public.station_attributes set valid_to = now() + interval '1 second' where station_id = 'c';
select is((select count(*)::int from pg_temp.cells()), 1, '閉じた行は無視する');
update public.station_attributes set valid_to = null where station_id = 'c';

-- システムをまたいで同じ格子ならまとめる（天気は事業者に依らない）
select pg_temp.put('d', 35.6805, 139.7665, false, 'docomo-cycle');
select is((select count(*)::int from pg_temp.cells()), 2, 'システムが違っても同じ格子はまとめる');
select is(
  (select n_ports from pg_temp.cells() where lat > 35.6 and lat < 35.8), 3,
  '両システムのポートを合わせて数える'
);

-- ────────────────────────────────────────────────────────────────
-- 刻みは引数で決まる（値の出どころを TypeScript の定数 1 箇所にする）
-- ────────────────────────────────────────────────────────────────
select is(
  (select count(*)::int from public.weather_grid_cells(1.0, 1.0)), 2,
  '刻みを粗くすると格子が減る'
);

-- ────────────────────────────────────────────────────────────────
-- 権限
-- ────────────────────────────────────────────────────────────────
select ok(
  not has_function_privilege('anon', 'public.weather_grid_cells(double precision, double precision)', 'execute'),
  '匿名からは実行できない'
);
select ok(
  has_function_privilege('service_role', 'public.weather_grid_cells(double precision, double precision)', 'execute'),
  'service_role からは実行できる'
);

select * from finish();
rollback;
