-- pgTAP: ポートの地理属性と近傍リスト（W3 プラン §5.7、マイグレーション 0025）
--
-- ここで固定したい契約は 4 つ。
--   * **住所の解析に正規表現を使わない**（一覧への前方一致・最長一致）。実データにある
--     罠（蒲郡市・大和郡山市・東村山市…）が解けること
--   * **推測で埋めない**。当たらなければ NULL のまま残し、件数を返す
--   * 近傍は 0.01 度の格子 × haversine。**500 m の境界**と**格子をまたぐ組**
--   * 全置換（2 回動かして増えない）

begin;
select plan(50);

delete from public.station_neighbors where true;
delete from public.station_attributes;
delete from public.station_status_latest;
delete from public.status_snapshots;
delete from public.stations;
delete from public.job_runs;

-- ポートを 1 つ置く。住所と座標はテストが決める
create function pg_temp.put(
  p_system text, p_station text, p_idx int,
  p_lat double precision, p_lon double precision,
  p_address text default null, p_geo_suspect boolean default false
) returns void language sql as $$
  with s as (
    insert into public.stations (system_id, station_id, idx) values (p_system, p_station, p_idx)
    returning system_id, station_id
  )
  insert into public.station_attributes
    (system_id, station_id, valid_from, name, lat, lon, geo_suspect, raw)
  select s.system_id, s.station_id, now() - interval '1 day', 'テスト', p_lat, p_lon, p_geo_suspect,
         case when p_address is null then '{}'::jsonb else jsonb_build_object('address', p_address) end
    from s;
$$;

create function pg_temp.muni(p_station text) returns integer language sql as $$
  select muni_code from public.stations where station_id = p_station and system_id = 'hellocycling';
$$;

create function pg_temp.pref(p_station text) returns integer language sql as $$
  select pref_code from public.stations where station_id = p_station and system_id = 'hellocycling';
$$;

create function pg_temp.nb(p_station text) returns text language sql as $$
  select coalesce(string_agg(nb_station_id || ':' || distance_m, ',' order by nb_station_id), '(なし)')
    from public.station_neighbors
   where system_id = 'hellocycling' and station_id = p_station;
$$;

-- ────────────────────────────────────────────────────────────────
-- 参照データ
-- ────────────────────────────────────────────────────────────────
select has_table('public', 'muni_codes', 'muni_codes がある');
select col_is_pk('public', 'muni_codes', 'muni_code', '主キーは市区町村コード');
select is(
  (select relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relname = 'muni_codes'),
  true, 'RLS 有効'
);
select is(has_table_privilege('anon', 'public.muni_codes', 'select'), false, '匿名は読めない');
select is((select count(*)::int from public.muni_codes), 1917, '1,917 件（政令指定都市の区を含む）');
select is((select count(distinct pref_code)::int from public.muni_codes), 47, '47 都道府県');
select is(
  (select max(muni_code) from public.muni_codes), 47382,
  '最大は与那国町の 47382（**smallint では溢れる**）'
);
select is(
  (select count(*)::int from public.muni_codes where pref_code <> muni_code / 1000),
  0, '先頭 2 桁が都道府県コードと一致する'
);
select is(
  (select muni_code from public.muni_codes where muni_name = '横浜市瀬谷区'),
  14114, '政令指定都市の区が入っている'
);

-- ────────────────────────────────────────────────────────────────
-- 住所の解析：**正規表現の罠が消えていること**
-- ────────────────────────────────────────────────────────────────
-- どれも `.+?郡.+?[町村]` や `.+?[市区町村]` で誤って切れる実データの形
select pg_temp.put('hellocycling', 'gamagori', 0, 34.83, 137.22, '愛知県蒲郡市西浦町大山25-1');
select pg_temp.put('hellocycling', 'yamatokoriyama', 1, 34.65, 135.78, '奈良県大和郡山市下三橋町741');
select pg_temp.put('hellocycling', 'higashimurayama', 2, 35.75, 139.46, '東京都東村山市本町2-3-1');
select pg_temp.put('hellocycling', 'musashimurayama', 3, 35.75, 139.38, '東京都武蔵村山市本町1-1-1');
select pg_temp.put('hellocycling', 'hamura', 4, 35.76, 139.31, '東京都羽村市緑ヶ丘5-2-1');
select pg_temp.put('hellocycling', 'hatsukaichi', 5, 34.35, 132.33, '広島県廿日市市下平良1-11-1');
select pg_temp.put('hellocycling', 'seya', 6, 35.46, 139.47, '神奈川県横浜市瀬谷区６－２０－１１');
select pg_temp.put('hellocycling', 'neyagawa', 7, 34.77, 135.63, '大阪府寝屋川市郡元町1-1');
-- 住所そのものが壊れている例（事業者のデータにある）
select pg_temp.put('hellocycling', 'shodoshima', 8, 34.48, 134.24, '香川県小豆島町小豆郡小豆島町片城甲2-1');
-- 当たらない住所（推測で埋めないことの確認）
select pg_temp.put('hellocycling', 'nowhere', 9, 35.0, 139.0, 'どこでもない場所 1-2-3');
-- ドコモは住所を持たない
select pg_temp.put('docomo-cycle', 'd1', 0, 35.68, 139.76, null);

