-- pgTAP: 運用の関数（W1 プラン §6.7）
--
-- pg_net の送信は**コミット後**に行われる（§4.3 の 8）。このテストは rollback するので
-- 実際に Webhook へ送られることはない。ここで確かめられるのは「記録と抑制の論理」まで。
-- 到達の確認は本番での強制発火で行う。

begin;
select plan(50);

-- テストはトランザクション内で完結し rollback するので、ここでの削除は外に影響しない
delete from public.station_status_latest;
delete from public.status_snapshots;
delete from public.station_attributes;
delete from public.stations;
delete from public.feed_fetch_log;
delete from public.job_runs;
delete from public.alert_state;
delete from public.daily_quality;
-- Vault の中身も**このテストが決める**。開発機で setup-vault.sh を流していても
-- 結果が変わらないようにするため（rollback で戻る。外にある本物の秘密は無事）
delete from vault.secrets where name in ('cron_secret', 'alert_webhook_url');
update public.feed_state
   set last_fetch_at = null, last_success_at = null, last_observed_at = null,
       last_etag = null, consecutive_errors = 0;

-- スナップショットを 1 行入れるヘルパ
create function pg_temp.put(p_system text, p_at timestamptz, p_bikes int[], p_anomalous boolean default false)
returns void language sql as $$
  insert into public.status_snapshots
    (system_id, observed_at, fetched_at, n_stations, is_anomalous, bikes, docks, flags, reported_age_s, raw_path)
  values (p_system, p_at, p_at,
          (select count(*) from unnest(p_bikes) v where v <> -1), p_anomalous,
          p_bikes::smallint[], p_bikes::smallint[], p_bikes::smallint[], p_bikes::smallint[], 'test');
$$;

create function pg_temp.alerts() returns text language sql as $$
  select coalesce(string_agg(alert_key, ',' order by alert_key), '(なし)') from public.alert_state;
$$;

create function pg_temp.job_status(p_name text) returns text language sql as $$
  select status from public.job_runs where job_name = p_name order by id desc limit 1;
$$;

create function pg_temp.job_detail(p_name text, p_key text) returns text language sql as $$
  select detail->>p_key from public.job_runs where job_name = p_name order by id desc limit 1;
$$;

-- ────────────────────────────────────────────────────────────────
-- 設定値
-- ────────────────────────────────────────────────────────────────
-- 既定値を -1 にしておくと、設定行が消えたときに素通りしない
select is(public.config_int('collect_interval_s', -1), 60, '出荷時の収集周期は 60 秒（0012）');
select is(public.config_int('存在しない', 42), 42, '未設定なら既定値を返す');

-- ────────────────────────────────────────────────────────────────
-- send_alert：抑制と、宛先が無くても記録する
-- ────────────────────────────────────────────────────────────────
select is(public.send_alert('k1', '{"message":"一度目"}'::jsonb), true, '初回は送る');
select is(public.send_alert('k1', '{"message":"二度目"}'::jsonb), false, '抑制間隔の内側は送らない');
select is(
  (select last_value->>'message' from public.alert_state where alert_key = 'k1'),
  '二度目', '送らなくても内容は最新にする（後から何が起きていたか追える）'
);
select is(
  (select count(*)::int from public.alert_state where alert_key = 'k1' and last_sent_at is not null),
  1, 'Webhook が未設定でも alert_state には残る'
);
select is(public.send_alert('k2', '{}'::jsonb), true, '別の key は独立して送る');

update public.alert_state set last_sent_at = now() - interval '2 hours' where alert_key = 'k1';
select is(public.send_alert('k1', '{}'::jsonb), true, '抑制間隔を過ぎれば再び送る');
select is(
  public.send_alert('k1', '{}'::jsonb, interval '1 second'), false,
  '抑制間隔は引数で変えられる（同一トランザクションでは now() が固定のため送らない）'
);

-- ────────────────────────────────────────────────────────────────
-- watchdog_collect：設定・発火条件・閾値（W1-29、W1-31）
-- ────────────────────────────────────────────────────────────────
-- net.http_get はキューに積むだけで、送信は**コミット後**（§4.3 の 8）。
-- このテストは rollback するので、ここから外へ出る通信は一切ない。

-- 設定が欠けたとき：例外を握って failed として残し、関数自体は正常に返す（W1-31）。
-- 再送出するとトランザクションごと巻き戻り「失敗した事実」まで消えてしまう
select is(public.watchdog_collect(), 0, 'cron_secret が無ければ 0 件で終わる');
select is(pg_temp.job_status('watchdog_collect'), 'failed', '設定不足が failed として残る');
select ok(
  (select detail ? 'reason' from public.job_runs where job_name = 'watchdog_collect' order by id desc limit 1),
  '失敗の理由も残る'
);

-- 以降は秘密がある前提にする。値は偽物で、rollback とともに消える
select ok(
  vault.create_secret('dummy-not-a-real-secret', 'cron_secret', 'pgTAP 用') is not null,
  'テスト用の cron_secret を置ける'
);

