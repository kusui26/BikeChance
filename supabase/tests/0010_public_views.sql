-- pgTAP: 公開 API のビューと権限（W2 プラン §5.7、PR E）
--
-- ここで固定したいのは 3 つ。
--   1. **匿名はビューだけを読める。** 基底テーブルには一切手が届かない（CLAUDE.md §5）
--   2. **ビューが内部の約束を外に漏らさない。** `-1`（未観測）は NULL、`flags` は真偽値、
--      壊れた座標と停止中システムは出さない
--   3. **予測が無くても行は落ちない**（0033）。台数は出せるので、予測だけを NULL にする
--
-- 前提条件はこのテストが自分で作る（0007 と同じ方針）。ローカルの状態に依存させない。
-- **行単位の検査は必ず `system_id` で絞る。** 他のデータが混ざっていても結果が変わらないように。

begin;
select plan(43);

-- ────────────────────────────────────────────────────────────────
-- 権限
-- ────────────────────────────────────────────────────────────────
select ok(has_table_privilege('anon', 'public.v1_stations_current', 'select'),
          'anon は v1_stations_current を読める');
select ok(has_table_privilege('anon', 'public.v1_feeds', 'select'),
          'anon は v1_feeds を読める');
select ok(has_table_privilege('service_role', 'public.v1_stations_current', 'select'),
          'service_role も読める（/v1 はサービスロールで問い合わせる）');

-- 「ビュー以外は読めない」を網羅で固定する。テーブルが増えても効く書き方にしておく
select is(
  (select count(*)::int
     from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind in ('r', 'p')
      and has_table_privilege('anon', c.oid, 'select')),
  0, 'anon が select できる「テーブル」は 1 つも無い');

select is(
  (select count(*)::int
     from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind = 'v'
      and has_table_privilege('anon', c.oid, 'select')),
  2, 'anon が select できる「ビュー」はちょうど 2 つ');

select ok(not has_table_privilege('anon', 'public.station_status_latest', 'select'),
          'anon は station_status_latest を読めない');
select ok(not has_table_privilege('anon', 'public.station_attributes', 'select'),
          'anon は station_attributes を読めない');
select ok(not has_table_privilege('anon', 'public.feed_state', 'select'),
          'anon は feed_state を読めない');
select ok(not has_table_privilege('anon', 'public.v1_stations_current', 'insert')
      and not has_table_privilege('anon', 'public.v1_stations_current', 'update')
      and not has_table_privilege('anon', 'public.v1_stations_current', 'delete'),
          'anon はビューに書き込めない');
select ok(not has_table_privilege('authenticated', 'public.v1_stations_current', 'select'),
          'authenticated には与えていない（認証の仕組みがまだ無い。必要になったら足す）');

-- ビューの実行はビューの所有者の権限で行われる（security_invoker = false）。
-- ここが true になると匿名から見えるのは「匿名が読めるもの」＝空になる
select is(
  (select coalesce((select option_value from pg_options_to_table(c.reloptions)
                     where option_name = 'security_invoker'), 'false')
     from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relname = 'v1_stations_current'),
  'false', 'v1_stations_current は security_invoker ではない（所有者の権限で実行する）');

-- ────────────────────────────────────────────────────────────────
-- bbox 索引の述語（0019）
-- ────────────────────────────────────────────────────────────────
-- 述語に `geo_suspect` を足すと、ビューの `coalesce(a.geo_suspect, false) = false` から
-- 含意を導けず、**索引が静かに使われなくなる**（実測で 11.5 ms 対 0.27 ms）。
-- 人間には同じ意味に見えるので、機械で固定しておく。
select ok(
  (select count(*)::int from pg_indexes
    where schemaname = 'public' and indexname = 'station_attributes_current_geo_idx') = 1,
  'bbox 検索用の索引がある');

select is(
  (select pg_get_expr(i.indpred, i.indrelid)
     from pg_index i join pg_class c on c.oid = i.indexrelid
    where c.relname = 'station_attributes_current_geo_idx'),
  '(valid_to IS NULL)',
  '索引の述語は valid_to is null だけ（geo_suspect を足すと使われなくなる。0019）');

-- ────────────────────────────────────────────────────────────────
-- 前提条件をこのテストが作る
-- ────────────────────────────────────────────────────────────────
insert into public.systems
  (system_id, display_name, operator_name, gbfs_base_url, expected_cadence_s, poll_interval_s, lock_key, is_active, capacity_is_dynamic)
