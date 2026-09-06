-- pgTAP: ingest_snapshot（W1 プラン §6.5、§11.1、§11.3）
--
-- ここが壊れると、あるポートの台数が別のポートの行に入る。配列と `idx` の対応は
-- 型でも制約でも守れないので、テストで固定するしかない。
--
-- 同時実行（`locked` の経路）は単一セッションでは再現できない。設計（アドバイザリロック）で
-- 担保し、PR D の観測で `dedup_hits` の内訳を見る。

begin;
select plan(38);

-- このファイルはトランザクション内で完結し rollback するので、ここでの削除は外に影響しない。
-- ベンチマークや手動確認でデータが残っていても同じ結果になるよう、作業テーブルを空にしてから始める
-- （テストが実行環境の状態に依存しないようにする）。
delete from public.station_status_latest;
delete from public.status_snapshots;
delete from public.station_attributes;
delete from public.stations;
delete from public.feed_fetch_log;
delete from public.job_runs;
update public.feed_state
   set last_fetch_at = null, last_success_at = null, last_observed_at = null,
       last_etag = null, consecutive_errors = 0;

-- 取り込みを 1 行で書けるようにする小さなヘルパ
create function pg_temp.ingest(
  p_ids text[], p_bikes int[], p_docks int[], p_observed text,
  p_system text default 'hellocycling', p_ratio numeric default 0.5
) returns jsonb
language sql as $$
  select public.ingest_snapshot(
    p_system, p_observed::timestamptz, p_observed::timestamptz, 'etag-' || p_observed,
    p_ids, p_bikes::smallint[], p_docks::smallint[],
    (select array_agg(7::smallint) from generate_series(1, coalesce(array_length(p_ids, 1), 0))),
    (select array_agg(0::smallint) from generate_series(1, coalesce(array_length(p_ids, 1), 0))),
    'raw/' || p_observed, p_ratio
  );
$$;

create function pg_temp.idx_map(p_system text default 'hellocycling') returns text
language sql as $$
  select coalesce(string_agg(station_id || '=' || idx, ',' order by idx), '')
    from public.stations where system_id = p_system;
$$;

-- ────────────────────────────────────────────────────────────────
-- 初回：ポート台帳が空の状態から
-- ────────────────────────────────────────────────────────────────
select is(
  pg_temp.ingest(array['a','b','c'], array[1,2,3], array[4,5,6], '2026-09-06T00:00:00Z')->>'status',
  'inserted', '初回の取り込みが通る'
);
select is(pg_temp.idx_map(), 'a=0,b=1,c=2', 'idx が 0 起点で密に振られる');
select is(
  (select array_length(bikes, 1) from public.status_snapshots where observed_at = '2026-09-06T00:00:00Z'),
  3, '配列長が登録ポート数と一致する'
);
select is(
  (select bikes::text from public.status_snapshots where observed_at = '2026-09-06T00:00:00Z'),
  '{1,2,3}', '値が idx 順に並ぶ'
);
select ok(
  (select not is_anomalous from public.status_snapshots where observed_at = '2026-09-06T00:00:00Z'),
  '初回は異常ガードに掛からない（登録済みが 0 のため）'
);

-- ────────────────────────────────────────────────────────────────
-- ポートの追加：既存の idx は不変
-- ────────────────────────────────────────────────────────────────
select is(
  pg_temp.ingest(array['a','b','c','d','e'], array[1,2,3,8,9], array[4,5,6,1,1],
                 '2026-09-06T00:05:00Z')->>'n_new_stations',
  '2', '新規ポートを 2 件登録する'
);
select is(pg_temp.idx_map(), 'a=0,b=1,c=2,d=3,e=4', '既存の idx は変わらず、新規は末尾に付く');
select is(
  (select array_length(bikes, 1) from public.status_snapshots where observed_at = '2026-09-06T00:05:00Z'),
  5, '配列が伸びる'
);
select is(
  (select array_length(bikes, 1) from public.status_snapshots where observed_at = '2026-09-06T00:00:00Z'),
  3, '過去のスナップショットは当時の長さのまま（後から伸ばさない）'
);

