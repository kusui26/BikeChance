-- pgTAP: テーブル・列・制約・参照データ（W1 プラン §6.3）
--
-- 構造の存在確認だけでなく、**制約が実際に悪い値を弾くか**を動かして確かめる。
-- 表を眺めて分かることより、間違った INSERT が落ちることの方が価値が高い。

begin;
select plan(61);

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

-- ────────────────────────────────────────────────────────────────
-- 10 テーブルの存在
-- ────────────────────────────────────────────────────────────────
select has_table('public'::name, 'systems'::name, 'systems がある');
select has_table('public'::name, 'stations'::name, 'stations がある');
select has_table('public'::name, 'station_attributes'::name, 'station_attributes がある');
select has_table('public'::name, 'status_snapshots'::name, 'status_snapshots がある');
select has_table('public'::name, 'station_status_latest'::name, 'station_status_latest がある');
select has_table('public'::name, 'feed_state'::name, 'feed_state がある');
select has_table('public'::name, 'feed_fetch_log'::name, 'feed_fetch_log がある');
select has_table('public'::name, 'job_runs'::name, 'job_runs がある');
select has_table('public'::name, 'daily_quality'::name, 'daily_quality がある');
select has_table('public'::name, 'app_config'::name, 'app_config がある');

-- W1 で作らないと決めたもの（W1-10）。空テーブルを先に作らない
select hasnt_table('public'::name, 'station_forecasts'::name, '予測テーブルは W4 まで作らない');
select hasnt_table('public'::name, 'model_versions'::name, 'モデル登録は W4 まで作らない');

-- ────────────────────────────────────────────────────────────────
-- 主キーと一意制約
-- ────────────────────────────────────────────────────────────────
select col_is_pk('public'::name, 'systems'::name, 'system_id'::name, 'systems の主キー');
select col_is_pk(
  'public'::name, 'stations'::name, array['system_id', 'station_id']::name[],
  'stations は (system_id, station_id) が主キー'
);
select col_is_pk(
  'public'::name, 'status_snapshots'::name, array['system_id', 'observed_at']::name[],
  'status_snapshots は (system_id, observed_at) が主キー'
);
select col_is_pk(
  'public'::name, 'station_status_latest'::name, array['system_id', 'station_id']::name[],
  'station_status_latest は (system_id, station_id) が主キー'
);
select col_is_pk(
  'public'::name, 'station_attributes'::name, array['system_id', 'station_id', 'valid_from']::name[],
  'station_attributes は SCD2 の 3 列が主キー'
);
select col_is_pk(
  'public'::name, 'daily_quality'::name, array['system_id', 'quality_date']::name[],
  'daily_quality は (system_id, quality_date) が主キー'
);
select col_is_unique('public'::name, 'systems'::name, 'lock_key'::name, 'lock_key は一意（アドバイザリロックのキー）');
select col_is_unique(
  'public'::name, 'stations'::name, array['system_id', 'idx']::name[],
  'idx はシステム内で一意（配列位置の割り当て）'
);

-- ────────────────────────────────────────────────────────────────
-- 配列の型（設計の核心）
-- ────────────────────────────────────────────────────────────────
select col_type_is('public'::name, 'status_snapshots'::name, 'bikes'::name, 'smallint[]', 'bikes は smallint[]');
select col_type_is('public'::name, 'status_snapshots'::name, 'docks'::name, 'smallint[]', 'docks は smallint[]');
select col_type_is('public'::name, 'status_snapshots'::name, 'flags'::name, 'smallint[]', 'flags は smallint[]');
select col_type_is(
  'public'::name, 'status_snapshots'::name, 'reported_age_s'::name, 'smallint[]',
  'reported_age_s は smallint[]'
);
select col_not_null('public'::name, 'status_snapshots'::name, 'reported_age_s'::name, 'reported_age_s は NOT NULL（欠損は -1 で表す）');
select col_not_null('public'::name, 'status_snapshots'::name, 'raw_path'::name, 'raw_path は NOT NULL（一次ソースへの参照）');