values
  ('t-active',  '稼働中',   '事業者', 'https://example.test/gbfs', 300, 60, 9101, true,  false),
  ('t-halted',  '停止中',   '事業者', 'https://example.test/gbfs', 300, 60, 9102, false, true),
  -- 容量が動的なシステム（ドコモと同じ形）。**capacity をラック数として出さない**（0035）
  ('t-dynamic', '動的容量', '事業者', 'https://example.test/gbfs', 300, 60, 9103, true,  true);

insert into public.feed_state (system_id, last_observed_at) values
  ('t-active',  timestamptz '2026-09-08 00:00:00+00'),
  ('t-halted',  timestamptz '2026-09-08 00:00:00+00'),
  ('t-dynamic', timestamptz '2026-09-08 00:00:00+00');

insert into public.stations (system_id, station_id, idx) values
  ('t-active', 'plain',    0),
  ('t-active', 'geo-bad',  1),
  ('t-active', 'no-attr',  2),
  ('t-active', 'unseen',   3),
  ('t-halted', 'halted',   0),
  ('t-dynamic', 'dyn',     0);

insert into public.station_attributes
  (system_id, station_id, valid_from, valid_to, name, lat, lon, capacity, geo_suspect, raw)
values
  ('t-active', 'plain',   timestamptz '2026-09-01 00:00+00', null, '普通のポート', 35.68, 139.76, 12, false, '{}'),
  -- 閉じた（過去の）行。現在有効な行と取り違えていないかを見る
  ('t-active', 'plain',   timestamptz '2026-08-01 00:00+00', timestamptz '2026-09-01 00:00+00',
                                                              '昔の名前',     35.00, 139.00,  1, false, '{}'),
  ('t-active', 'geo-bad', timestamptz '2026-09-01 00:00+00', null, '壊れた座標', 35.90,  39.55,  5, true,  '{}'),
  ('t-active', 'unseen',  timestamptz '2026-09-01 00:00+00', null, '未観測',     35.70, 139.70,  8, false, '{}'),
  ('t-halted', 'halted',  timestamptz '2026-09-01 00:00+00', null, '停止中',     35.60, 139.60,  4, false, '{}'),
  -- 属性には数が入っている。**ビューが出さないだけ**で、データは消さない
  ('t-dynamic', 'dyn',   timestamptz '2026-09-01 00:00+00', null, '動的なポート', 35.65, 139.65, 20, false, '{}');

insert into public.station_status_latest
  (system_id, station_id, bikes, docks, flags, is_present, last_changed_at)
values
  ('t-active', 'plain',   3, 9, 7, true,  timestamptz '2026-09-08 00:00+00'),
  ('t-active', 'geo-bad', 1, 4, 7, true,  timestamptz '2026-09-08 00:00+00'),
  ('t-active', 'no-attr', 0, 5, 1, true,  timestamptz '2026-09-08 00:00+00'),
  -- 一度も観測されていないポート。本番に実在する（2026-09-08 の実測でドコモに 2 件）
  ('t-active', 'unseen', -1, -1, -1, false, timestamptz '2026-09-08 00:00+00'),
  ('t-halted', 'halted',  2, 2, 7, true,  timestamptz '2026-09-08 00:00+00'),
  -- 容量（20）より台数（27）が多い。**本番のドコモで 628 件起きていた形**（§12 の 115）
  ('t-dynamic', 'dyn',   27, 0, 7, true,  timestamptz '2026-09-08 00:00+00');

-- ────────────────────────────────────────────────────────────────
-- ビューの中身
-- ────────────────────────────────────────────────────────────────
select is(
  (select count(*)::int from public.v1_stations_current where system_id = 't-active'),
  3, '壊れた座標のポートは出ない（4 件のうち 3 件）');

select is(
  (select count(*)::int from public.v1_stations_current where system_id = 't-active' and station_id = 'geo-bad'),
  0, 'geo_suspect のポートは地図に出さない');

select is(
  (select count(*)::int from public.v1_stations_current where system_id = 't-halted'),
  0, '停止したシステムのポートは出さない（古い値を現在値にしない）');

select is(
  (select name from public.v1_stations_current where system_id = 't-active' and station_id = 'plain'),
  '普通のポート', '現在有効な属性行を使う（閉じた行の名前ではない）');

select is(
  (select capacity from public.v1_stations_current where system_id = 't-active' and station_id = 'plain')::int,
  12, '容量も現在有効な行から取る');

-- ────────────────────────────────────────────────────────────────
-- capacity は「固定のラック数」だけ（0035。W4 プラン §12 の 115）
-- ────────────────────────────────────────────────────────────────
-- 動的なシステムの `capacity` は日次同期の瞬間の `bikes + docks` が凍結された値で、
-- ラック数ではない。**そのまま渡すと「容量 20・借りられる 27」という矛盾が画面に出る。**
select ok(
  (select capacity is null from public.v1_stations_current
    where system_id = 't-dynamic' and station_id = 'dyn'),
  '容量が動的なシステムでは capacity を出さない（NULL）');

