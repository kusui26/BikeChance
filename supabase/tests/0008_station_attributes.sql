-- pgTAP: ポート属性の日次同期（W1 プラン §6.9、PR F）
--
-- SCD2 の要は「変わったときだけ行が増える」こと。増えすぎれば容量が壊れ、
-- 増えなければ履歴が失われる。境界を 1 つずつ確かめる。

begin;
select plan(41);

-- テストはトランザクション内で完結し rollback する
delete from public.station_status_latest;
delete from public.status_snapshots;
delete from public.station_attributes;
delete from public.stations;
delete from public.feed_fetch_log;
delete from public.job_runs;

-- 入力を 1 件つくるヘルパ
create function pg_temp.row_of(
  p_id text, p_name text default 'ポート', p_lat double precision default 35.1,
  p_lon double precision default 139.1, p_capacity int default 6,
  p_geo_suspect boolean default false
) returns jsonb language sql immutable as $$
  select jsonb_build_object(
    'station_id', p_id, 'name', p_name, 'lat', p_lat, 'lon', p_lon,
    'capacity', p_capacity, 'geo_suspect', p_geo_suspect,
    'raw', jsonb_build_object('station_id', p_id, 'name', p_name));
$$;

create function pg_temp.sync(p_at text, p_rows jsonb) returns jsonb language sql as $$
  select public.upsert_station_attributes('hellocycling', p_at::timestamptz, p_rows);
$$;

create function pg_temp.current_of(p_id text) returns public.station_attributes language sql as $$
  select a.* from public.station_attributes a
   where a.system_id = 'hellocycling' and a.station_id = p_id and a.valid_to is null;
$$;

-- ────────────────────────────────────────────────────────────────
-- 初回：全件が新規
-- ────────────────────────────────────────────────────────────────
select is(
  (pg_temp.sync('2026-09-06T00:00:00Z',
     jsonb_build_array(pg_temp.row_of('a'), pg_temp.row_of('b')))->>'n_new_stations')::int,
  2, '初回は全件が新規ポート'
);
select is((select count(*)::int from public.station_attributes), 2, '属性の行が 2 本');
select is((select count(*)::int from public.stations), 2, '台帳にも 2 件登録された');
select is(
  (select count(distinct idx)::int from public.stations), 2, 'idx が重複していない'
);
select is((select min(idx)::int from public.stations), 0, 'idx は 0 起点');
select ok(
  (select bool_and(valid_to is null) from public.station_attributes), '初回はすべて有効行'
);

-- ────────────────────────────────────────────────────────────────
-- 同じ入力の 2 回目：何も増えない
-- ────────────────────────────────────────────────────────────────
select is(
  (pg_temp.sync('2026-09-07T00:00:00Z',
     jsonb_build_array(pg_temp.row_of('a'), pg_temp.row_of('b')))->>'n_changed')::int,
  0, '同じ入力なら変更 0'
);
select is((select count(*)::int from public.station_attributes), 2, '行は増えない');
select is(
  (pg_temp.sync('2026-09-07T00:00:00Z',
     jsonb_build_array(pg_temp.row_of('a'), pg_temp.row_of('b')))->>'n_unchanged')::int,
  2, '2 件とも「変わっていない」と数える'
);

-- ────────────────────────────────────────────────────────────────
-- 値が変わったら旧行を閉じて新行を足す
-- ────────────────────────────────────────────────────────────────
select is(
  (pg_temp.sync('2026-09-08T00:00:00Z',
     jsonb_build_array(pg_temp.row_of('a', 'ポート', 35.9), pg_temp.row_of('b')))->>'n_changed')::int,
  1, '座標が変わった 1 件だけを検知する'
);
select is((select count(*)::int from public.station_attributes), 3, '行が 1 本だけ増える');
select is(
  (select count(*)::int from public.station_attributes where station_id = 'a' and valid_to is not null),
  1, '旧行が閉じている'
);
select is(
  (select valid_to from public.station_attributes where station_id = 'a' and valid_to is not null),
  '2026-09-08T00:00:00Z'::timestamptz, '閉じた時刻は取得時刻'
);
select is((pg_temp.current_of('a')).lat, 35.9::double precision, '有効行は新しい値');
select is(
  (select count(*)::int from public.station_attributes where station_id = 'b'),
  1, '変わっていないポートの行は増えない'
);

-- 名前・容量の変更も検知する
select is(
  (pg_temp.sync('2026-09-09T00:00:00Z',
     jsonb_build_array(pg_temp.row_of('a', '改名', 35.9), pg_temp.row_of('b')))->>'n_changed')::int,
  1, '名前の変更を検知する'
);
select is(
  (pg_temp.sync('2026-09-10T00:00:00Z',
     jsonb_build_array(pg_temp.row_of('a', '改名', 35.9, 139.1, 9), pg_temp.row_of('b')))->>'n_changed')::int,
  1, '容量の変更を検知する'
);
select is((pg_temp.current_of('a')).capacity::int, 9, '容量が反映される');

-- capacity は NULL になり得る。NULL 同士を「変わった」と数えない
select is(
  (pg_temp.sync('2026-09-11T00:00:00Z',
     jsonb_build_array(pg_temp.row_of('a', '改名', 35.9, 139.1, null),
                       pg_temp.row_of('b')))->>'n_changed')::int,
  1, '値から NULL への変化は検知する'
);
select is(
  (pg_temp.sync('2026-09-12T00:00:00Z',
     jsonb_build_array(pg_temp.row_of('a', '改名', 35.9, 139.1, null),
                       pg_temp.row_of('b')))->>'n_changed')::int,
  0, 'NULL 同士は「変わっていない」（is distinct from を使っている）'
);

