-- pgTAP: 配信中のモデルの古さ（W5 プラン §6.13、マイグレーション 0048）
--
-- **主題は「鳴るべき日にだけ鳴ること」。** 見張りの値打ちは境目にしか無い——
-- 1 日早ければ毎週鳴って読まれなくなり、1 日遅ければ回し忘れを 1 週間見逃す。
-- だから 7 日（鳴らない）と 8 日（鳴る）を両側から挟む。
--
-- **`now()` はトランザクションの中で固定される**（0011 と同じ前提）。日付はすべて
-- 「JST の今日から何日前か」で作るので、いつ走らせても同じ結果になる。

begin;
select plan(24);

-- 自分の前提を作る
delete from public.alert_state;
delete from public.model_versions;
delete from vault.secrets where name in ('cron_secret', 'alert_webhook_url');

create function pg_temp.jst_today() returns date language sql stable as $$
  select (timezone('Asia/Tokyo', now()))::date;
$$;

-- `p_days` は「最終学習日が何日前か」。**日付の並びは呼ぶ側が決める**（順序の試験で使う）
create function pg_temp.active(variadic p_days integer[]) returns void language sql as $$
  delete from public.model_versions;
  insert into public.model_versions
    (model_version, kind, status, feature_set, artifact_path, train_days)
  values ('baseline-b3-v0-test', 'baseline', 'active', 'v3', 'baselines/test.json.gz',
          (select array_agg((pg_temp.jst_today() - d)::text order by o)
             from unnest(p_days) with ordinality as u(d, o)));
$$;

create function pg_temp.fresh() returns jsonb language sql as $$
  select public.check_model_freshness();
$$;

-- ────────────────────────────────────────────────────────────────
-- 関数の形と権限（0011 の 6 つと同じ作法）
-- ────────────────────────────────────────────────────────────────
select has_function('public', 'check_model_freshness', 'check_model_freshness がある');
select is(
  (select p.prosecdef from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'check_model_freshness'),
  true, 'security definer'
);
select is(
  (select p.proconfig @> array['search_path=""'] from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'check_model_freshness'),
  true, 'search_path = ''''（スキーマ付きで書く前提）'
);
select is(
  has_function_privilege('anon', 'public.check_model_freshness()', 'execute'),
  false, '匿名は実行できない'
);

-- 閾値は app_config に置いてある（周期を変えたら migration 無しで追随できる）
select is(
  (select value from public.app_config where key = 'model_stale_days'),
  '8', '既定の閾値は 8 日（週次 ＋ 1 日の余白）'
);

-- ────────────────────────────────────────────────────────────────
-- active が 1 つも無い
-- ────────────────────────────────────────────────────────────────
-- **これは「古い」とは別の事象。** 推論が版を引けずに止まるので、原因を言えるのは
-- ここだけである（check_inference の文面は「予測が古い」になる）
select is(pg_temp.fresh() -> 'alerts', '1'::jsonb, 'active が無ければ鳴る');
select is(pg_temp.fresh() -> 'active', 'null'::jsonb, 'active は null と報告する');
select ok(
  (select exists (select 1 from public.alert_state where alert_key = 'model_missing')),
  '鍵は model_missing（古いときとは別の鍵）'
);

-- ────────────────────────────────────────────────────────────────
-- 境目：7 日は鳴らない・8 日は鳴る
-- ────────────────────────────────────────────────────────────────
delete from public.alert_state;
select pg_temp.active(0);
select is(pg_temp.fresh() -> 'age_days', '0'::jsonb, '今日まで当てはめたなら 0 日');
select is(pg_temp.fresh() -> 'alerts', '0'::jsonb, '当日は鳴らない');

delete from public.alert_state;
select pg_temp.active(7);
select is(pg_temp.fresh() -> 'age_days', '7'::jsonb, '7 日前まで');
select is(pg_temp.fresh() -> 'alerts', '0'::jsonb,
  '**7 日は鳴らない**（週次で回している正常の上限。ここで鳴ると毎週鳴る）');

delete from public.alert_state;
select pg_temp.active(8);
select is(pg_temp.fresh() -> 'alerts', '1'::jsonb,
  '**8 日で鳴る**（週次を 1 回飛ばした、だけが意味になる）');
select is(
  (select last_value ->> 'model_version' from public.alert_state where alert_key = 'model_stale'),
  'baseline-b3-v0-test', '通知にどの版かが入る'
);
select is(
  (select last_value ->> 'age_days' from public.alert_state where alert_key = 'model_stale'),
  '8', '通知に何日古いかが入る'
);

-- ────────────────────────────────────────────────────────────────
-- 抑制：6 時間に 1 回
-- ────────────────────────────────────────────────────────────────
-- 監視は 5 分毎に回るので、抑制が効かなければ 1 日 288 通になる
select is(pg_temp.fresh() -> 'alerts', '0'::jsonb, '続けて呼んでも 2 通目は出ない');
update public.alert_state set last_sent_at = now() - interval '7 hours'
 where alert_key = 'model_stale';
select is(pg_temp.fresh() -> 'alerts', '1'::jsonb, '6 時間を越えれば また鳴る');

-- ────────────────────────────────────────────────────────────────
-- 最終学習日は max で採る（並び順に頼らない）
-- ────────────────────────────────────────────────────────────────
-- **新しい日を末尾以外に置く。** 末尾の要素を読む実装なら「30 日前まで」と読んで鳴る
delete from public.alert_state;
select pg_temp.active(0, 30);
select is(
  (pg_temp.fresh() ->> 'last_train_day')::date,
  pg_temp.jst_today(), '並びが昇順でなくても最新の日を採る'
);
select is(pg_temp.fresh() -> 'alerts', '0'::jsonb, '最新の日で判定するので鳴らない');

-- ────────────────────────────────────────────────────────────────
-- 閾値は app_config から読む
-- ────────────────────────────────────────────────────────────────
delete from public.alert_state;
select pg_temp.active(4);
select is(pg_temp.fresh() -> 'alerts', '0'::jsonb, '既定（8 日）では 4 日は鳴らない');
update public.app_config set value = '3' where key = 'model_stale_days';
select is(pg_temp.fresh() -> 'alerts', '1'::jsonb, '閾値を 3 日に縮めると同じ行で鳴る');
update public.app_config set value = '8' where key = 'model_stale_days';

-- ────────────────────────────────────────────────────────────────
-- 親に組み込まれている
-- ────────────────────────────────────────────────────────────────
delete from public.job_runs; delete from public.alert_state;
select pg_temp.active(0);
select lives_ok('select public.monitor_jobs()', 'monitor_jobs は例外を投げない');
select is(
  (select count(*)::int
     from jsonb_object_keys((select detail->'checks' from public.job_runs
                              where job_name = 'monitor_jobs' order by id desc limit 1))),
  7, '検査は 7 つになった（0048 で check_model_freshness を足した）'
);
select ok(
  (select detail->'checks' ? 'check_model_freshness' from public.job_runs
    where job_name = 'monitor_jobs' order by id desc limit 1),
  'check_model_freshness が detail に出る'
);

select * from finish();
rollback;