-- 配列の位置 idx+1 との対応（§11.1 の核心）
select is(
  (select bikes[(select idx from public.stations where station_id = 'd') + 1]
     from public.status_snapshots where observed_at = '2026-09-06T00:05:00Z'),
  8::smallint, 'arr[idx + 1] がそのポートの値になる'
);

-- ────────────────────────────────────────────────────────────────
-- ポートの欠落：-1 で埋め、is_present を倒す
-- ────────────────────────────────────────────────────────────────
select is(
  pg_temp.ingest(array['a','b','c','d'], array[1,2,3,8], array[4,5,6,1],
                 '2026-09-06T00:10:00Z')->>'n_stations',
  '4', '現れたポート数を返す（配列長ではない）'
);
select is(
  (select bikes[5] from public.status_snapshots where observed_at = '2026-09-06T00:10:00Z'),
  -1::smallint, '現れなかったポートの要素は -1'
);
select ok(
  (select not is_present from public.station_status_latest where station_id = 'e'),
  '現れなかったポートは is_present=false'
);
select is(
  (select bikes from public.station_status_latest where station_id = 'e'),
  9::smallint, '消えても最後に観測した値は保持する（§11.3）'
);

-- ────────────────────────────────────────────────────────────────
-- 二重処理
-- ────────────────────────────────────────────────────────────────
select is(
  pg_temp.ingest(array['a'], array[99], array[99], '2026-09-06T00:10:00Z')->>'status',
  'duplicate', '同じ observed_at は duplicate'
);
select is(
  (select count(*)::int from public.status_snapshots where observed_at = '2026-09-06T00:10:00Z'),
  1, '行は増えない'
);
select is(
  (select bikes[1] from public.status_snapshots where observed_at = '2026-09-06T00:10:00Z'),
  1::smallint, '既存の行は書き換わらない'
);
select is(
  (select last_etag from public.feed_state where system_id = 'hellocycling'),
  'etag-2026-09-06T00:10:00Z', 'duplicate では feed_state を触らない'
);

-- ────────────────────────────────────────────────────────────────
-- last_updated の後退（ODPT 側のキャッシュ戻り。§5 の 26）
-- ────────────────────────────────────────────────────────────────
select is(
  pg_temp.ingest(array['a','b','c','d','e'], array[50,50,50,50,50], array[1,1,1,1,1],
                 '2026-09-06T00:02:00Z')->>'status',
  'inserted', '古い observed_at でもスナップショットは保存する'
);
select is(
  (select bikes from public.station_status_latest where station_id = 'a'),
  1::smallint, '古い観測で station_status_latest は書き換わらない'
);
select is(
  (select last_observed_at from public.feed_state where system_id = 'hellocycling'),
  '2026-09-06T00:10:00Z'::timestamptz, 'feed_state の last_observed_at は巻き戻らない'
);

-- ────────────────────────────────────────────────────────────────
-- 変化していない再取り込み
-- ────────────────────────────────────────────────────────────────
select is(
  pg_temp.ingest(array['a','b','c','d'], array[1,2,3,8], array[4,5,6,1],
                 '2026-09-06T00:15:00Z')->>'n_changed',
  '0', '値が変わっていなければ n_changed=0（無駄な UPDATE をしない）'
);
select is(
  pg_temp.ingest(array['a','b','c','d'], array[1,2,3,7], array[4,5,6,1],
                 '2026-09-06T00:20:00Z')->>'n_changed',
  '1', '1 ポートだけ変われば n_changed=1'
);