-- ────────────────────────────────────────────────────────────────
-- 容量が動的なシステムでは、容量の変化で版を作らない（§5.7 の 45）
-- ────────────────────────────────────────────────────────────────
-- ドコモの capacity は `bikes + docks` の動的値（開発プラン §3.6）。比較に入れると
-- 属性の履歴が容量の変更ログになる（実測で 2 分の間に 154 版、全部が容量のみの変更）
select ok(
  (select capacity_is_dynamic from public.systems where system_id = 'docomo-cycle'),
  'ドコモは capacity_is_dynamic'
);
select ok(
  not (select capacity_is_dynamic from public.systems where system_id = 'hellocycling'),
  'HELLO は capacity_is_dynamic ではない'
);
select is(
  (public.upsert_station_attributes('docomo-cycle', '2026-09-06T00:00:00Z',
     jsonb_build_array(pg_temp.row_of('d1', 'ドコモのポート', 35.1, 139.1, 6)))->>'n_new_stations')::int,
  1, 'ドコモに 1 件登録する'
);
select is(
  (public.upsert_station_attributes('docomo-cycle', '2026-09-07T00:00:00Z',
     jsonb_build_array(pg_temp.row_of('d1', 'ドコモのポート', 35.1, 139.1, 20)))->>'n_changed')::int,
  0, '容量だけが変わっても版を作らない'
);
select is(
  (select count(*)::int from public.station_attributes where system_id = 'docomo-cycle'),
  1, '行が増えていない'
);
select is(
  (public.upsert_station_attributes('docomo-cycle', '2026-09-08T00:00:00Z',
     jsonb_build_array(pg_temp.row_of('d1', '改名した', 35.1, 139.1, 20)))->>'n_changed')::int,
  1, '名前が変われば版を作る（容量を外しても他の変化は拾う）'
);

-- ────────────────────────────────────────────────────────────────
-- 入力から消えても閉じない（フィードの一時的な欠落で属性を失わない）
-- ────────────────────────────────────────────────────────────────
select is(
  (pg_temp.sync('2026-09-13T00:00:00Z', jsonb_build_array(pg_temp.row_of('a', '改名', 35.9, 139.1, null)))
   ->>'n_input')::int,
  1, 'b を含まない入力'
);
select ok(
  (select valid_to is null from public.station_attributes where station_id = 'b'),
  '入力から消えても有効行は閉じない'
);

-- ────────────────────────────────────────────────────────────────
-- 有効行は 1 ポートにつき 1 本だけ（部分ユニーク索引）
-- ────────────────────────────────────────────────────────────────
select is(
  (select count(*)::int from public.station_attributes where system_id = 'hellocycling' and valid_to is null),
  2, '有効行はポートごとに 1 本だけ'
);
select throws_ok(
  $$insert into public.station_attributes (system_id, station_id, valid_from, name, lat, lon, raw)
    values ('hellocycling', 'a', '2026-10-01T00:00:00Z', 'x', 1, 1, '{}'::jsonb)$$,
  '23505', null, '有効行を 2 本作ろうとすると弾かれる'
);

-- ────────────────────────────────────────────────────────────────
-- geo_suspect（開発プラン §14）
-- ────────────────────────────────────────────────────────────────
select is(
  (pg_temp.sync('2026-09-14T00:00:00Z',
     jsonb_build_array(pg_temp.row_of('z', 'こわれた座標', 35.5, 39.5, 3, true)))->>'n_geo_suspect')::int,
  1, '範囲外の座標を数える'
);
select ok((pg_temp.current_of('z')).geo_suspect, 'geo_suspect が保存される');
select ok(not (pg_temp.current_of('b')).geo_suspect, '範囲内のポートは false');

-- ────────────────────────────────────────────────────────────────
-- 過去の時刻での取り込みは、黙って捨てずに数える
-- ────────────────────────────────────────────────────────────────
select is(
  (pg_temp.sync('2026-01-01T00:00:00Z',
     jsonb_build_array(pg_temp.row_of('b', 'ずっと昔の名前')))->>'n_skipped_older')::int,
  1, '有効行より古い取り込みは n_skipped_older に出る'
);
select is(
  (select name from public.station_attributes where station_id = 'b' and valid_to is null),
  'ポート', '古い取り込みで有効行は書き換わらない'
);

-- ────────────────────────────────────────────────────────────────
-- 契約違反は黙って通さない
-- ────────────────────────────────────────────────────────────────
select throws_ok(
  $$select public.upsert_station_attributes('存在しない', now(), '[]'::jsonb)$$,
  '22023', null, '未知のシステムは弾く'
);
select throws_ok(
  $$select public.upsert_station_attributes('hellocycling', now(),
      jsonb_build_array(pg_temp.row_of('dup'), pg_temp.row_of('dup')))$$,
  '22023', null, '重複した station_id は弾く'
);
select throws_ok(
  $$select public.upsert_station_attributes('hellocycling', now(),
      '[{"station_id":"x","name":"n","lat":1,"lon":1}]'::jsonb)$$,
  '22023', null, 'raw が無い行は弾く'
);
select is(
  (public.upsert_station_attributes('hellocycling', now(), '[]'::jsonb)->>'n_input')::int,
  0, '空の入力でも壊れない'
);

-- ────────────────────────────────────────────────────────────────
-- 権限
-- ────────────────────────────────────────────────────────────────
select ok(
  not has_function_privilege('anon', 'public.upsert_station_attributes(text, timestamptz, jsonb)', 'execute'),
  '匿名からは実行できない'
);
select ok(
  has_function_privilege('service_role', 'public.upsert_station_attributes(text, timestamptz, jsonb)', 'execute'),
  'service_role からは実行できる'
);

select * from finish();
rollback;
