-- pgTAP: ジョブの監視（W3 プラン §5.3、マイグレーション 0020）
--
-- **このテストは自分の前提を自分で作る。** ローカルでは pg_cron が動いていて
-- `job_runs` に行が増え続けるので、件数を数える検査は「自分で入れた行」だけに
-- 閉じる（`monitored_jobs` を全部 is_active = false にしてから必要な行を足す）。
--
-- pg_net の送信はコミット後なので、rollback するここでは Webhook に届かない。
-- 確かめられるのは記録と抑制の論理まで（0007 と同じ）。

begin;
select plan(107);

-- 自分の前提を作る
delete from public.status_snapshots;
delete from public.job_runs;
delete from public.alert_state;
delete from vault.secrets where name in ('cron_secret', 'alert_webhook_url');

-- **`storage.objects` からは直接 DELETE できない**（`storage.protect_delete()` が止める。
-- 「Storage API を使え」というトリガで、孤児オブジェクトを防ぐためのもの）。したがって
-- ここは「消してから始める」ができない。前提を**表明**して始める（W3 プラン §12 の 81）。
select is(
  (select count(*)::int from storage.objects where bucket_id = 'gbfs-parquet'),
  0, '前提：ローカルの gbfs-parquet は空（storage.objects は削除できないので表明で始める）'
);

create function pg_temp.alerts() returns text language sql as $$
  select coalesce(string_agg(alert_key, ',' order by alert_key), '(なし)') from public.alert_state;
$$;

create function pg_temp.has_alert(p_key text) returns boolean language sql as $$
  select exists (select 1 from public.alert_state where alert_key = p_key);
$$;

create function pg_temp.job_status(p_name text) returns text language sql as $$
  select status from public.job_runs where job_name = p_name order by id desc limit 1;
$$;

-- UTC の「時」に丸めた時刻から、Parquet のオブジェクト名を組み立てる
create function pg_temp.parquet_name(p_system text, p_hour timestamp) returns text language sql as $$
  select p_system || '/date=' || to_char(p_hour, 'YYYY-MM-DD')
                  || '/hour=' || to_char(p_hour, 'HH24') || '/part.parquet';
$$;

-- 直近 6 時間の窓に確実に入る時刻（UTC の時に丸めた値）
create function pg_temp.window_hour() returns timestamp language sql stable as $$
  select date_trunc('hour', (now() at time zone 'UTC')) - interval '3 hours';
$$;

create function pg_temp.put_snapshot(p_system text, p_hour timestamp) returns void language sql as $$
  insert into public.status_snapshots
    (system_id, observed_at, fetched_at, n_stations, is_anomalous, bikes, docks, flags, reported_age_s, raw_path)
  values (p_system, (p_hour at time zone 'UTC') + interval '10 minutes',
          (p_hour at time zone 'UTC') + interval '10 minutes',
          1, false, '{1}'::smallint[], '{1}'::smallint[], '{7}'::smallint[], '{0}'::smallint[], 'test');
$$;

-- ────────────────────────────────────────────────────────────────
-- 構造
-- ────────────────────────────────────────────────────────────────
select has_table('public', 'monitored_jobs', 'monitored_jobs がある');
select col_is_pk('public', 'monitored_jobs', 'job_name', '主キーはジョブ名');
select is(
  (select relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relname = 'monitored_jobs'),
  true, 'monitored_jobs は RLS 有効（CLAUDE.md §5）'
);
select is(
  has_table_privilege('anon', 'public.monitored_jobs', 'select'),
  false, '匿名は monitored_jobs を読めない'
);

select has_function('public', 'check_jobs_missing', 'check_jobs_missing がある');
select has_function('public', 'check_jobs_failed', 'check_jobs_failed がある');
select has_function('public', 'check_parquet_gap', 'check_parquet_gap がある');
select has_function('public', 'check_reference_data', 'check_reference_data がある');
select has_function('public', 'check_inference', 'check_inference がある（0029）');
select has_function('public', 'trigger_infer', 'trigger_infer がある（0029）');
select has_function('public', 'monitor_jobs', 'monitor_jobs がある');