select is(
  (select capacity from public.station_attributes
    where system_id = 't-dynamic' and station_id = 'dyn' and valid_to is null)::int,
  20, '**属性の値は消さない**。ビューが出さないだけ（学習側はこちらを読む）');

select ok(
  (select bikes = 27 and docks = 0 and is_present
     from public.v1_stations_current where system_id = 't-dynamic' and station_id = 'dyn'),
  '容量を伏せても行は返り、台数はそのまま出る');

-- **矛盾が 1 つも残らないこと**を網羅で見る。ここが破れると画面に嘘が出る
select is(
  (select count(*)::int from public.v1_stations_current
    where capacity is not null and bikes is not null and capacity < bikes),
  0, '「容量より台数が多い」行はビューに 1 つも無い');

select ok(
  (select name is null and lat is null and lon is null and capacity is null
     from public.v1_stations_current where system_id = 't-active' and station_id = 'no-attr'),
  '属性の無いポートは落とさず NULL で返す（新しいポートは最大 1 日属性を持たない）');

select is(
  (select bikes from public.v1_stations_current where system_id = 't-active' and station_id = 'no-attr')::int,
  0, '属性が無くても現在値は返す（0 は「本当に 0 台」）');

select ok(
  (select bikes is null and docks is null from public.v1_stations_current where system_id = 't-active' and station_id = 'unseen'),
  '一度も観測されていないポートは -1 ではなく NULL');

select ok(
  (select is_installed is null and is_renting is null and is_returning is null
     from public.v1_stations_current where system_id = 't-active' and station_id = 'unseen'),
  '未観測の flags も NULL（false と区別する）');

select ok(
  (select is_installed and is_renting and is_returning
     from public.v1_stations_current where system_id = 't-active' and station_id = 'plain'),
  'flags = 7 は 3 つとも true');

select ok(
  (select is_installed and not is_renting and not is_returning
     from public.v1_stations_current where system_id = 't-active' and station_id = 'no-attr'),
  'flags = 1 は設置のみ true（ビット和を取り違えていない）');

-- ────────────────────────────────────────────────────────────────
-- v1_feeds
-- ────────────────────────────────────────────────────────────────
select is(
  (select count(*)::int from public.v1_feeds where system_id = 't-halted'),
  0, '停止したシステムは v1_feeds にも出ない');

select ok(
  (select last_observed_at is not null and expected_cadence_s = 300 and poll_interval_s = 60
     from public.v1_feeds where system_id = 't-active'),
  'v1_feeds は鮮度の判定に要る値を揃えて返す');

-- ────────────────────────────────────────────────────────────────
-- 列の一覧（0033）
-- ────────────────────────────────────────────────────────────────
-- 公開側は `select *` をせず列を並べて読む（`apps/web/lib/api/view-query.ts` の
-- `STATION_COLUMNS` / `FEED_COLUMNS`）。**片方だけ変えると実行時まで気づけない**ので、
-- ビューの側でも列を固定する。増やすときは両方を直すことになる。
select columns_are('public'::name, 'v1_stations_current'::name, array[
  'system_id', 'station_id', 'name', 'lat', 'lon', 'capacity', 'bikes', 'docks',
  'is_installed', 'is_renting', 'is_returning', 'is_present', 'last_changed_at',
  'forecast_horizons_min', 'forecast_p_bike_x1000', 'forecast_p_dock_x1000',
  'forecast_confidence', 'forecast_base_observed_at', 'forecast_model_version',
  'forecast_generated_at'
]::name[], 'v1_stations_current の列は view-query.ts の STATION_COLUMNS と同じ');

select columns_are('public'::name, 'v1_feeds'::name, array[
  'system_id', 'display_name', 'expected_cadence_s', 'poll_interval_s',
  'capacity_is_dynamic', 'last_observed_at', 'forecast_model_version', 'forecast_generated_at'
]::name[], 'v1_feeds の列は view-query.ts の FEED_COLUMNS と同じ');

-- ────────────────────────────────────────────────────────────────
-- 予測（0033）
-- ────────────────────────────────────────────────────────────────
-- 推論がまだ 1 度も走っていない状態。**「予測が無い」は「まだ」であって異常ではない**
select ok(
  (select forecast_model_version is null and forecast_generated_at is null
     from public.v1_feeds where system_id = 't-active'),
  '推論がまだなら v1_feeds の版は NULL（W2 からの意味を変えない）');

