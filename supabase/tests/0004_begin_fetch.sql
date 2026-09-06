-- pgTAP: begin_fetch（W1 プラン §6.5、§11.3、§5 の 6）
--
-- 守りたいのは 1 つ：**同時に走る 2 つの実行が両方通過しないこと**。
-- 「読んでから判断」だとそれが起きる。1 文の UPDATE で claim していることを、
-- 「直後の 2 回目が false になる」という形で確かめる。

begin;
select plan(14);

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
-- claim の基本
-- ────────────────────────────────────────────────────────────────
select is(
  public.begin_fetch('hellocycling')->>'claimed', 'true',
  '初回は claim できる'
);
select is(
  public.begin_fetch('hellocycling')->>'claimed', 'false',
  '直後の 2 回目は claim できない（Cron の二重配信を無害化する）'
);
select is(
  public.begin_fetch('docomo-cycle')->>'claimed', 'true',
  'システムごとに独立している'
);

-- last_fetch_at を手で戻して「31 秒後」を再現する
update public.feed_state set last_fetch_at = now() - interval '31 seconds'
 where system_id = 'hellocycling';
select is(
  public.begin_fetch('hellocycling')->>'claimed', 'true',
  '既定の 30 秒を過ぎれば再び claim できる'
);

update public.feed_state set last_fetch_at = now() - interval '29 seconds'
 where system_id = 'hellocycling';
select is(
  public.begin_fetch('hellocycling')->>'claimed', 'false',
  '29 秒では claim できない（境界の手前）'
);
select is(
  public.begin_fetch('hellocycling', 10)->>'claimed', 'true',
  '間隔を短く指定すれば claim できる（ウォッチドッグ用）'
);

-- ────────────────────────────────────────────────────────────────
-- claim すると last_fetch_at が進む
-- ────────────────────────────────────────────────────────────────
update public.feed_state set last_fetch_at = null where system_id = 'hellocycling';
select ok(
  (select last_fetch_at is null from public.feed_state where system_id = 'hellocycling'),
  '初期状態では last_fetch_at が null'
);
select is(
  public.begin_fetch('hellocycling')->>'claimed', 'true',
  'last_fetch_at が null なら claim できる（初回取得）'
);
select ok(
  (select last_fetch_at > now() - interval '5 seconds'
     from public.feed_state where system_id = 'hellocycling'),
  'claim すると last_fetch_at が現在時刻に進む'
);

-- ────────────────────────────────────────────────────────────────
-- 戻り値に条件付き要求の材料が入る（往復を 1 回にまとめる）
-- ────────────────────────────────────────────────────────────────
update public.feed_state
   set last_etag = 'W/"400ede-xyz"',
       last_observed_at = '2026-09-06T06:51:33Z',
       last_fetch_at = null
 where system_id = 'docomo-cycle';

select is(
  public.begin_fetch('docomo-cycle')->>'last_etag', 'W/"400ede-xyz"',
  'ETag を返す（If-None-Match に使う）'
);
-- now() はトランザクション内で固定されるため、claim 済みの行は間隔 0 でも再 claim できない。
-- 実運用では毎回別トランザクションなので起きないが、テストでは明示的に戻す
update public.feed_state set last_fetch_at = null where system_id = 'docomo-cycle';
select is(
  (public.begin_fetch('docomo-cycle')->>'last_observed_at')::timestamptz,
  '2026-09-06T06:51:33Z'::timestamptz,
  'last_observed_at を返す（後退したスナップショットの判定に使う）'
);

-- ────────────────────────────────────────────────────────────────
-- begin_fetch は他の列を書かない（§11.3 の書き込み規律）
-- ────────────────────────────────────────────────────────────────
select is(
  (select last_etag from public.feed_state where system_id = 'docomo-cycle'),
  'W/"400ede-xyz"',
  'claim は last_etag を書き換えない（書くのは ingest_snapshot だけ）'
);
select ok(
  (select last_success_at is null from public.feed_state where system_id = 'docomo-cycle'),
  'claim は last_success_at を書かない（書くのは finish_fetch だけ）'
);

-- ────────────────────────────────────────────────────────────────
-- 未知のシステム
-- ────────────────────────────────────────────────────────────────
select throws_ok(
  $$select public.begin_fetch('unknown-system')$$,
  '22023', null,
  '未知のシステムは claim=false ではなく例外にする（設定ミスを黙って流さない）'
);

select * from finish();
rollback;