-- 送り先だけ欠けても同じ失敗の道を通る（本番 URL を書かずに済むよう退避して戻す）
create temp table saved_base_url on commit drop as
  select value from public.app_config where key = 'project_base_url';
delete from public.app_config where key = 'project_base_url';
select is(public.watchdog_collect(), 0, 'project_base_url が無ければ 0 件で終わる');
select is(pg_temp.job_status('watchdog_collect'), 'failed', '送り先が無いことも failed として残る');
insert into public.app_config (key, value)
  select 'project_base_url', value from saved_base_url;

-- 停滞しているシステムを叩き直す。
-- **周期はこのブロックが決める。** 出荷値（0012 の 60 秒）に暗黙に依存させない
update public.app_config set value = '300' where key = 'collect_interval_s';
update public.feed_state set last_fetch_at = null;
select is(public.watchdog_collect(), 2, '一度も取得できていない 2 システムを叩き直す');
select is(pg_temp.job_status('watchdog_collect'), 'ok', '成功も job_runs に残る');
select is(
  pg_temp.job_detail('watchdog_collect', 'threshold_s'), '630',
  'E1 の閾値は collect_interval_s 300 秒 × 2 + 30 = 630 秒'
);

-- Vercel Cron が正常に動いているときは何もしない（平常運転）
update public.feed_state set last_fetch_at = now();
select is(public.watchdog_collect(), 0, '直近に取得できていれば叩かない');

-- 閾値は収集周期に追随する。E2（毎分）では 150 秒に締まる（W1-29）
update public.app_config set value = '60' where key = 'collect_interval_s';
update public.feed_state set last_fetch_at = now() - interval '3 minutes';
select is(public.watchdog_collect(), 2, 'E2 では 3 分前の取得を停滞とみなす');
select is(
  pg_temp.job_detail('watchdog_collect', 'threshold_s'), '150',
  'E2 の閾値は 60 秒 × 2 + 30 = 150 秒'
);
select is(
  (select count(*)::int from public.job_runs where job_name = 'watchdog_collect' and status = 'running'),
  0, '走りっぱなしの記録は残らない（成功も失敗も必ず閉じる）'
);

-- 収集周期は出荷値（60）のまま次のブロックへ渡す
update public.feed_state set last_fetch_at = null;

-- ────────────────────────────────────────────────────────────────
-- monitor_feeds：E1 の間はドコモの停滞で誤報を出さない（W1-29 の要）
-- ────────────────────────────────────────────────────────────────
-- ドコモの期待周期は 80 秒なので素朴な閾値は 4 分。しかし収集が 5 分間隔の E1 では
-- 必ず超えてしまう。閾値を収集周期の 3 倍で下支えすると、手で無効化しなくても消える
update public.app_config set value = '300' where key = 'collect_interval_s';
update public.feed_state set last_observed_at = now() - interval '6 minutes';
select is(public.monitor_feeds(), 0, 'E1（収集 300 秒）では 6 分前の観測でも誤報を出さない');
select is(pg_temp.alerts(), 'k1,k2', '停滞の通知は増えていない');

update public.feed_state set last_observed_at = now() - interval '20 minutes';
select is(public.monitor_feeds(), 2, 'E1 でも 20 分の停滞は両システムとも検知する');
select ok(
  (select count(*)::int from public.alert_state where alert_key like 'feed_stalled:%') = 2,
  '両システムの停滞が記録された'
);

-- 収集周期を毎分に変えると、ドコモの閾値が 4 分に戻る
delete from public.alert_state where alert_key like 'feed_stalled:%';
update public.app_config set value = '60' where key = 'collect_interval_s';
update public.feed_state set last_observed_at = now() - interval '6 minutes';
select is(
  (select count(*)::int from public.alert_state where alert_key = 'feed_stalled:docomo-cycle'),
  0, '検知前は通知が無い'
);
select ok(public.monitor_feeds() >= 1, 'E2（収集 60 秒）では 6 分の停滞を検知する');
select is(
  (select count(*)::int from public.alert_state where alert_key = 'feed_stalled:docomo-cycle'),
  1, 'ドコモの停滞が検知された（HELLO の 15 分閾値は超えていない）'
);
select is(
  (select count(*)::int from public.alert_state where alert_key = 'feed_stalled:hellocycling'),
  0, 'HELLO は 6 分では停滞としない（期待周期 300 秒 × 3）'
);
update public.app_config set value = '60' where key = 'collect_interval_s';

-- ────────────────────────────────────────────────────────────────
-- monitor_feeds：その他の検知
-- ────────────────────────────────────────────────────────────────
delete from public.alert_state;
update public.feed_state set last_observed_at = now(), consecutive_errors = 7;
select ok(public.monitor_feeds() >= 1, '連続失敗を検知する');
select is(
  (select count(*)::int from public.alert_state where alert_key like 'collector_errors:%'),
  2, '両システムの連続失敗が記録された'
);