-- ────────────────────────────────────────────────────────────────
-- 異常ガード（W1-18）
-- ────────────────────────────────────────────────────────────────
select is(
  pg_temp.ingest(array['a','b'], array[1,2], array[1,1], '2026-09-06T00:25:00Z')->>'is_anomalous',
  'true', '登録 5 に対し 2 ポートしか現れなければ異常'
);
select ok(
  (select is_present from public.station_status_latest where station_id = 'c'),
  '異常時は不在への反転をしない（翌日の is_active 判定を汚さない）'
);
select ok(
  (select is_anomalous from public.status_snapshots where observed_at = '2026-09-06T00:25:00Z'),
  '異常でもスナップショットは保存する（生データを捨てない）'
);
select is(
  pg_temp.ingest(array['a','b','c'], array[1,2,3], array[1,1,1], '2026-09-06T00:30:00Z')->>'is_anomalous',
  'false', 'ちょうど閾値（5 × 0.5 = 2.5 に対し 3）なら異常ではない'
);
select is(
  pg_temp.ingest(array['a','b','c','d','e'], array[1,2,3,8,9], array[4,5,6,1,1],
                 '2026-09-06T00:35:00Z', 'hellocycling', 1.0)->>'is_anomalous',
  'false', '閾値を 1.0 にしても全件揃っていれば異常ではない'
);

-- ────────────────────────────────────────────────────────────────
-- 空のフィード
-- ────────────────────────────────────────────────────────────────
select lives_ok(
  $$select pg_temp.ingest(array[]::text[], array[]::int[], array[]::int[], '2026-09-06T00:40:00Z')$$,
  '空のポート集合でも例外にならない'
);
select is(
  (select n_stations from public.status_snapshots where observed_at = '2026-09-06T00:40:00Z'),
  0, '空なら n_stations=0'
);
select ok(
  (select is_anomalous from public.status_snapshots where observed_at = '2026-09-06T00:40:00Z'),
  '空のフィードは異常ガードに掛かる'
);

-- 台帳が空のシステムに空のフィードを入れても壊れない
select lives_ok(
  $$select pg_temp.ingest(array[]::text[], array[]::int[], array[]::int[],
                          '2026-09-06T00:00:00Z', 'docomo-cycle')$$,
  '台帳が空のシステムに空のフィードを入れても例外にならない'
);

-- ────────────────────────────────────────────────────────────────
-- 契約違反は黙って通さない
-- ────────────────────────────────────────────────────────────────
select throws_ok(
  $$select public.ingest_snapshot('hellocycling', now(), now(), null,
      array['a','b'], array[1]::smallint[], array[1,2]::smallint[],
      array[7,7]::smallint[], array[0,0]::smallint[], 'p')$$,
  '22023', null, '配列長が揃っていなければ例外'
);
select throws_ok(
  $$select public.ingest_snapshot('hellocycling', now(), now(), null,
      array['a','a'], array[1,2]::smallint[], array[1,2]::smallint[],
      array[7,7]::smallint[], array[0,0]::smallint[], 'p')$$,
  '22023', null, '入力に重複した station_id があれば例外（左結合で行が増えるため）'
);
select throws_ok(
  $$select public.ingest_snapshot('unknown', now(), now(), null,
      array['a'], array[1]::smallint[], array[1]::smallint[],
      array[7]::smallint[], array[0]::smallint[], 'p')$$,
  '22023', null, '未知のシステムは例外'
);

-- ────────────────────────────────────────────────────────────────
-- システム間の独立
-- ────────────────────────────────────────────────────────────────
select is(
  pg_temp.ingest(array['a','x'], array[1,1], array[1,1], '2026-09-06T01:00:00Z', 'docomo-cycle')->>'status',
  'inserted', '別システムに同じ station_id があっても独立して扱える'
);
select is(
  pg_temp.idx_map('docomo-cycle'), 'a=0,x=1',
  'idx はシステムごとに 0 から振り直す'
);
select is(
  (select idx from public.stations where system_id = 'hellocycling' and station_id = 'a'),
  0, '他システムの登録が既存の idx に影響しない'
);

select * from finish();
rollback;