select ok(
  (select forecast_base_observed_at is null and forecast_horizons_min is null
     from public.v1_stations_current where system_id = 't-active' and station_id = 'plain'),
  '予測がまだ無いポートは、予測の列がすべて NULL');

-- 1 ポートにだけ予測を入れる。**残りの 2 ポートが消えないこと**がこの節の主眼
insert into public.station_forecasts
  (system_id, station_id, generated_at, base_observed_at, model_version,
   horizons_min, p_bike_x1000, p_dock_x1000, confidence)
values
  ('t-active', 'plain', timestamptz '2026-09-08 00:00:30+00', timestamptz '2026-09-08 00:00:00+00',
   'b1-2026-09-08', '{5,10,15}', '{900,880,860}', '{100,120,140}', 3);

select is(
  (select count(*)::int from public.v1_stations_current where system_id = 't-active'),
  3, '1 ポートにしか予測が無くても 3 件のまま（left join。行を落とさない）');

select ok(
  (select forecast_horizons_min = '{5,10,15}'::smallint[]
      and forecast_p_bike_x1000 = '{900,880,860}'::smallint[]
      and forecast_p_dock_x1000 = '{100,120,140}'::smallint[]
     from public.v1_stations_current where system_id = 't-active' and station_id = 'plain'),
  '予測の配列はそのまま渡す（ビューで加工しない）');

select ok(
  (select forecast_base_observed_at = timestamptz '2026-09-08 00:00:00+00'
      and forecast_model_version = 'b1-2026-09-08'
      and forecast_confidence = 3
     from public.v1_stations_current where system_id = 't-active' and station_id = 'plain'),
  '鮮度に使う base_observed_at と版・確度を返す');

-- **2 つの時刻は役目が違う**（0034）。取り違えると、鮮度で切る物差しと補間の起点が入れ替わる
--   base_observed_at … その予測がどの観測に基づくか。古ければ出さない
--   generated_at     … 水平の起点。補間する位置を決める
select ok(
  (select forecast_generated_at = timestamptz '2026-09-08 00:00:30+00'
      and forecast_generated_at <> forecast_base_observed_at
     from public.v1_stations_current where system_id = 't-active' and station_id = 'plain'),
  '水平の起点として generated_at を返す（base_observed_at とは別の列）');

select ok(
  (select forecast_horizons_min is null and forecast_p_bike_x1000 is null
      and forecast_p_dock_x1000 is null and forecast_confidence is null
      and forecast_base_observed_at is null and forecast_model_version is null
      and forecast_generated_at is null
     from public.v1_stations_current where system_id = 't-active' and station_id = 'no-attr'),
  '予測の無いポートは 7 列とも NULL（台数は出せるので行は残す）');

select is(
  (select bikes from public.v1_stations_current where system_id = 't-active' and station_id = 'no-attr')::int,
  0, '予測が無くても現在値は返る');

-- ────────────────────────────────────────────────────────────────
-- v1_feeds の「いま配信している版」（0033）
-- ────────────────────────────────────────────────────────────────
insert into public.inference_log
  (system_id, generated_at, base_observed_at, model_version, status, n_rows)
values
  ('t-active', timestamptz '2026-09-07 23:50:30+00', timestamptz '2026-09-07 23:50:00+00', 'b0-old',  'ok',     10),
  ('t-active', timestamptz '2026-09-07 23:55:30+00', timestamptz '2026-09-07 23:55:00+00', 'b0-bad',  'failed',  0),
  ('t-active', timestamptz '2026-09-08 00:00:30+00', timestamptz '2026-09-08 00:00:00+00', 'b1-2026-09-08', 'ok', 10),
  -- 最新だが失敗。**失敗した回の版を「配信中」と言わない**
  ('t-active', timestamptz '2026-09-08 00:05:30+00', timestamptz '2026-09-08 00:05:00+00', 'b2-bad',  'failed',  0),
  -- 走っている最中の回も「配信中」ではない
  ('t-active', timestamptz '2026-09-08 00:10:30+00', timestamptz '2026-09-08 00:10:00+00', 'b3-run',  'running', 0);

select is(
  (select forecast_model_version from public.v1_feeds where system_id = 't-active'),
  'b1-2026-09-08', '最後に成功した推論の版を返す（失敗・実行中は数えない）');

select is(
  (select forecast_generated_at from public.v1_feeds where system_id = 't-active'),
  timestamptz '2026-09-08 00:00:30+00', 'その回の generated_at を添える');

select is(
  (select count(*)::int from public.v1_feeds where system_id = 't-active'),
  1, '推論の記録が何行あってもフィードは 1 行（lateral が行を増やさない）');

select * from finish();
rollback;
