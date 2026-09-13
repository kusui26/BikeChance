-- pgTAP: 低ズームの格子集約（マイグレーション 0049、W5 プラン §6.5 の PR E）
--
-- ここで固定したい契約は 6 つ。
--   1. **セルの境界は `quantizeBbox` と同じ格子**（`BBOX_QUANTUM_DEG` の倍数）
--   2. **確率は水平ごとの最大。平均ではない**（W5-12。1 台でもあれば答えは「借りられる」）
--   3. **台数は観測のあるポートだけ**を足す（`is_present` でない行の値は日付が付かない）
--   4. **添字のずれを作らない**——水平の並びが違う行は確率の集約に入れない
--   5. **鮮度の閾値は呼ぶ側が渡す**（規則の正は `packages/shared`。ここは比較だけ）
--   6. **匿名からは呼べない**（`/v1` はサービスロールで読む）
--
-- 前提条件はこのテストが自分で作る。**行単位の検査は必ず `system_id` で絞る。**

begin;
select plan(26);

-- ────────────────────────────────────────────────────────────────
-- 権限
-- ────────────────────────────────────────────────────────────────
select has_function('public', 'v1_station_cells', 'v1_station_cells がある');
select ok(
  not has_function_privilege(
    'anon',
    'public.v1_station_cells(numeric,numeric,numeric,numeric,numeric,text,timestamptz,smallint[])',
    'execute'),
  '匿名は呼べない（/v1 はサービスロールで読む）'
);
select ok(
  has_function_privilege(
    'service_role',
    'public.v1_station_cells(numeric,numeric,numeric,numeric,numeric,text,timestamptz,smallint[])',
    'execute'),
  'サービスロールは呼べる'
);
select is(
  (select p.prosecdef from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'v1_station_cells'),
  true, 'security definer'
);

-- ────────────────────────────────────────────────────────────────
-- 前提データ
-- ────────────────────────────────────────────────────────────────
insert into public.systems
  (system_id, display_name, operator_name, gbfs_base_url, expected_cadence_s, poll_interval_s,
   lock_key, is_active, capacity_is_dynamic)
values
  ('t-cell-a', '稼働',   '事業者', 'https://example.test/gbfs', 300, 60, 9501, true,  false),
  ('t-cell-b', '別系統', '事業者', 'https://example.test/gbfs', 300, 60, 9502, true,  false),
  ('t-cell-x', '停止中', '事業者', 'https://example.test/gbfs', 300, 60, 9503, false, false);

-- **同じセル（139.76〜139.77, 35.68〜35.69）に 3 つ、隣のセルに 1 つ。**
--   pair-1 / pair-2 … 予測あり。**水平ごとに最大を取る相手が入れ替わる**
--   absent          … フィードに現れない（台数を足さない・stale になる）
--   north           … 1 つ上のセル
-- そのほか：座標なし・geo_suspect・停止中の系統・別系統
insert into public.stations (system_id, station_id, idx) values
  ('t-cell-a', 'pair-1',   0),
  ('t-cell-a', 'pair-2',   1),
  ('t-cell-a', 'absent',   2),
  ('t-cell-a', 'north',    3),
  ('t-cell-a', 'nowhere',  4),
  ('t-cell-a', 'suspect',  5),
  ('t-cell-a', 'oldcast',  6),
  ('t-cell-a', 'offgrid',  7),
  ('t-cell-b', 'other',    0),
  ('t-cell-x', 'halted',   0);

insert into public.station_attributes
  (system_id, station_id, name, lat, lon, capacity, valid_from, raw, geo_suspect)
values
  ('t-cell-a', 'pair-1',  'P1',   35.6812, 139.7671, 10, now(), '{}'::jsonb, false),
  ('t-cell-a', 'pair-2',  'P2',   35.6815, 139.7675, 10, now(), '{}'::jsonb, false),
  ('t-cell-a', 'absent',  'AB',   35.6819, 139.7679, 10, now(), '{}'::jsonb, false),
  ('t-cell-a', 'north',   'N',    35.6901, 139.7671, 10, now(), '{}'::jsonb, false),
  ('t-cell-a', 'nowhere', 'NO',   null,    null,     10, now(), '{}'::jsonb, false),
  ('t-cell-a', 'suspect', 'SU',   35.6813, 139.7672, 10, now(), '{}'::jsonb, true),
  ('t-cell-a', 'oldcast', 'OLD',  35.6814, 139.7673, 10, now(), '{}'::jsonb, false),
  ('t-cell-a', 'offgrid', 'OFF',  35.6816, 139.7674, 10, now(), '{}'::jsonb, false),
  ('t-cell-b', 'other',   'OT',   35.6817, 139.7676, 10, now(), '{}'::jsonb, false),
  ('t-cell-x', 'halted',  'HA',   35.6818, 139.7677, 10, now(), '{}'::jsonb, false);