-- security definer かつ search_path='' でなければ、本番と権限の効き方が変わる
select is(
  (select bool_and(p.prosecdef) from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public'
      and p.proname in ('check_jobs_missing','check_jobs_failed','check_parquet_gap',
                        'check_reference_data','check_inference','trigger_infer','monitor_jobs')),
  true, '7 つとも security definer'
);
select is(
  (select bool_and(p.proconfig @> array['search_path=""']) from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public'
      and p.proname in ('check_jobs_missing','check_jobs_failed','check_parquet_gap',
                        'check_reference_data','check_inference','trigger_infer','monitor_jobs')),
  true, '7 つとも search_path = ''''（スキーマ付きで書く前提）'
);
select is(
  has_function_privilege('anon', 'public.monitor_jobs()', 'execute'),
  false, '匿名は monitor_jobs を実行できない'
);

-- pg_cron の登録。monitor_feeds（:00,:05…）・compact（:07）・weather（:17）と重ねない
select is(
  (select schedule from cron.job where jobname = 'monitor_jobs'),
  '4-59/5 * * * *', 'monitor_jobs は :04 起点の 5 分毎'
);
select is((select active from cron.job where jobname = 'monitor_jobs'), true, 'monitor_jobs は有効');

-- 推論のウォッチドッグ（0029）。**検査（monitor_jobs）とは別の登録**にしてある
select is(
  (select schedule from cron.job where jobname = 'infer_watchdog'),
  '*/5 * * * *', 'infer_watchdog は 5 分毎'
);
select is(
  (select active from cron.job where jobname = 'infer_watchdog'), true, 'infer_watchdog は有効'
);

-- ────────────────────────────────────────────────────────────────
-- 設計が成り立つ前提
-- ────────────────────────────────────────────────────────────────
-- **これが読めないと Parquet の欠落検査は作れない。** postgres は BYPASSRLS を持つ
select lives_ok(
  'select count(*) from storage.objects',
  'postgres は storage.objects を読める（BYPASSRLS）'
);
select is(
  (select rolbypassrls from pg_roles where rolname = 'postgres'),
  true, 'postgres は BYPASSRLS（security definer の中から全行が見える）'
);

-- 0020 で 9 本、0021・0022・0025・0029・0036・0037・0040 が 1 本ずつ足して 16 本
select is((select count(*)::int from public.monitored_jobs), 16, '監視対象は 16 ジョブ');

-- **参照スナップショットは Vercel Cron だが、見張りには入れる**（0036。§12 の 116）。
-- 学習も推論も「前日の版」を読むので、止まると翌日に静かに壊れる
select ok(
  (select is_active and cron_job_name is null
      and expected_every = interval '1 day' and missing_after = interval '30 hours'
     from public.monitored_jobs where job_name = 'build_reference'),
  'build_reference は日次で見張る（pg_cron ではないので cron_job_name は NULL）');
-- **学習サンプルは GitHub Actions だが、見張りには入れる**（0040。§8.5.2）。
-- 止まったまま 30 日経つと、天気が保持期間で消えて作り直せなくなる
select ok(
  (select is_active and cron_job_name is null
      and expected_every = interval '1 day' and missing_after = interval '30 hours'
     from public.monitored_jobs where job_name = 'build_features'),
  'build_features は日次で見張る（pg_cron ではないので cron_job_name は NULL）');

-- **登録した直後は鳴らない。** `check_jobs_missing` は `added_at + missing_after` を
-- 過ぎた行しか見ない（0020）。入れた瞬間に「1 度も成功していません」と鳴ると、
-- 見張りを足すたびに誤報が出て、誰も通知を読まなくなる
select ok(
  (select added_at + missing_after > now()
     from public.monitored_jobs where job_name = 'build_features'),
  '足したばかりの build_features は、まだ検査の対象にならない');

select is(
  (select array_agg(job_name order by job_name) from public.monitored_jobs where not is_active),
  array['trigger_backup_collect', 'trigger_infer'],
  '成功の有無を見ないのは発火したときしか記録しない 2 本だけ（0021・0029）'
);
select is(
  (select bool_and(missing_after > expected_every) from public.monitored_jobs),
  true, '閾値は必ず周期より長い（短いと鳴り続ける）'
);
select throws_ok(
  $$insert into public.monitored_jobs (job_name, expected_every, missing_after)
    values ('bad', interval '1 hour', interval '10 minutes')$$,
  '23514', null, '閾値が周期より短い行は制約で弾かれる'
);

-- ────────────────────────────────────────────────────────────────
-- 検査 1：直近 N 時間に成功が無い
-- ────────────────────────────────────────────────────────────────
-- 以降は自分で入れた 1 行だけを見る（ローカルの pg_cron に影響されないため）
update public.monitored_jobs set is_active = false;
insert into public.monitored_jobs (job_name, expected_every, missing_after, note)
values ('pgtap_fake_job', interval '1 hour', interval '3 hours', 'テスト用');

select is(
  public.check_jobs_missing() -> 'checked', '0'::jsonb,
  '登録直後は見ない（まだ 1 度も回っていないだけかもしれない）'
);
select is(pg_temp.alerts(), '(なし)', '登録直後は通知しない');

update public.monitored_jobs set added_at = now() - interval '10 days';
select is(public.check_jobs_missing() -> 'checked', '1'::jsonb, '猶予を過ぎたジョブは見る');
select ok(pg_temp.has_alert('job_missing:pgtap_fake_job'), '成功が 1 件も無ければ通知する');

select is(public.check_jobs_missing() -> 'alerts', '0'::jsonb, '2 回目は抑制されて送らない');

delete from public.alert_state;
insert into public.job_runs (job_name, started_at, finished_at, status)
values ('pgtap_fake_job', now() - interval '1 minute', now(), 'ok');
select is(public.check_jobs_missing() -> 'alerts', '0'::jsonb, '直近に成功があれば鳴らない');
select is(pg_temp.alerts(), '(なし)', '同上（通知も残らない）');

delete from public.job_runs;
insert into public.job_runs (job_name, started_at, finished_at, status)
values ('pgtap_fake_job', now() - interval '4 hours', now() - interval '4 hours', 'ok');
select ok(
  public.check_jobs_missing() -> 'alerts' = '1'::jsonb,
  '閾値より古い成功しか無ければ鳴る（3 時間の閾値に対し 4 時間前）'
);
select is(
  (select last_value->>'last_ok_at' is not null from public.alert_state
    where alert_key = 'job_missing:pgtap_fake_job'),
  true, '通知に最後に成功した時刻が入る'
);

-- ────────────────────────────────────────────────────────────────
-- 検査 2：失敗したジョブ
-- ────────────────────────────────────────────────────────────────
delete from public.job_runs; delete from public.alert_state;
select is(public.check_jobs_failed() -> 'alerts', '0'::jsonb, '失敗が無ければ鳴らない');

insert into public.job_runs (job_name, started_at, finished_at, status, detail)
values ('pgtap_fake_job', now() - interval '10 minutes', now(), 'failed',
        jsonb_build_object('error', 'boom', 'code', 'XX000'));
select is(public.check_jobs_failed() -> 'alerts', '1'::jsonb, '直近 3 時間の失敗で鳴る');
select is(
  (select last_value->>'last_error' from public.alert_state where alert_key = 'job_failed:pgtap_fake_job'),
  'boom', '通知に最後のエラー文が入る'
);

delete from public.job_runs; delete from public.alert_state;
insert into public.job_runs (job_name, started_at, finished_at, status)
values ('pgtap_fake_job', now() - interval '5 hours', now() - interval '5 hours', 'failed');
select is(public.check_jobs_failed() -> 'alerts', '0'::jsonb, '3 時間より古い失敗では鳴らない');

-- ────────────────────────────────────────────────────────────────
-- 通知に「何が失われたか」を書く（0041、W4 プラン §6.8 の PR K）
-- ────────────────────────────────────────────────────────────────
-- **理由の取り出しは、実際に書かれている 4 つの形を並べたもの。** 推測ではない
select is(public.failure_reason(jsonb_build_object('error', 'boom', 'code', 'XX000')),
          'boom', 'error（ジョブ全体が落ちた形）を拾う');
select is(public.failure_reason(
            jsonb_build_object('n_failed', 1,
                               'failures', jsonb_build_array('0: fetch/TypeError: fetch failed'))),
          '0: fetch/TypeError: fetch failed',
          'failures（分割が落ちた形。**2026-09-10 に拾えなかった形**）を拾う');
select is(public.failure_reason(
            jsonb_build_object('ok', false, 'systems', jsonb_build_array(
              jsonb_build_object('system_id', 'a', 'error', null),
              jsonb_build_object('system_id', 'b', 'error', 'storage 500')))),
          'storage 500', 'systems[].error（系統が落ちた形）を拾う');
select is(public.failure_reason(jsonb_build_object('reason', 'cron_secret が未設定')),
          'cron_secret が未設定', 'reason（起動できなかった形）を拾う');

-- **知らない形でも黙らない。** NULL を返すと、また「last_error: null」だけが届く
select is(public.failure_reason(jsonb_build_object('未知の鍵', 42)),
          '{"未知の鍵": 42}', '知らない形は生の断片を返す');
select is(public.failure_reason(null), null, 'detail が無ければ理由も無い');
select is(public.failure_reason('{}'::jsonb), null, '空の detail は理由にしない');
-- 空文字は理由として採らない。**ただし黙りもしない**——生の断片が出る
select is(public.failure_reason(jsonb_build_object('error', '')),
          '{"error": ""}', '空文字は理由にせず、生の断片に落ちる');

-- **`error` が在れば、ほかの鍵より優先する**（ジョブ全体の失敗のほうが上位の事実）
select is(public.failure_reason(
            jsonb_build_object('error', '全体が落ちた', 'failures', jsonb_build_array('分割'))),
          '全体が落ちた', 'error が在れば failures より優先する');

-- 完了条件：**`archive_weather` を 1 回わざと失敗させると、理由と「取り返せない」が出る**
delete from public.job_runs; delete from public.alert_state;
insert into public.job_runs (job_name, started_at, finished_at, status, detail)
values ('archive_weather', now() - interval '2 minutes', now(), 'failed',
        jsonb_build_object('n_batches', 6, 'n_saved', 5, 'n_failed', 1,
                           'failures', jsonb_build_array('0: fetch/TypeError: fetch failed')));
select is(public.check_jobs_failed() -> 'alerts', '1'::jsonb, 'archive_weather の失敗で鳴る');
select is(
  (select last_value->>'last_error' from public.alert_state
    where alert_key = 'job_failed:archive_weather'),
  '0: fetch/TypeError: fetch failed',
  '通知に理由が入る（2026-09-10 は null だった）'
);
select alike(
  (select last_value->>'message' from public.alert_state
    where alert_key = 'job_failed:archive_weather'),
  '%二度と取れません%',
  '通知の本文に「取り返せない」と書いてある'
);
select alike(
  (select last_value->>'message' from public.alert_state
    where alert_key = 'job_failed:archive_weather'),
  '%Open-Meteo は過去の発行を返さない%',
  '本文にその理由（なぜ取り返せないか）も入る'
);

-- **印の無いジョブには足さない。** 全部に同じ文が付くと、付いていることに意味が無くなる
delete from public.job_runs; delete from public.alert_state;
insert into public.job_runs (job_name, started_at, finished_at, status, detail)
values ('compact_parquet', now() - interval '2 minutes', now(), 'failed',
        jsonb_build_object('error', 'boom'));
select is(public.check_jobs_failed() -> 'alerts', '1'::jsonb, 'compact_parquet の失敗でも鳴る');
select is(
  (select last_value->>'message' from public.alert_state
    where alert_key = 'job_failed:compact_parquet'),
  'compact_parquet が直近 3 時間に 1 回失敗しました',
  '印の無いジョブの本文は元のまま（再実行で取り返せるので足すことが無い）'
);

-- **印が付いているのは 1 本だけ**であることを固定する（増やすなら理由を書いてから）
select is(
  (select array_agg(job_name order by job_name) from public.monitored_jobs
    where failure_note is not null),
  array['archive_weather'],
  'failure_note が付いているのは archive_weather だけ'
);

-- **monitored_jobs に入っていないジョブの失敗も拾う**（どこで落ちても知りたい）
delete from public.job_runs; delete from public.alert_state;
insert into public.job_runs (job_name, started_at, finished_at, status)
values ('not_monitored_job', now() - interval '1 minute', now(), 'failed');
select is(public.check_jobs_failed() -> 'failed_jobs', '1'::jsonb, '監視対象表に無いジョブも数える');
select ok(pg_temp.has_alert('job_failed:not_monitored_job'), '監視対象表に無いジョブの失敗も拾う');

-- ────────────────────────────────────────────────────────────────
-- 検査 3：Parquet の欠落
-- ────────────────────────────────────────────────────────────────
delete from public.job_runs; delete from public.alert_state;
select is(public.check_parquet_gap() -> 'missing', '0'::jsonb, 'スナップショットが無ければ欠落も無い');

select pg_temp.put_snapshot('hellocycling', pg_temp.window_hour());
select pg_temp.put_snapshot('docomo-cycle', pg_temp.window_hour());
select is(public.check_parquet_gap() -> 'checked', '2'::jsonb, '2 系統 × 1 時間の組が対象になる');
select is(public.check_parquet_gap() -> 'missing', '2'::jsonb, 'Parquet が無ければ 2 件の欠落');
select ok(pg_temp.has_alert('parquet_gap'), '欠落があれば通知する');
select is(
  (select last_value->'missing'->>0 from public.alert_state where alert_key = 'parquet_gap'),
  'docomo-cycle ' || to_char(pg_temp.window_hour(), 'YYYY-MM-DD HH24') || 'Z',
  '通知に「どの系統のどの時間か」が入る'
);

delete from public.alert_state;
insert into storage.objects (bucket_id, name, metadata)
values ('gbfs-parquet', pg_temp.parquet_name('hellocycling', pg_temp.window_hour()), '{"size":1}'::jsonb);
select is(public.check_parquet_gap() -> 'missing', '1'::jsonb, '置いたぶんは欠落から外れる');

insert into storage.objects (bucket_id, name, metadata)
values ('gbfs-parquet', pg_temp.parquet_name('docomo-cycle', pg_temp.window_hour()), '{"size":1}'::jsonb);
delete from public.alert_state;
select is(public.check_parquet_gap() -> 'missing', '0'::jsonb, '両方置けば欠落なし');
select is(pg_temp.alerts(), '(なし)', '同上（通知も出ない）');

-- **直前の 1 時間は猶予**：毎時ジョブは :07 に前 1 時間を畳むので、:00〜:07 は
-- 「まだ無い」のが正常。猶予が無いと毎時 7 分間だけ誤報が出る
delete from public.status_snapshots; delete from public.alert_state;
select pg_temp.put_snapshot('hellocycling',
  date_trunc('hour', (now() at time zone 'UTC')) - interval '30 minutes');
select is(
  public.check_parquet_gap() -> 'checked', '0'::jsonb,
  '直前の 1 時間は対象にしない（畳む前でも鳴らない）'
);

-- 7 時間より古いものも見ない（全期間の突き合わせは重い）
delete from public.status_snapshots;
select pg_temp.put_snapshot('hellocycling',
  date_trunc('hour', (now() at time zone 'UTC')) - interval '10 hours');
select is(public.check_parquet_gap() -> 'checked', '0'::jsonb, '窓より古い時間は見ない');

-- ────────────────────────────────────────────────────────────────
-- 検査 4：参照データの期限
-- ────────────────────────────────────────────────────────────────
-- 0023 が jp_holidays を作ったので「表が無い」経路はもう通らない。**それでも
-- `to_regclass` の分岐は残す**：0020 だけを当てた環境（本番に 0023 を入れる前）で
-- この関数が落ちないことが、段どうしを独立させる根拠だった
delete from public.status_snapshots; delete from public.alert_state;
delete from public.jp_holidays where true;
select is(
  public.check_reference_data() -> 'skipped', 'true'::jsonb,
  'jp_holidays が空なら飛ばす（0 件を「期限切れ」と読まない）'
);
select is(pg_temp.alerts(), '(なし)', '飛ばしたときは通知しない');

insert into public.jp_holidays values (current_date + 30, 'テスト');
select is(public.check_reference_data() -> 'days_left', '30'::jsonb, '残り日数を数える');
select ok(pg_temp.has_alert('holidays_expiring'), '残り 90 日を切ったら通知する');

delete from public.alert_state;
delete from public.jp_holidays where true;
insert into public.jp_holidays values (current_date + 400, 'テスト');
select is(public.check_reference_data() -> 'alerts', '0'::jsonb, '十分先まであれば鳴らない');

-- ────────────────────────────────────────────────────────────────
-- 検査 6：予測の鮮度（0029）
-- ────────────────────────────────────────────────────────────────
-- **鮮度は `station_forecasts` で測る。`inference_log` では測らない。** 推論が毎回
-- 失敗していても `inference_log` には行が増えるので、そちらを見ると「新しい」に見える。
delete from public.station_forecasts where true;
delete from public.inference_log where true;
delete from public.station_status_latest;
delete from public.status_snapshots;
delete from public.stations;
delete from public.alert_state;
insert into public.stations (system_id, station_id, idx) values
  ('hellocycling', 'a', 0), ('docomo-cycle', 'a', 0);

-- 予測を 1 行置く。**動かすのは鮮度だけ**（値は検査に関係しない）
create function pg_temp.put_forecast(p_system text, p_age interval, p_base interval)
returns void language sql as $$
  insert into public.station_forecasts
    (system_id, station_id, generated_at, base_observed_at, model_version,
     horizons_min, p_bike_x1000, p_dock_x1000, confidence)
  values (p_system, 'a', now() - p_age, now() - p_base, 'test',
          array[5]::smallint[], array[500]::smallint[], array[500]::smallint[], 2)
  on conflict (system_id, station_id) do update
     set generated_at = excluded.generated_at, base_observed_at = excluded.base_observed_at;
$$;

-- **一度も動いていない**は null なので、比較だけでは拾えない
select is(public.check_inference() -> 'alerts', '2'::jsonb, '予測が 1 度も出ていなければ鳴る');
select is(public.check_inference() -> 'checked', '2'::jsonb, '活性なシステムを全部見る');
select ok(pg_temp.has_alert('inference_stale:hellocycling'), '通知の鍵はシステム別');

delete from public.alert_state;
select pg_temp.put_forecast('hellocycling', interval '1 minute', interval '3 minutes');
select pg_temp.put_forecast('docomo-cycle', interval '1 minute', interval '3 minutes');
select is(public.check_inference() -> 'alerts', '0'::jsonb, '新しければ鳴らない');

select pg_temp.put_forecast('hellocycling', interval '16 minutes', interval '18 minutes');
select is(public.check_inference() -> 'alerts', '1'::jsonb, '15 分より古ければ鳴る');
select ok(
  (select last_value ? 'feed_observed_at' and last_value ? 'last_status'
     from public.alert_state where alert_key = 'inference_stale:hellocycling'),
  '**原因の当たりを付ける材料**を payload に入れる（収集の持ち場か推論の持ち場か）'
);

-- ────────────────────────────────────────────────────────────────
-- 再起動：投げれば進むときだけ叩く（0029）
-- ────────────────────────────────────────────────────────────────
-- net.http_get はキューに積むだけで、送信は**コミット後**。rollback するここからは
-- 外へ出ない（0007 と同じ）。
delete from public.job_runs; delete from public.alert_state;
select ok(
  vault.create_secret('dummy-not-a-real-secret', 'cron_secret', 'pgTAP 用') is not null,
  'テスト用の cron_secret を置ける'
);

select pg_temp.put_forecast('hellocycling', interval '1 minute', interval '3 minutes');
select pg_temp.put_forecast('docomo-cycle', interval '1 minute', interval '3 minutes');
update public.feed_state set last_observed_at = now() - interval '1 minute';
select is(public.trigger_infer(), 0, '予測が新しければ叩かない');
select is(
  (select count(*)::int from public.job_runs where job_name = 'trigger_infer'),
  0, '**何もしなかった回は job_runs に書かない**（5 分毎に 1 日 288 行を増やさない）'
);

-- 予測は停滞しているが、**まだ推論していない観測が無い**
select pg_temp.put_forecast('hellocycling', interval '20 minutes', interval '22 minutes');
select pg_temp.put_forecast('docomo-cycle', interval '20 minutes', interval '22 minutes');
update public.feed_state set last_observed_at = now() - interval '22 minutes';
select is(
  public.trigger_infer(), 0,
  '**未推論の観測が無ければ叩かない**（収集の停滞は monitor_feeds と trigger_backup_collect の持ち場）'
);

-- 新しい観測が来た。**ここで初めて「投げれば進む」**
update public.feed_state set last_observed_at = now() - interval '1 minute'
 where system_id = 'hellocycling';
select is(public.trigger_infer(), 1, '未推論の観測があり、予測が停滞していれば叩く');
select is(pg_temp.job_status('trigger_infer'), 'ok', '発火したときは job_runs に残る');
select ok(
  pg_temp.has_alert('infer_watchdog_fired'),
  '**埋め合わせたことを知らせる**（慢性的な配信漏れがウォッチドッグに隠されないよう）'
);

-- ────────────────────────────────────────────────────────────────
-- 閉じられなかった行（0031、§12 の 111）
-- ────────────────────────────────────────────────────────────────
-- 掴んだあと `finish_inference` が失敗すると、行は `running` のまま残る。**それ自体は
-- 正しい**（同じ表に「失敗した」とも書けない）。**誰も見ていない**のが問題だった。
delete from public.alert_state;
select pg_temp.put_forecast('hellocycling', interval '1 minute', interval '3 minutes');
select pg_temp.put_forecast('docomo-cycle', interval '1 minute', interval '3 minutes');
delete from public.inference_log where true;

insert into public.inference_log (system_id, generated_at, base_observed_at, model_version, status)
values ('hellocycling', now() - interval '1 minute', now() - interval '1 minute', 'm1', 'running');
select is(
  public.check_inference() -> 'stuck', '0'::jsonb, '走っている最中の running は数えない'
);

insert into public.inference_log (system_id, generated_at, base_observed_at, model_version, status)
values ('hellocycling', now() - interval '30 minutes', now() - interval '30 minutes', 'm1', 'running');
select is(
  public.check_inference() -> 'stuck', '1'::jsonb,
  '**閾値を過ぎた running は「閉じられなかった」**（5 分周期で maxDuration は 120 秒）'
);
select ok(pg_temp.has_alert('inference_stuck'), '通知する');
select is(
  (select last_value->>'systems' from public.alert_state where alert_key = 'inference_stuck'),
  'hellocycling', 'どのシステムかを payload に入れる'
);

-- ────────────────────────────────────────────────────────────────
-- 予測ログが置けていない（0042、W5 プラン §6.1 の PR A）
-- ────────────────────────────────────────────────────────────────
-- **置けなくても推論は落とさない**（W3-18）ので、`status` は `ok` のままである。
-- 失敗は `detail.forecast_log` にしか出ない——**R19 と同じ「誰も見ていない」形**。
-- 天気ほどではないが期限もある：作り直しには `weather_hourly` の 30 日が要る。
delete from public.inference_log where true;
delete from public.alert_state;

insert into public.inference_log
  (system_id, generated_at, base_observed_at, model_version, status, detail)
values ('hellocycling', now() - interval '1 minute', now() - interval '1 minute',
        'm1', 'ok', '{"forecast_log": "ok"}'::jsonb);
select is(
  public.check_inference() -> 'unlogged', '0'::jsonb, '置けていれば数えない'
);

insert into public.inference_log
  (system_id, generated_at, base_observed_at, model_version, status, detail)
values ('hellocycling', now() - interval '2 minutes', now() - interval '2 minutes',
        'm1', 'ok', '{"forecast_log": "failed:TimeoutError"}'::jsonb);
select is(
  public.check_inference() -> 'unlogged', '1'::jsonb,
  '**配信が ok でもログが置けていなければ数える**（status では気づけない）'
);
select ok(pg_temp.has_alert('forecast_log_failed'), '通知する');
select is(
  (select last_value->>'last_error' from public.alert_state
    where alert_key = 'forecast_log_failed'),
  'failed:TimeoutError',
  '**理由まで載せる**（W4 の PR K：「1 回失敗しました」では動けない）'
);
select ok(
  (select last_value->>'message' like '%作り直す%' from public.alert_state
    where alert_key = 'forecast_log_failed'),
  '**何が失われるかを書く**（配信は無事で、失われるのはその時刻の記録）'
);

-- **3 時間より前は数えない**（古い失敗で鳴り続けない）
insert into public.inference_log
  (system_id, generated_at, base_observed_at, model_version, status, detail)
values ('docomo-cycle', now() - interval '4 hours', now() - interval '4 hours',
        'm1', 'ok', '{"forecast_log": "failed:SupabaseError"}'::jsonb);
select is(
  public.check_inference() -> 'unlogged', '1'::jsonb, '窓は直近 3 時間'
);

-- **欄が無い回は数えない。** マイグレーションを先に出してもコードが古ければ欄は無い
insert into public.inference_log
  (system_id, generated_at, base_observed_at, model_version, status, detail)
values ('docomo-cycle', now() - interval '3 minutes', now() - interval '3 minutes',
        'm1', 'ok', '{"rows": 100}'::jsonb);
select is(
  public.check_inference() -> 'unlogged', '1'::jsonb,
  '**欄が無い回は「置けなかった」と数えない**（デプロイの順序で鳴らない）'
);

-- 親の検査に持ち込まないよう、片づけてから戻す
delete from public.inference_log where true;
delete from public.alert_state;
select pg_temp.put_forecast('hellocycling', interval '1 minute', interval '3 minutes');
select pg_temp.put_forecast('docomo-cycle', interval '1 minute', interval '3 minutes');

-- ────────────────────────────────────────────────────────────────
-- 親：検査どうしが独立していること
-- ────────────────────────────────────────────────────────────────
delete from public.job_runs; delete from public.alert_state;
select lives_ok('select public.monitor_jobs()', '正常時は例外を投げない');
select is(pg_temp.job_status('monitor_jobs'), 'ok', '正常時の status は ok');
select is(
  (select jsonb_object_keys_count from (
     select count(*)::int as jsonb_object_keys_count
       from jsonb_object_keys((select detail->'checks' from public.job_runs
                                where job_name='monitor_jobs' order by id desc limit 1))) t),
  6, '6 つの検査すべてが detail に残る（0021 で check_cron_jobs、0029 で check_inference）'
);

-- **1 つ壊しても他は走る。** 0009 の monitor_feeds は全検査を 1 つの exception で
-- 包んでいるため、この性質を持たない（だから別関数にした。W3-02）
delete from public.job_runs; delete from public.alert_state;
alter table public.monitored_jobs rename to monitored_jobs_hidden;
select lives_ok('select public.monitor_jobs()', '検査が 1 つ壊れても親は例外を投げない');
select is(
  (select detail->'checks'->'check_jobs_missing'->>'code' from public.job_runs
    where job_name='monitor_jobs' order by id desc limit 1),
  '42P01', '壊れた検査の SQLSTATE が detail に残る'
);
select is(
  (select detail->'checks'->'check_parquet_gap'->>'missing' from public.job_runs
    where job_name='monitor_jobs' order by id desc limit 1),
  '0', '壊れていない検査は走り続ける'
);
select is(pg_temp.job_status('monitor_jobs'), 'failed', '1 つでも壊れたら status は failed');
select ok(
  pg_temp.has_alert('monitor_check_failed:check_jobs_missing'),
  '壊れた検査そのものを通知する'
);
alter table public.monitored_jobs_hidden rename to monitored_jobs;

-- アドバイザリロックの番号を固定する。0009・0011・0013 が 1〜5 を使っている（W1-30）
select is(
  (select count(*)::int from pg_locks
    where locktype = 'advisory' and classid = 8423 and objid = 6 and pid = pg_backend_pid()),
  1, 'monitor_jobs は (8423, 6) を取る（他のジョブとぶつからない）'
);

select * from finish();
rollback;