delete from public.alert_state;
update public.feed_state set consecutive_errors = 0, last_fetch_at = now();
select pg_temp.put('hellocycling', now() - interval '10 minutes', array[1, 2], true);
select ok(public.monitor_feeds() >= 1, '異常スナップショットを検知する');
select is(
  (select count(*)::int from public.alert_state where alert_key = 'anomalous_snapshot'),
  1, '異常スナップショットが記録された'
);

delete from public.alert_state;
delete from public.status_snapshots;
-- DEFAULT パーティションに入る時刻（3 年先）
select pg_temp.put('hellocycling', now() + interval '3 years', array[1]);
select ok(public.monitor_feeds() >= 1, 'DEFAULT パーティションに行が入ったことを検知する');
select is(
  (select count(*)::int from public.alert_state where alert_key = 'default_partition_used'),
  1, 'DEFAULT の使用が記録された'
);

-- ────────────────────────────────────────────────────────────────
-- refresh_station_activity（W1-13 の書き手）
-- ────────────────────────────────────────────────────────────────
delete from public.status_snapshots;
delete from public.alert_state;
insert into public.stations (system_id, station_id, idx, first_seen_at)
values ('hellocycling', 'a', 0, now() - interval '10 days'),
       ('hellocycling', 'b', 1, now() - interval '10 days');

-- a は現れ、b は現れない（-1）
select pg_temp.put('hellocycling', now() - interval '2 hours', array[5, -1]);
select ok(public.refresh_station_activity() >= 1, '観測されたポートを更新する');
select ok(
  (select last_seen_at is not null from public.stations where station_id = 'a'),
  '現れたポートの last_seen_at が入る'
);
select ok(
  (select last_seen_at is null from public.stations where station_id = 'b'),
  '現れなかったポート（-1）は更新しない'
);

-- b を 72 時間以上見えていない状態にして非活性化
update public.stations set last_seen_at = now() - interval '80 hours' where station_id = 'b';
select ok(public.refresh_station_activity() >= 0, '再実行できる');
select ok(
  (select not is_active from public.stations where station_id = 'b'),
  '72 時間見えないポートを非活性にする'
);
select ok(
  (select is_active from public.stations where station_id = 'a'),
  '観測されているポートは活性のまま'
);

-- b が再出現したら活性に戻す
select pg_temp.put('hellocycling', now() - interval '1 hour', array[5, 3]);
select ok(public.refresh_station_activity() >= 1, '再出現を取り込む');
select ok(
  (select is_active from public.stations where station_id = 'b'),
  '再出現したポートは活性に戻る'
);

-- ────────────────────────────────────────────────────────────────
-- compute_daily_quality：日付は JST（§5 の 17）
-- ────────────────────────────────────────────────────────────────
delete from public.status_snapshots;
delete from public.daily_quality;
-- JST の 09-06 は UTC の 09-05T15:00Z 〜 09-06T15:00Z。
-- **範囲内に複数件置く。** 1 件だけだと、集計が二乗になっていても 1 のままで気づけない
-- （§5.5 の 41 はそれで見逃した）
select pg_temp.put('hellocycling', '2026-09-05T15:30:00Z', array[1, 2]);
select pg_temp.put('hellocycling', '2026-09-05T15:35:00Z', array[1, 2]);          -- 欠損 300 秒
select pg_temp.put('hellocycling', '2026-09-05T15:45:00Z', array[1, 2], true);    -- 欠損 600 秒・異常
-- 09-05T14:30:00Z は JST では 09-05 の 23:30。範囲の外
select pg_temp.put('hellocycling', '2026-09-05T14:30:00Z', array[1, 2]);

select is(public.compute_daily_quality('2026-09-06'::date), 2, '2 システム分の行を書く');
select is(
  (select n_snapshots from public.daily_quality
    where system_id = 'hellocycling' and quality_date = '2026-09-06'),
  3, 'JST の範囲内の 3 件だけを数える（件数の二乗にならない）'
);
select is(
  (select n_anomalous from public.daily_quality
    where system_id = 'hellocycling' and quality_date = '2026-09-06'),
  1, '異常は 1 件（スナップショット数に比例して膨らまない）'
);
select is(
  (select max_gap_s from public.daily_quality
    where system_id = 'hellocycling' and quality_date = '2026-09-06'),
  600, '最大欠損は 15:35 → 15:45 の 600 秒'
);
select is(
  (select n_snapshots from public.daily_quality
    where system_id = 'docomo-cycle' and quality_date = '2026-09-06'),
  0, '1 件も取れていないシステムも 0 件として行を残す'
);
select is(
  (select n_expected from public.daily_quality
    where system_id = 'docomo-cycle' and quality_date = '2026-09-06'),
  1080, '期待値は 86400 / expected_cadence_s（ドコモは 1,080）'
);

select * from finish();
rollback;
