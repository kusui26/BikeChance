-- pgTAP: 予測テーブルと推論の記録（W3 プラン §5.10、マイグレーション 0026）
--
-- ここで固定したい契約は 4 つ。
--   * **同じ観測時刻に対する 2 度目の推論は掴めない**（Vercel Cron の二重起動を無害にする）
--   * 予測は**まとめて UPSERT** でき、2 回書いても行が増えない
--   * 確率は 0〜1000 の範囲で、配列の長さがそろっている
--   * 匿名ロールからは**何も見えない**

begin;
select plan(44);

delete from public.station_forecasts where true;
delete from public.inference_log where true;
delete from public.station_status_latest;
delete from public.status_snapshots;
delete from public.stations;

insert into public.stations (system_id, station_id, idx) values
  ('hellocycling', 'a', 0), ('hellocycling', 'b', 1);

create function pg_temp.forecast(p_station text, p_bike int[], p_confidence int default 2)
returns jsonb language sql as $$
  select jsonb_build_array(jsonb_build_object(
    'system_id', 'hellocycling', 'station_id', p_station,
    'generated_at', now(), 'base_observed_at', now() - interval '2 minutes',
    'model_version', 'baseline-b3-test',
    'horizons_min', array[5, 10, 15],
    'p_bike_x1000', p_bike,
    'p_dock_x1000', array[500, 500, 500],
    'confidence', p_confidence
  ));
$$;

-- ────────────────────────────────────────────────────────────────
-- 形
-- ────────────────────────────────────────────────────────────────
select has_table('public', 'station_forecasts', 'station_forecasts がある');
select has_table('public', 'inference_log', 'inference_log がある');
select col_is_pk(
  'public', 'station_forecasts', array['system_id', 'station_id'],
  '**1 ポート 1 行**（水平は配列）'
);
select is(
  (select relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relname = 'station_forecasts'),
  true, 'station_forecasts は RLS 有効'
);
select is(
  (select relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relname = 'inference_log'),
  true, 'inference_log は RLS 有効'
);
select is(
  (select count(*)::int from information_schema.role_table_grants
    where table_schema = 'public' and table_name = 'station_forecasts'
      and grantee in ('anon', 'authenticated')),
  0, '**匿名ロールには何も許さない**（公開は W4 のビュー経由）'
);
select ok(
  (select reloptions::text like '%fillfactor=50%' from pg_class c
     join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relname = 'station_forecasts'),
  '`fillfactor = 50`（5 分毎の全件更新を HOT に乗せる。W3-19）'
);

-- ────────────────────────────────────────────────────────────────
-- 掴む（二重推論を止める）
-- ────────────────────────────────────────────────────────────────
select is(
  public.begin_inference('hellocycling', '2026-09-08 10:00+09', 'm1') -> 'claimed',
  'true'::jsonb, '1 度目は掴める'
);
select is(
  public.begin_inference('hellocycling', '2026-09-08 10:00+09', 'm1') -> 'claimed',
  'false'::jsonb, '**同じ観測時刻の 2 度目は掴めない**（Cron の二重起動が無害になる）'
);
select is(
  (select count(*)::int from public.inference_log), 1, '記録は 1 行しか増えない'
);
select is(
  public.begin_inference('hellocycling', '2026-09-08 10:05+09', 'm1') -> 'claimed',
  'true'::jsonb, '観測時刻が進めば掴める'
);
select is(
  public.begin_inference('docomo-cycle', '2026-09-08 10:00+09', 'm1') -> 'claimed',
  'true'::jsonb, 'システムが違えば掴める'
);
select throws_ok(
  $$select public.begin_inference('nope', now(), 'm1')$$,
  '22023', null, '未知のシステムは例外にする'
);
select is(
  (select status from public.inference_log order by id limit 1), 'running',
  '掴んだ時点では running'
);

-- ────────────────────────────────────────────────────────────────
-- 終える
-- ────────────────────────────────────────────────────────────────
select lives_ok(
  $$select public.finish_inference(
      (select id from public.inference_log order by id limit 1), 'ok', 123, 4567)$$,
  'finish_inference は例外を投げない'
);
select is(
  (select status from public.inference_log order by id limit 1), 'ok', '状態が変わる'
);
select is(
  (select n_rows from public.inference_log order by id limit 1), 123, '行数が入る'
);
select ok(
  (select finished_at is not null and finished_at >= generated_at
     from public.inference_log order by id limit 1),
  '**`finished_at` は `clock_timestamp()`**（`now()` はトランザクション開始時刻で動かない）'
);
select throws_ok(
  $$update public.inference_log set status = 'weird' where id = (select min(id) from public.inference_log)$$,
  '23514', null, '知らない状態は入らない'
);

-- ────────────────────────────────────────────────────────────────
-- detail：成果物のドリフトを残す（0030、W3 プラン §12 の 110）
-- ────────────────────────────────────────────────────────────────
-- **落ちていることが見えないと、再学習の間隔が長すぎるのに気づけない。**
-- `finish_inference` に `p_detail` を足し、列になっていない要約だけを入れる。
select has_column('public', 'inference_log', 'detail', 'detail がある（0030）');
select is(
  (select data_type from information_schema.columns
    where table_schema = 'public' and table_name = 'inference_log' and column_name = 'detail'),
  'jsonb', 'detail は jsonb（列を足さずに数を増やせる）'
);