-- flags 7 ＝ 設置・貸出・返却すべて可
insert into public.station_status_latest
  (system_id, station_id, bikes, docks, flags, is_present, last_changed_at)
values
  ('t-cell-a', 'pair-1',  3,  7, 7, true,  now()),
  ('t-cell-a', 'pair-2',  5,  5, 7, true,  now()),
  ('t-cell-a', 'absent',  99, 1, 7, false, now()),
  ('t-cell-a', 'north',   2,  8, 7, true,  now()),
  ('t-cell-a', 'nowhere', 1,  9, 7, true,  now()),
  ('t-cell-a', 'suspect', 4,  6, 7, true,  now()),
  ('t-cell-a', 'oldcast', 1,  9, 7, true,  now()),
  ('t-cell-a', 'offgrid', 1,  9, 7, true,  now()),
  ('t-cell-b', 'other',   8,  2, 7, true,  now()),
  ('t-cell-x', 'halted',  6,  4, 7, true,  now());

-- **水平ごとに最大を取る相手が入れ替わる。**
--   h=5  … pair-1 が 900、pair-2 が 100 → 最大 900
--   h=10 … pair-1 が 100、pair-2 が 800 → 最大 800
-- 平均なら 500 と 450 になる。**最大でなければここで落ちる**
insert into public.station_forecasts
  (system_id, station_id, horizons_min, p_bike_x1000, p_dock_x1000, confidence,
   base_observed_at, model_version, generated_at)
values
  ('t-cell-a', 'pair-1', array[5,10]::smallint[], array[900,100]::smallint[],
   array[100,900]::smallint[], 3, now(), 'm', now() - interval '4 minutes'),
  ('t-cell-a', 'pair-2', array[5,10]::smallint[], array[100,800]::smallint[],
   array[800,200]::smallint[], 1, now(), 'm', now() - interval '1 minute'),
  -- **鮮度の閾値より古い。** 呼ぶ側が切る
  ('t-cell-a', 'oldcast', array[5,10]::smallint[], array[999,999]::smallint[],
   array[999,999]::smallint[], 3, now() - interval '30 minutes', 'm', now()),
  -- **水平の並びが違う。** 添字がずれるので入れない
  ('t-cell-a', 'offgrid', array[5,60]::smallint[], array[999,999]::smallint[],
   array[999,999]::smallint[], 3, now(), 'm', now()),
  ('t-cell-a', 'north', array[5,10]::smallint[], array[400,400]::smallint[],
   array[400,400]::smallint[], 2, now(), 'm', now());

-- **返り値の形を書き下す。** `returns table` の関数は名前付きの複合型を作らないので、
-- `setof public.v1_station_cells` とは書けない
create function pg_temp.cells(p_system text default 't-cell-a', p_deg numeric default 0.01)
returns table (
  west double precision, south double precision, east double precision, north double precision,
  n_stations integer, bikes integer, docks integer, stale boolean,
  p_bike_x1000 smallint[], p_dock_x1000 smallint[], confidence smallint,
  generated_at timestamptz, n_forecast integer
) language sql as $$
  select * from public.v1_station_cells(
    139.76, 35.67, 139.78, 35.70, p_deg, p_system,
    now() - interval '15 minutes', array[5,10]::smallint[]);
$$;

-- ────────────────────────────────────────────────────────────────
-- セルの境界と、まとまり方
-- ────────────────────────────────────────────────────────────────
select is((select count(*)::int from pg_temp.cells()), 2, '2 つのセルにまとまる（南と北）');

select is(
  (select west || ',' || south || ',' || east || ',' || north
     from pg_temp.cells() where south = 35.68),
  '139.76,35.68,139.77,35.69',
  '**境界は 0.01 度の倍数**（quantizeBbox と同じ格子）'
);