select lives_ok('select public.rebuild_station_geo()', '住所の解析は例外を投げない');

select is(pg_temp.muni('gamagori'), 23214, '蒲**郡**市が「蒲郡市西浦町」に化けない');
select is(pg_temp.muni('yamatokoriyama'), 29203, '大和**郡**山市が正しく解ける');
select is(pg_temp.muni('higashimurayama'), 13213, '東**村**山市が「東村」で止まらない');
select is(pg_temp.muni('musashimurayama'), 13223, '武蔵**村**山市も同様');
select is(pg_temp.muni('hamura'), 13227, '羽**村**市も同様');
select is(pg_temp.muni('hatsukaichi'), 34213, '廿日**市**市も同様');
select is(pg_temp.muni('neyagawa'), 27215, '寝屋川市（町名に「郡」がある）も同様');
select is(pg_temp.muni('shodoshima'), 37324, '住所が壊れていても先頭の市区町村で当たる');

select is(pg_temp.muni('seya'), 14114, '**最長一致**：横浜市ではなく横浜市瀬谷区');
select is(pg_temp.pref('seya'), 14, '都道府県コードも入る');

select is(pg_temp.muni('nowhere'), null, '**当たらなければ NULL のまま**（推測で埋めない）');
select is(
  (select muni_code from public.stations where system_id = 'docomo-cycle'),
  null, 'ドコモは住所が無いので NULL のまま'
);
select is(
  public.rebuild_station_geo() -> 'unmatched', '1'::jsonb,
  '当たらなかった件数を返す（黙って落とさない）'
);
select is(
  public.rebuild_station_geo() -> 'updated', '0'::jsonb,
  '**2 回目は 1 行も書かない**（冪等）'
);

-- **住所が消えたら古いコードを持ち越さない。** 台帳から作り直す関数が前回の値を
-- 残すと、特徴量が静かに間違った地域を指す
update public.station_attributes set raw = '{}'::jsonb
 where station_id = 'seya' and valid_to is null;
select is(public.rebuild_station_geo() -> 'updated', '1'::jsonb, '住所が消えた 1 行を書き直す');
select is(pg_temp.muni('seya'), null, '**住所が消えたら muni_code も NULL に戻る**');
select is(pg_temp.pref('seya'), null, 'pref_code も戻る');

-- 元に戻して、以降の検査に影響させない
update public.station_attributes
   set raw = jsonb_build_object('address', '神奈川県横浜市瀬谷区６－２０－１１')
 where station_id = 'seya' and valid_to is null;
select is(public.rebuild_station_geo() -> 'updated', '1'::jsonb, '住所が戻れば埋め直す');
select is(pg_temp.muni('seya'), 14114, '埋め直した値が正しい');

-- ────────────────────────────────────────────────────────────────
-- 近傍
-- ────────────────────────────────────────────────────────────────
delete from public.station_attributes;
delete from public.station_status_latest;
delete from public.stations;

-- 距離は事前に haversine で確かめた値（コメントは実測）
select pg_temp.put('hellocycling', 'a', 0, 35.0, 139.0, '東京都千代田区1-1');
select pg_temp.put('hellocycling', 'b', 1, 35.0, 139.001, '東京都千代田区1-2');    -- a から 91 m
select pg_temp.put('hellocycling', 'c', 2, 35.0044, 139.0, '東京都千代田区1-3');   -- a から 489 m
select pg_temp.put('hellocycling', 'd', 3, 35.0046, 139.0, '東京都千代田区1-4');   -- a から 511 m（圏外）
select pg_temp.put('docomo-cycle', 'x', 1, 35.0, 139.0005, null);                  -- a から 46 m（別事業者）
select pg_temp.put('hellocycling', 'far', 4, 35.02, 139.0, '東京都千代田区1-5');   -- a から 2,224 m
select pg_temp.put('hellocycling', 'bad', 5, 35.0, 139.0002, '東京都千代田区1-6', true); -- geo_suspect