-- **多重定義になっていないこと。** `create or replace` は引数の並びが同じときしか
-- 置き換えにならない。末尾に既定値付きの引数を足すと 2 つ並び、PostgREST が
-- どちらを呼ぶか決められなくなる（0030 で落として作り直した理由）
select is(
  (select count(*)::int from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'finish_inference'),
  1, '**`finish_inference` は 1 つだけ**（多重定義していない）'
);
select is(
  (select pronargs::int from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'finish_inference'),
  6, '引数は 6 つ（p_detail を足した）'
);
select ok(
  (select proacl::text like '%service_role=X%' from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'finish_inference'),
  '**落として作り直すと ACL も消える。** 明示的に付け直してある（§12 の 90）'
);

select lives_ok(
  $$select public.finish_inference(
      (select id from public.inference_log order by id limit 1), 'ok', 123, 4567, null,
      '{"stations": 14928, "skipped": 492, "unknown_ports": 5}'::jsonb)$$,
  'detail を渡せる'
);
select is(
  (select detail->>'unknown_ports' from public.inference_log order by id limit 1),
  '5', '成果物に無かったポートの数が残る'
);
select is(
  (select detail->>'status' from public.inference_log order by id limit 1),
  null, '**列に在るものは入れない**（status は列で持っている）'
);

-- 古い呼び方（5 引数）も通る。マイグレーションとコードのどちらを先に出しても壊れない
select lives_ok(
  $$select public.finish_inference(
      (select id from public.inference_log order by id limit 1), 'ok', 1, 2)$$,
  '既定値があるので 4 引数でも呼べる'
);

-- ────────────────────────────────────────────────────────────────
-- まとめて書く
-- ────────────────────────────────────────────────────────────────
select is(
  public.upsert_forecasts(pg_temp.forecast('a', array[10, 20, 30])), 1, '1 行入る'
);
select is(
  (select p_bike_x1000 from public.station_forecasts where station_id = 'a'),
  array[10, 20, 30]::smallint[], '値が入る'
);
select is(
  public.upsert_forecasts(pg_temp.forecast('a', array[40, 50, 60])), 1, '同じポートは上書き'
);
select is(
  (select count(*)::int from public.station_forecasts), 1, '**2 回書いても行が増えない**'
);
select is(
  (select p_bike_x1000 from public.station_forecasts where station_id = 'a'),
  array[40, 50, 60]::smallint[], '新しい値になる'
);
select is(
  public.upsert_forecasts(
    pg_temp.forecast('a', array[1, 2, 3]) || pg_temp.forecast('b', array[4, 5, 6])),
  2, 'まとめて書ける'
);
select is(public.upsert_forecasts('[]'::jsonb), 0, '空でも落ちない');

-- ────────────────────────────────────────────────────────────────
-- 値の妥当性
-- ────────────────────────────────────────────────────────────────
select throws_ok(
  $$select public.upsert_forecasts(pg_temp.forecast('a', array[10, 1001, 30]))$$,
  '23514', null, '**確率は 0〜1000**（×1000 の整数）'
);
select throws_ok(
  $$select public.upsert_forecasts(pg_temp.forecast('a', array[10, 20]))$$,
  '23514', null, '**配列の長さがそろっていなければ入らない**（水平と確率がずれる）'
);
select throws_ok(
  $$select public.upsert_forecasts(pg_temp.forecast('a', array[10, 20, 30], 9))$$,
  '23514', null, 'confidence は 0〜3'
);
select throws_ok(
  $$select public.upsert_forecasts(pg_temp.forecast('unknown', array[10, 20, 30]))$$,
  '23503', null, '**台帳に無いポートは書けない**（外部キー）'
);

-- ────────────────────────────────────────────────────────────────
-- モデル成果物の置き場所（0027）
-- ────────────────────────────────────────────────────────────────
-- **`gbfs-parquet` に相乗りさせない。** あちらは Parquet の MIME しか許さない。
-- 段 8 で成果物を上げようとして 415 で弾かれた（W3 プラン §12 の 106）
select is(
  (select allowed_mime_types from storage.buckets where id = 'models'),
  array['application/gzip'],
  'models バケットは gzip を受け付ける'
);
select is(
  (select public from storage.buckets where id = 'models'), false,
  '**非公開**（読み書きはサービスロールのみ）'
);
select is(
  (select allowed_mime_types from storage.buckets where id = 'gbfs-parquet'),
  array['application/vnd.apache.parquet'],
  'gbfs-parquet は Parquet だけのまま（成果物を混ぜない）'
);

-- ────────────────────────────────────────────────────────────────
-- 監視とスケジュール
-- ────────────────────────────────────────────────────────────────
-- ウォッチドッグは**両系に広げてから**入れる（W3-19）。**手順 5 で入れた**（0029）
select is(
  (select count(*)::int from cron.job where jobname = 'infer_watchdog'), 1,
  '推論のウォッチドッグ（pg_cron 5 分毎）が登録されている（0029。W3-19 の手順 5）'
);
select is(
  (select count(*)::int from public.monitored_jobs where job_name = 'trigger_infer'), 1,
  '**監視対象にも入れる**（入れ忘れると check_cron_jobs が「知らないジョブ」として鳴る）'
);

select * from finish();
rollback;