-- **並べ替えずに集める。** `array_agg(... order by south)` と書くと検査が自分で並べ直し、
-- 関数の並びを 1 つも確かめないことになる（W5 §12 の 154 と同じ形）
select is(
  (select array_agg(south) from pg_temp.cells()),
  array[35.68, 35.69]::double precision[],
  '**並びは南から北へ**（同じ要求が同じ応答になる）'
);

-- ────────────────────────────────────────────────────────────────
-- 数える範囲
-- ────────────────────────────────────────────────────────────────
select is(
  (select n_stations from pg_temp.cells() where south = 35.68),
  5, '**予測の無いポートも数える**（pair-1・pair-2・absent・oldcast・offgrid の 5 件）'
);

select is(
  (select bikes from pg_temp.cells() where south = 35.68),
  3 + 5 + 1 + 1, '**台数は観測のあるポートだけ**（absent の 99 を足さない）'
);

select is(
  (select docks from pg_temp.cells() where south = 35.68),
  7 + 5 + 9 + 9, '空き枠も同じ規則で足す'
);

select is(
  (select stale from pg_temp.cells() where south = 35.68),
  true, '**1 つでも「いつのものか言えない」ポートがあれば stale**'
);

select is(
  (select stale from pg_temp.cells() where south = 35.69),
  false, '全部そろっていれば stale ではない'
);

-- ────────────────────────────────────────────────────────────────
-- 落ちるもの
-- ────────────────────────────────────────────────────────────────
select is(
  (select count(*)::int from pg_temp.cells('t-cell-b')), 1, '系統で絞れる'
);
select is(
  (select n_stations from pg_temp.cells('t-cell-b') where south = 35.68),
  1, '別系統のポートは混ざらない'
);
select is(
  (select count(*)::int from pg_temp.cells('t-cell-x')), 0, '停止した系統は出ない'
);
select is(
  (select sum(n_stations)::int from pg_temp.cells(null)),
  7, '系統を指定しなければ両系統ぶん（座標なし・geo_suspect・停止中は入らない）'
);
select is(
  (select count(*)::int from public.v1_station_cells(
     139.90, 35.90, 139.92, 35.92, 0.01, 't-cell-a', null, null)),
  0, 'bbox の外は入らない'
);

-- ────────────────────────────────────────────────────────────────
-- 確率は「水平ごとの最大」
-- ────────────────────────────────────────────────────────────────
select is(
  (select p_bike_x1000 from pg_temp.cells() where south = 35.68),
  array[900, 800]::smallint[],
  '**水平ごとの最大**（平均なら 500・450 になる。W5-12）'
);
select is(
  (select p_dock_x1000 from pg_temp.cells() where south = 35.68),
  array[800, 900]::smallint[],
  '返せる確率も同じ規則（相手が入れ替わる）'
);
select is(
  (select confidence from pg_temp.cells() where south = 35.68),
  3::smallint, '**confidence はセルの最大**（値も最大なので揃える）'
);
select is(
  (select n_forecast from pg_temp.cells() where south = 35.68),
  2, '**寄与したのは 2 件**（古い予測と、並びの違う予測は入らない）'
);
select ok(
  (select generated_at from pg_temp.cells() where south = 35.68) < now() - interval '3 minutes',
  '**generated_at はセルの最小**（古いほう。確率を大きく見せない側に倒れる）'
);

-- ────────────────────────────────────────────────────────────────
-- 予測が 1 つも無いセル
-- ────────────────────────────────────────────────────────────────
select is(
  (select n_forecast from public.v1_station_cells(
     139.76, 35.67, 139.78, 35.70, 0.01, 't-cell-b', now() - interval '15 minutes',
     array[5,10]::smallint[])),
  0, '予測の無いセルは n_forecast = 0'
);
select ok(
  (select p_bike_x1000 is null from public.v1_station_cells(
     139.76, 35.67, 139.78, 35.70, 0.01, 't-cell-b', now() - interval '15 minutes',
     array[5,10]::smallint[])),
  '予測の無いセルは曲線が null（0 を作らない）'
);

-- ────────────────────────────────────────────────────────────────
-- 粗い刻み
-- ────────────────────────────────────────────────────────────────
select is(
  (select count(*)::int from pg_temp.cells('t-cell-a', 0.05)),
  1, '**0.05 度なら 1 つにまとまる**（南北のセルが同じ格子に入る）'
);
select is(
  (select west || ',' || south from pg_temp.cells('t-cell-a', 0.05)),
  '139.75,35.65', '粗い刻みでも境界は倍数の上'
);

select * from finish();
rollback;
