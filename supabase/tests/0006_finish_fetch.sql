-- pgTAP: finish_fetch と補助関数（W1 プラン §6.5、§11.3、§11.6）
--
-- finish_fetch は**エラー経路からも呼ばれる**。ここで型変換に失敗すると、記録を残せない
-- まま二次的な例外になり、何が起きたか分からなくなる。壊れた入力でも記録だけは残ることを
-- 確かめる。

begin;
select plan(27);

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

create function pg_temp.log_count(p_system text default 'hellocycling') returns integer
language sql as $$
  select count(*)::int from public.feed_fetch_log where system_id = p_system;
$$;

create function pg_temp.errors(p_system text default 'hellocycling') returns integer
language sql as $$
  select consecutive_errors from public.feed_state where system_id = p_system;
$$;

-- ────────────────────────────────────────────────────────────────
-- 記録は必ず残る
-- ────────────────────────────────────────────────────────────────
select lives_ok(
  $$select public.finish_fetch('hellocycling',
      '{"ok": true, "result": "inserted", "source": "cron", "endpoint": "token",
        "http_status": 200, "bytes": 4197516, "duration_ms": 573, "n_stations": 14835,
        "ratelimit_remaining_day": 23998}'::jsonb)$$,
  '通常の記録が通る'
);
select is(pg_temp.log_count(), 1, 'feed_fetch_log に 1 行入る');
select is(
  (select result from public.feed_fetch_log order by id desc limit 1), 'inserted', 'result が記録される'
);
select is(
  (select source from public.feed_fetch_log order by id desc limit 1), 'cron', 'source が記録される'
);
select is(
  (select endpoint from public.feed_fetch_log order by id desc limit 1), 'token', 'endpoint が記録される'
);
select is(
  (select ratelimit_remaining_day from public.feed_fetch_log order by id desc limit 1),
  23998, 'ODPT の残量が記録される（W1-21）'
);
select is(
  (select bytes from public.feed_fetch_log order by id desc limit 1), 4197516, 'bytes が記録される'
);

-- ────────────────────────────────────────────────────────────────
-- feed_state の更新規則（§11.3）
-- ────────────────────────────────────────────────────────────────
update public.feed_state set consecutive_errors = 3, last_success_at = null
 where system_id = 'hellocycling';

select public.finish_fetch('hellocycling', '{"ok": true, "result": "unchanged"}'::jsonb);
select is(pg_temp.errors(), 0, 'unchanged は成功として連続失敗を 0 に戻す');
select ok(
  (select last_success_at is not null from public.feed_state where system_id = 'hellocycling'),
  'unchanged で last_success_at が進む（304 も ODPT からの正常な応答）'
);

update public.feed_state set consecutive_errors = 2 where system_id = 'hellocycling';
select public.finish_fetch('hellocycling', '{"ok": true, "result": "duplicate"}'::jsonb);
select is(pg_temp.errors(), 0, 'duplicate も成功として扱う');

update public.feed_state set consecutive_errors = 0, last_success_at = null
 where system_id = 'hellocycling';
select public.finish_fetch('hellocycling',
  '{"ok": false, "result": "error", "error": "fetch: TimeoutError"}'::jsonb);
select is(pg_temp.errors(), 1, 'error は連続失敗を加算する');
select public.finish_fetch('hellocycling', '{"ok": false, "result": "error"}'::jsonb);
select is(pg_temp.errors(), 2, '続けて失敗すればさらに加算する');
select ok(
  (select last_success_at is null from public.feed_state where system_id = 'hellocycling'),
  '失敗では last_success_at を進めない'
);

-- skipped_recent / locked はログだけ
update public.feed_state set consecutive_errors = 5 where system_id = 'hellocycling';
select public.finish_fetch('hellocycling', '{"ok": true, "result": "skipped_recent"}'::jsonb);
select is(pg_temp.errors(), 5, 'skipped_recent は連続失敗を変えない');
select public.finish_fetch('hellocycling', '{"ok": true, "result": "locked"}'::jsonb);
select is(pg_temp.errors(), 5, 'locked も連続失敗を変えない');
select is(
  (select result from public.feed_fetch_log order by id desc limit 1), 'locked',
  'それでもログには残る（二重配信の回数を数えるため）'
);

-- ────────────────────────────────────────────────────────────────
-- 壊れた入力でも記録は残る
-- ────────────────────────────────────────────────────────────────
select lives_ok(
  $$select public.finish_fetch('hellocycling', '{"ok": false, "result": "error"}'::jsonb)$$,
  '任意の項目が無くても記録できる'
);
select is(
  (select bytes from public.feed_fetch_log order by id desc limit 1), null,
  '欠けた数値は null になる'
);
select lives_ok(
  $$select public.finish_fetch('hellocycling',
      '{"ok": "yes", "result": "error", "bytes": "many", "http_status": null}'::jsonb)$$,
  '数値の位置に文字列が来ても例外にならない（記録を優先する）'
);
select is(
  (select bytes from public.feed_fetch_log order by id desc limit 1), null,
  '数値でない値は null に倒す'
);
select is(
  (select ok::text from public.feed_fetch_log order by id desc limit 1), 'false',
  '真偽値でない ok は false に倒す（成功と誤認しない）'
);

-- warnings は正規化が返す 5 項目をそのまま入れる（§11.6）
select public.finish_fetch('hellocycling',
  '{"ok": true, "result": "inserted",
    "warnings": {"exact_duplicates": 11, "conflicting_duplicates": 0,
                 "missing_last_reported": 0, "clamped_reported_age": 0,
                 "clamped_negative_counts": 17}}'::jsonb);
select is(
  (select (warnings->>'clamped_negative_counts')::int from public.feed_fetch_log order by id desc limit 1),
  17, 'warnings をそのまま保持する'
);
select is(
  (select (warnings->>'exact_duplicates')::int from public.feed_fetch_log order by id desc limit 1),
  11, '重複の件数も残る'
);

-- ────────────────────────────────────────────────────────────────
-- 補助関数
-- ────────────────────────────────────────────────────────────────
select is(public.jsonb_number('42'::jsonb), 42::numeric, '数値を取り出す');
select is(public.jsonb_number('"42"'::jsonb), null, '文字列は null に倒す');
select is(public.jsonb_boolean('true'::jsonb), true, '真偽値を取り出す');
select is(public.jsonb_boolean('null'::jsonb), null, 'JSON の null は null');

select * from finish();
rollback;