-- ────────────────────────────────────────────────────────────────
-- v1.1 で意味を直した列（§5 の 4）
-- ────────────────────────────────────────────────────────────────
select has_column(
  'public'::name, 'station_status_latest'::name, 'last_changed_at'::name,
  'last_changed_at がある（observed_at ではない）'
);
select has_column('public'::name, 'station_status_latest'::name, 'is_present'::name, 'is_present がある');
select hasnt_column(
  'public'::name, 'station_status_latest'::name, 'observed_at'::name,
  'observed_at は無い（「最後に変化」と「最後に観測」を混同させない）'
);
select hasnt_column(
  'public'::name, 'status_snapshots'::name, 'gap'::name,
  'gap 列は作らない（容量と結合して特徴量の段階で導出する。§11.2）'
);
select has_column(
  'public'::name, 'feed_fetch_log'::name, 'ratelimit_remaining_day'::name,
  'ODPT の日次残量を記録する列がある（W1-21）'
);
select has_column('public'::name, 'feed_fetch_log'::name, 'source'::name, 'ウォッチドッグを見分ける source がある');

-- ────────────────────────────────────────────────────────────────
-- パーティションと索引
-- ────────────────────────────────────────────────────────────────
select is(
  (select relkind::text from pg_class where oid = 'public.status_snapshots'::regclass),
  'p', 'status_snapshots はパーティション親テーブル'
);
select has_table(
  'public'::name, 'status_snapshots_default'::name,
  'DEFAULT パーティションがある（月替わりの安全網。W1-15）'
);
select has_index(
  'public'::name, 'station_attributes'::name, 'station_attributes_current_idx'::name,
  '現在有効な行を引く部分一意索引がある'
);
select has_index(
  'public'::name, 'feed_fetch_log'::name, 'feed_fetch_log_fetched_at_idx'::name,
  '取得ログの時刻索引がある（集計と 30 日削除の両方が使う）'
);

-- ────────────────────────────────────────────────────────────────
-- 参照データ（マイグレーションに入れた。seed ではない。§5 の 23）
-- ────────────────────────────────────────────────────────────────
select is((select count(*)::int from public.systems), 2, 'systems が 2 行');
select is((select count(*)::int from public.feed_state), 2, 'feed_state が 2 行（begin_fetch が claim できる）');
-- 行数ではなくキーの有無で見る。設定は後の PR で増える（PR E1 で監視の設定が加わった）
select ok(
  (select bool_and(exists (select 1 from public.app_config where key = k))
     from unnest(array['project_base_url']) k),
  'app_config にウォッチドッグの叩き先がある'
);
select is(
  (select expected_cadence_s from public.systems where system_id = 'hellocycling'),
  300, 'HELLO の期待周期は 300 秒'
);
select is(
  (select expected_cadence_s from public.systems where system_id = 'docomo-cycle'),
  80, 'ドコモの期待周期は 80 秒'
);
select is(
  (select count(distinct lock_key)::int from public.systems), 2, 'lock_key が重複していない'
);
select is(
  (select value from public.app_config where key = 'project_base_url'),
  'https://bike-chance.vercel.app', 'ウォッチドッグの叩き先が入っている'
);
select is(
  (select count(*)::int from public.feed_state where last_observed_at is not null),
  0, 'feed_state の初期状態は空（まだ何も取り込んでいない）'
);

