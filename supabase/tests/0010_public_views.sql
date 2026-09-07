-- pgTAP: 公開 API のビューと権限（W2 プラン §5.7、PR E）
--
-- ここで固定したいのは 2 つ。
--   1. **匿名はビューだけを読める。** 基底テーブルには一切手が届かない（CLAUDE.md §5）
--   2. **ビューが内部の約束を外に漏らさない。** `-1`（未観測）は NULL、`flags` は真偽値、
--      壊れた座標と停止中システムは出さない
--
-- 前提条件はこのテストが自分で作る（0007 と同じ方針）。ローカルの状態に依存させない。
-- **行単位の検査は必ず `system_id` で絞る。** 他のデータが混ざっていても結果が変わらないように。

begin;
select plan(24);

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
-- 前提条件をこのテストが作る
-- ────────────────────────────────────────────────────────────────
insert into public.systems
  (system_id, display_name, operator_name, gbfs_base_url, expected_cadence_s, poll_interval_s, lock_key, is_active, capacity_is_dynamic)
values
  ('t-active', '稼働中', '事業者', 'https://example.test/gbfs', 300, 60, 9101, true,  false),
  ('t-halted', '停止中', '事業者', 'https://example.test/gbfs', 300, 60, 9102, false, true);

insert into public.feed_state (system_id, last_observed_at) values
  ('t-active', timestamptz '2026-09-08 00:00:00+00'),
  ('t-halted', timestamptz '2026-09-08 00:00:00+00');

insert into public.stations (system_id, station_id, idx) values
  ('t-active', 'plain',    0),
  ('t-active', 'geo-bad',  1),
  ('t-active', 'no-attr',  2),
  ('t-active', 'unseen',   3),
  ('t-halted', 'halted',   0);

insert into public.station_attributes
  (system_id, station_id, valid_from, valid_to, name, lat, lon, capacity, geo_suspect, raw)
values
  ('t-active', 'plain',   timestamptz '2026-09-01 00:00+00', null, '普通のポート', 35.68, 139.76, 12, false, '{}'),
  -- 閉じた（過去の）行。現在有効な行と取り違えていないかを見る
  ('t-active', 'plain',   timestamptz '2026-08-01 00:00+00', timestamptz '2026-09-01 00:00+00',
                                                              '昔の名前',     35.00, 139.00,  1, false, '{}'),
  ('t-active', 'geo-bad', timestamptz '2026-09-01 00:00+00', null, '壊れた座標', 35.90,  39.55,  5, true,  '{}'),
  ('t-active', 'unseen',  timestamptz '2026-09-01 00:00+00', null, '未観測',     35.70, 139.70,  8, false, '{}'),
  ('t-halted', 'halted',  timestamptz '2026-09-01 00:00+00', null, '停止中',     35.60, 139.60,  4, false, '{}');

insert into public.station_status_latest
  (system_id, station_id, bikes, docks, flags, is_present, last_changed_at)
values
  ('t-active', 'plain',   3, 9, 7, true,  timestamptz '2026-09-08 00:00+00'),
  ('t-active', 'geo-bad', 1, 4, 7, true,  timestamptz '2026-09-08 00:00+00'),
  ('t-active', 'no-attr', 0, 5, 1, true,  timestamptz '2026-09-08 00:00+00'),
  -- 一度も観測されていないポート。本番に実在する（2026-09-08 の実測でドコモに 2 件）
  ('t-active', 'unseen', -1, -1, -1, false, timestamptz '2026-09-08 00:00+00'),
  ('t-halted', 'halted',  2, 2, 7, true,  timestamptz '2026-09-08 00:00+00');

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

select * from finish();
rollback;