select is(
  public.rebuild_station_neighbors() -> 'radius_m', '500'::jsonb, '既定の半径は 500 m'
);
select is(pg_temp.nb('a'), 'b:91,c:489,x:46', 'a の近傍は 3 つ（500 m 以内・距離も合う）');
select is(pg_temp.nb('d'), 'c:22', 'd は c だけ（**格子をまたぐ 22 m の組を取りこぼさない**）');
select is(pg_temp.nb('far'), '(なし)', '2,224 m 離れたポートは近傍なし');
select is(
  (select count(*)::int from public.station_neighbors
    where station_id = 'a' and nb_station_id = 'a'), 0, '自分自身は入らない'
);
select is(
  (select count(*)::int from public.station_neighbors
    where system_id = 'docomo-cycle' and station_id = 'x' and nb_station_id = 'a'),
  1, '**両向きの行が入る**（x → a も）'
);
select is(
  (select same_system from public.station_neighbors
    where station_id = 'a' and nb_station_id = 'x'),
  false, '別事業者は same_system = false'
);
select is(
  (select same_system from public.station_neighbors
    where station_id = 'a' and nb_station_id = 'b'),
  true, '同じ事業者は true'
);
select is(
  (select count(*)::int from public.station_neighbors
    where station_id = 'bad' or nb_station_id = 'bad'),
  0, '**geo_suspect のポートは入らない**（座標が日本の外）'
);
select is(
  public.rebuild_station_neighbors() -> 'isolated', '2'::jsonb,
  '近傍が 1 つも無いポートを数える（far と bad）'
);

-- 全置換
select is(
  public.rebuild_station_neighbors() -> 'before',
  public.rebuild_station_neighbors() -> 'after',
  '2 回動かしても件数が変わらない（全置換）'
);
select throws_ok(
  $$select public.rebuild_station_neighbors(501)$$,
  'P0001', null, '**500 m を超える半径は拒む**（3 × 3 の格子で覆えなくなる）'
);
select throws_ok(
  $$select public.rebuild_station_neighbors(0)$$, 'P0001', null, '0 も拒む'
);
select is(
  (select count(*)::int from public.station_neighbors where distance_m > 500),
  0, '距離の制約（0〜500）が効いている'
);

-- **格子は等値結合で突き合わせる。** これは本番でしか効かない契約なので、速度ではなく
-- 定義そのものを固定する。`between` の範囲結合に戻すとハッシュ結合にできず、20,741 ポートで
-- 80.4 秒かかった（等値結合なら 1.7 秒）。ローカルはポートが数個しか無く、速度では検知できない。
select ok(
  (select prosrc like '%r.gy = l.key_gy%' and prosrc not like '%between l.gy%'
     from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'rebuild_station_neighbors'),
  '**格子の突き合わせは等値結合**（範囲結合に戻すと本番で 47 倍遅くなる）'
);

-- ────────────────────────────────────────────────────────────────
-- 日次ジョブ
-- ────────────────────────────────────────────────────────────────
delete from public.job_runs;
select lives_ok('select public.rebuild_geo()', 'rebuild_geo は例外を投げない');
select is(
  (select status from public.job_runs where job_name = 'rebuild_geo' order by id desc limit 1),
  'ok', 'job_runs に ok で記録する'
);
select ok(
  (select detail ? 'geo' and detail ? 'neighbors' from public.job_runs
    where job_name = 'rebuild_geo' order by id desc limit 1),
  'detail に両方の結果が入る'
);
select is(
  (select count(*)::int from pg_locks
    where locktype = 'advisory' and classid = 8423 and objid = 8 and pid = pg_backend_pid()),
  1, 'rebuild_geo は (8423, 8) を取る'
);
select is(
  (select cron_job_name from public.monitored_jobs where job_name = 'rebuild_geo'),
  'rebuild_geo', '監視対象に入っている'
);
select is((select schedule from cron.job where jobname = 'rebuild_geo'), '30 19 * * *',
  '04:30 JST（属性同期の後）');

select * from finish();
rollback;