-- ────────────────────────────────────────────────────────────────
-- 制約が実際に悪い値を弾くか
-- ────────────────────────────────────────────────────────────────
select throws_ok(
  $$insert into public.systems (system_id, display_name, operator_name, gbfs_base_url, expected_cadence_s, lock_key)
    values ('x', 'x', 'x', 'x', 0, 9)$$,
  '23514', null, '期待周期 0 は弾く'
);
select throws_ok(
  $$insert into public.systems (system_id, display_name, operator_name, gbfs_base_url, expected_cadence_s, lock_key)
    values ('x', 'x', 'x', 'x', 60, 1)$$,
  '23505', null, '既存の lock_key と重複したら弾く'
);
select throws_ok(
  $$insert into public.stations (system_id, station_id, idx) values ('hellocycling', 'a', -1)$$,
  '23514', null, '負の idx は弾く'
);
select throws_ok(
  $$insert into public.stations (system_id, station_id, idx) values ('unknown-system', 'a', 0)$$,
  '23503', null, '存在しないシステムのポートは弾く'
);
select throws_ok(
  $$insert into public.feed_fetch_log (system_id, source, result, ok)
    values ('hellocycling', 'unknown-source', 'inserted', true)$$,
  '23514', null, '未知の source は弾く'
);
select throws_ok(
  $$insert into public.feed_fetch_log (system_id, endpoint, result, ok)
    values ('hellocycling', 'origin', 'inserted', true)$$,
  '23514', null, '未知の endpoint は弾く'
);
select throws_ok(
  $$insert into public.feed_fetch_log (system_id, result, ok) values ('hellocycling', 'saved', true)$$,
  '23514', null, '未知の result は弾く（§11.6 の 6 種類だけ）'
);
select throws_ok(
  $$insert into public.job_runs (job_name, status) values ('x', 'unknown')$$,
  '23514', null, '未知の job status は弾く'
);
select throws_ok(
  $$update public.feed_state set consecutive_errors = -1 where system_id = 'hellocycling'$$,
  '23514', null, '連続失敗回数は負にできない'
);
select throws_ok(
  $$insert into public.feed_state (system_id) values ('hellocycling')$$,
  '23505', null, '同じシステムの feed_state を二重に作れない'
);

-- 配列長が揃っていないスナップショットを弾く（設計の核心の不変条件）
select throws_ok(
  $$insert into public.status_snapshots
      (system_id, observed_at, fetched_at, n_stations, bikes, docks, flags, reported_age_s, raw_path)
    values ('hellocycling', now(), now(), 2, '{1,2}', '{1}', '{7,7}', '{0,0}', 'p')$$,
  '23514', null, '配列長が揃っていなければ弾く'
);
select lives_ok(
  $$insert into public.status_snapshots
      (system_id, observed_at, fetched_at, n_stations, bikes, docks, flags, reported_age_s, raw_path)
    values ('hellocycling', now(), now(), 2, '{1,-1}', '{3,-1}', '{7,-1}', '{0,-1}', 'p')$$,
  '長さが揃っていれば通る（欠損は -1）'
);
select lives_ok(
  $$insert into public.status_snapshots
      (system_id, observed_at, fetched_at, n_stations, bikes, docks, flags, reported_age_s, raw_path)
    values ('docomo-cycle', now(), now(), 0, '{}', '{}', '{}', '{}', 'p')$$,
  '空のポート集合でも通る（フィードが空でも保存する）'
);

-- SCD2：有効な行は 1 ポートにつき 1 本だけ
select lives_ok(
  $$insert into public.stations (system_id, station_id, idx) values ('hellocycling', 's1', 0);
    insert into public.station_attributes (system_id, station_id, valid_from, raw)
      values ('hellocycling', 's1', now() - interval '1 day', '{}'::jsonb)$$,
  '属性の有効行を 1 本作れる'
);
select throws_ok(
  $$insert into public.station_attributes (system_id, station_id, valid_from, raw)
    values ('hellocycling', 's1', now(), '{}'::jsonb)$$,
  '23505', null, '同じポートに有効行を 2 本は作れない'
);
select throws_ok(
  $$insert into public.station_attributes (system_id, station_id, valid_from, valid_to, raw)
    values ('hellocycling', 's1', now(), now() - interval '1 hour', '{}'::jsonb)$$,
  '23514', null, 'valid_to が valid_from より前なら弾く'
);
select throws_ok(
  $$insert into public.station_status_latest
      (system_id, station_id, bikes, docks, flags, is_present, last_changed_at)
    values ('hellocycling', 'not-registered', 0, 0, 7, true, now())$$,
  '23503', null, '台帳に無いポートの最新状態は作れない'
);

select * from finish();
rollback;
