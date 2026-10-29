-- pgTAP: 森の鮮度と shadow の失敗の見張り（マイグレーション 0058、W8 プランの W8-12）
--
-- **主題は「鳴るべきときにだけ鳴ること」**（0020 と同じ）。鳴り過ぎれば読まれなくなり、
-- 鳴らなければ黙って弱っていくのを見逃す。だから境目を両側から挟む。
--
--   * 森：41 日は鳴らない・42 日で鳴る。**合成器の鮮度（0048）が鳴らない日に、森だけが鳴る**
--   * shadow：今の shadow の配った周期が 3 つ続けて健全でないときだけ鳴る。**昇格の直後に、
--     前の周期で鳴らない**
--
-- `now()` は取引の中で固定される（0011 と同じ前提）。日付と時刻は「今から何日・何分前か」で作る。
-- pg_net の送信はコミット後なので、rollback するここでは Webhook に届かない（0011 と同じ）。

begin;
select plan(58);

-- 自分の前提を作る
delete from public.alert_state;
delete from public.model_daily_metrics;
delete from public.model_versions;
delete from public.inference_log where true;
delete from vault.secrets where name in ('cron_secret', 'alert_webhook_url');

create function pg_temp.jst_today() returns date language sql stable as $$
  select (timezone('Asia/Tokyo', now()))::date;
$$;

-- 森の部品の記録。`p_days` は「何日前か」で、**並びは呼ぶ側が決める**（順序の試験で使う）
create function pg_temp.forest(variadic p_days integer[]) returns jsonb language sql stable as $$
  select jsonb_build_object(
    'model_version', 'lgbm-v1-test',
    'train_days', (select jsonb_agg((pg_temp.jst_today() - d)::text order by o)
                     from unnest(p_days) with ordinality as u(d, o)));
$$;

-- 配信中の合成器を置く。**合成器の `train_days` は今日まで**（B3 を毎週当てはめ直している形）
create function pg_temp.composite(p_forest jsonb) returns void language sql as $$
  delete from public.model_versions where status = 'active';
  insert into public.model_versions
    (model_version, kind, status, feature_set, artifact_path, train_days, metrics)
  values ('composite-v1-test', 'composite', 'active', 'v1', 'composite/test.json.gz',
          array[pg_temp.jst_today()::text],
          jsonb_build_object('components', jsonb_build_object('lightgbm', p_forest)));
$$;

create function pg_temp.shadow(p_version text) returns void language sql as $$
  delete from public.model_versions where status = 'shadow';
  insert into public.model_versions
    (model_version, kind, status, feature_set, artifact_path, train_days)
  values (p_version, 'baseline', 'shadow', 'v3', 'baseline/' || p_version || '.json.gz',
          array[pg_temp.jst_today()::text]);
$$;

-- shadow を歩いた 1 回の `detail.shadow`。**`p_version` に null を渡すと「行を引けなかった」形**
create function pg_temp.walked(p_status text, p_log text default 'ok',
                               p_version text default 'baseline-b3-v0-shadow')
returns jsonb language sql immutable as $$
  select jsonb_build_object('model_version', p_version, 'status', p_status,
                            'forecast_log', p_log, 'model_load_ms', 0, 'predict_ms', 120);
$$;

-- `p_ago_min` 分前に始まった推論の 1 周期。`p_shadow` が null なら `detail` に shadow の欄を出さない
create function pg_temp.cycle(p_system text, p_ago_min integer, p_shadow jsonb,
                              p_status text default 'ok')
returns void language sql as $$
  insert into public.inference_log
    (system_id, generated_at, finished_at, base_observed_at, model_version, status, detail)
  values (p_system, now() - make_interval(mins => p_ago_min),
          now() - make_interval(mins => p_ago_min) + interval '20 seconds',
          now() - make_interval(mins => p_ago_min), 'composite-v1-test', p_status,
          case when p_shadow is null then '{}'::jsonb
               else jsonb_build_object('shadow', p_shadow) end);
$$;

-- 周期を消し、通知の抑制も消して始め直す
create function pg_temp.reset_cycles() returns void language sql as $$
  delete from public.inference_log where true;
  delete from public.alert_state;
$$;

create function pg_temp.has_alert(p_key text) returns boolean language sql as $$
  select exists (select 1 from public.alert_state where alert_key = p_key);
$$;

-- ────────────────────────────────────────────────────────────────
-- 関数の形と権限（0011 の作法）
-- ────────────────────────────────────────────────────────────────
select has_function('public', 'check_forest_freshness', 'check_forest_freshness がある');
select has_function('public', 'check_shadow', 'check_shadow がある');
select is(
  (select bool_and(p.prosecdef) from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname in ('check_forest_freshness', 'check_shadow')),
  true, '2 つとも security definer'
);
select is(
  (select bool_and(p.proconfig @> array['search_path=""']) from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname in ('check_forest_freshness', 'check_shadow')),
  true, '2 つとも search_path = ''''（スキーマ付きで書く前提）'
);
select is(
  has_function_privilege('anon', 'public.check_forest_freshness()', 'execute'),
  false, '匿名は check_forest_freshness を実行できない'
);
select is(
  has_function_privilege('anon', 'public.check_shadow()', 'execute'),
  false, '匿名は check_shadow を実行できない'
);
select is(
  (select value from public.app_config where key = 'forest_stale_days'),
  '42', '森の閾値は 42 日（学習の窓 35 日 ＋ 1 週）'
);
select is(
  (select value from public.app_config where key = 'shadow_fail_cycles'),
  '3', 'shadow の閾値は 3 周期'
);

-- ────────────────────────────────────────────────────────────────
-- 森：合成器でなければ何もしない
-- ────────────────────────────────────────────────────────────────
select is(public.check_forest_freshness() -> 'alerts', '0'::jsonb,
  'active が無ければ鳴らない（model_missing は 0048 が鳴らす）');
select is(public.check_forest_freshness() -> 'forest', 'null'::jsonb, 'active が無ければ森も無い');

insert into public.model_versions
  (model_version, kind, status, feature_set, artifact_path, train_days)
values ('baseline-b3-v0-test', 'baseline', 'active', 'v3', 'baseline/test.json.gz',
        array[(pg_temp.jst_today() - 60)::text]);
select is(public.check_forest_freshness() -> 'forest', 'null'::jsonb,
  'B3 だけを配っているなら森は無い');
select is(public.check_forest_freshness() -> 'alerts', '0'::jsonb,
  'B3 だけなら、古くても森の警報は鳴らない（B3 の古さは 0048）');

-- ────────────────────────────────────────────────────────────────
-- 森：境目（41 日は鳴らない・42 日で鳴る）
-- ────────────────────────────────────────────────────────────────
delete from public.alert_state;
select pg_temp.composite(pg_temp.forest(68, 41));
select is(public.check_forest_freshness() -> 'age_days', '41'::jsonb, '森の最終学習日は 41 日前');
select is(public.check_forest_freshness() -> 'alerts', '0'::jsonb, '**41 日は鳴らない**');

delete from public.alert_state;
select pg_temp.composite(pg_temp.forest(69, 42));
select is(public.check_forest_freshness() -> 'alerts', '1'::jsonb,
  '**42 日で鳴る**（当てはめ直す窓が入れ替わり、さらに 1 週）');
select is(public.check_model_freshness() -> 'alerts', '0'::jsonb,
  '**同じ合成器で、0048 の鮮度は鳴らない**（train_days は B3 の今日まで。これが 0058 の理由）');
select is(
  (select last_value ->> 'forest_version' from public.alert_state where alert_key = 'forest_stale'),
  'lgbm-v1-test', '通知にどの森かが入る'
);
select is(
  (select last_value ->> 'model_version' from public.alert_state where alert_key = 'forest_stale'),
  'composite-v1-test', '通知にどの合成器かが入る'
);
select is(
  (select last_value ->> 'age_days' from public.alert_state where alert_key = 'forest_stale'),
  '42', '通知に何日古いかが入る'
);

-- 抑制：6 時間に 1 回（監視は 5 分毎）
select is(public.check_forest_freshness() -> 'alerts', '0'::jsonb, '続けて呼んでも 2 通目は出ない');
update public.alert_state set last_sent_at = now() - interval '7 hours'
 where alert_key = 'forest_stale';
select is(public.check_forest_freshness() -> 'alerts', '1'::jsonb, '6 時間を越えれば また鳴る');

-- 最終学習日は max で採る（並び順に頼らない）
delete from public.alert_state;
select pg_temp.composite(pg_temp.forest(0, 50));
select is((public.check_forest_freshness() ->> 'last_train_day')::date, pg_temp.jst_today(),
  '並びが昇順でなくても最新の日を採る');
select is(public.check_forest_freshness() -> 'alerts', '0'::jsonb, '最新の日で判定するので鳴らない');

-- 閾値は app_config から読む
delete from public.alert_state;
select pg_temp.composite(pg_temp.forest(47, 20));
select is(public.check_forest_freshness() -> 'alerts', '0'::jsonb, '既定（42 日）では 20 日は鳴らない');
update public.app_config set value = '15' where key = 'forest_stale_days';
select is(public.check_forest_freshness() -> 'alerts', '1'::jsonb, '閾値を 15 日に縮めると同じ行で鳴る');
update public.app_config set value = '42' where key = 'forest_stale_days';

-- **森の欄が読めない合成器は例外**（登録側の異常。「古い」とは別の報せ方）
select pg_temp.composite(jsonb_build_object('model_version', 'lgbm-v1-test'));
select throws_ok('select public.check_forest_freshness()', '22023', null,
  '森の train_days が無い合成器は 22023 の例外');
select pg_temp.composite(jsonb_build_object('model_version', 'lgbm-v1-test',
                                            'train_days', jsonb_build_array('2026-13-45')));
select throws_ok('select public.check_forest_freshness()', null, null,
  '日付でない train_days も例外（古い日として読まない）');

-- 親の中では、森が壊れても B3 の鮮度は走る（検査ごとに例外を切り分ける。0020）
delete from public.job_runs; delete from public.alert_state;
select pg_temp.composite(jsonb_build_object('model_version', 'lgbm-v1-test'));
select lives_ok('select public.monitor_jobs()', '森の欄が壊れていても monitor_jobs は例外を投げない');
select is(
  (select detail -> 'checks' -> 'check_forest_freshness' ->> 'code' from public.job_runs
    where job_name = 'monitor_jobs' order by id desc limit 1),
  '22023', '壊れた森の検査の SQLSTATE が detail に残る'
);
select is(
  (select detail -> 'checks' -> 'check_model_freshness' ->> 'age_days' from public.job_runs
    where job_name = 'monitor_jobs' order by id desc limit 1),
  '0', 'B3 の鮮度（0048）は走り続ける'
);
select ok(pg_temp.has_alert('monitor_check_failed:check_forest_freshness'),
  '壊れた森の検査そのものを通知する');

-- ────────────────────────────────────────────────────────────────
-- shadow：登録されていなければ何もしない
-- ────────────────────────────────────────────────────────────────
select pg_temp.composite(pg_temp.forest(27, 0));
select pg_temp.reset_cycles();
-- 前の shadow が落ちた記録だけが残っている
select pg_temp.cycle('hellocycling', 1, pg_temp.walked('failed:RuntimeError', '', 'old-b3'));
select pg_temp.cycle('hellocycling', 6, pg_temp.walked('failed:RuntimeError', '', 'old-b3'));
select pg_temp.cycle('hellocycling', 11, pg_temp.walked('failed:RuntimeError', '', 'old-b3'));
select is(public.check_shadow() -> 'shadow', 'null'::jsonb, 'shadow が無いと報告する');
select is(public.check_shadow() -> 'alerts', '0'::jsonb,
  'shadow が登録されていなければ、残った失敗の記録では鳴らない');

-- ────────────────────────────────────────────────────────────────
-- shadow：健全なら鳴らない・3 つ続けて落ちたら鳴る
-- ────────────────────────────────────────────────────────────────
select pg_temp.shadow('baseline-b3-v0-shadow');
select pg_temp.reset_cycles();
select pg_temp.cycle('hellocycling', 1, pg_temp.walked('ok'));
select pg_temp.cycle('hellocycling', 6, pg_temp.walked('ok'));
select pg_temp.cycle('hellocycling', 11, pg_temp.walked('ok'));
select is(public.check_shadow() -> 'alerts', '0'::jsonb, '健全な 3 周期では鳴らない');
select is(public.check_shadow() -> 'checked', '2'::jsonb, '動いている系統を 2 つとも見る');

select pg_temp.reset_cycles();
select pg_temp.cycle('hellocycling', 1, pg_temp.walked('failed:RuntimeError', ''));
select pg_temp.cycle('hellocycling', 6, pg_temp.walked('failed:RuntimeError', ''));
select pg_temp.cycle('hellocycling', 11, pg_temp.walked('failed:MemoryError', ''));
select is(public.check_shadow() -> 'alerts', '1'::jsonb, '**3 周期続けて落ちたら鳴る**');
select ok(pg_temp.has_alert('shadow_failing:hellocycling'), '鍵は系統ごと（shadow_failing:<系統>）');
select ok(not pg_temp.has_alert('shadow_failing:docomo-cycle'), '周期の無い系統では鳴らない');
select is(
  (select last_value -> 'outcomes' from public.alert_state
    where alert_key = 'shadow_failing:hellocycling'),
  '["failed:RuntimeError", "failed:RuntimeError", "failed:MemoryError"]'::jsonb,
  '通知に、新しい順の 3 周期の結果が入る'
);

-- 抑制：6 時間に 1 回
select is(public.check_shadow() -> 'alerts', '0'::jsonb, '続けて呼んでも 2 通目は出ない');
update public.alert_state set last_sent_at = now() - interval '7 hours'
 where alert_key = 'shadow_failing:hellocycling';
select is(public.check_shadow() -> 'alerts', '1'::jsonb, '6 時間を越えれば また鳴る');

-- ────────────────────────────────────────────────────────────────
-- shadow：続いていなければ鳴らない
-- ────────────────────────────────────────────────────────────────
select pg_temp.reset_cycles();
select pg_temp.cycle('hellocycling', 1, pg_temp.walked('ok'));
select pg_temp.cycle('hellocycling', 6, pg_temp.walked('failed:RuntimeError', ''));
select pg_temp.cycle('hellocycling', 11, pg_temp.walked('failed:RuntimeError', ''));
select is(public.check_shadow() -> 'alerts', '0'::jsonb, 'いちばん新しい周期が健全なら鳴らない');

select pg_temp.reset_cycles();
select pg_temp.cycle('hellocycling', 1, pg_temp.walked('failed:RuntimeError', ''));
select pg_temp.cycle('hellocycling', 6, pg_temp.walked('ok'));
select pg_temp.cycle('hellocycling', 11, pg_temp.walked('failed:RuntimeError', ''));
select is(public.check_shadow() -> 'alerts', '0'::jsonb, '間に健全な周期があれば鳴らない');

select pg_temp.reset_cycles();
select pg_temp.cycle('hellocycling', 1, pg_temp.walked('failed:RuntimeError', ''));
select pg_temp.cycle('hellocycling', 6, pg_temp.walked('failed:RuntimeError', ''));
select is(public.check_shadow() -> 'alerts', '0'::jsonb, '周期が 2 つしか無ければ鳴らない');

-- ────────────────────────────────────────────────────────────────
-- shadow：昇格の直後に、前の周期で鳴らない
-- ────────────────────────────────────────────────────────────────
-- shadow の上げ下げは推論の合間に psql で行う。**上げた直後の監視は、上げる前の周期を読む**
select pg_temp.reset_cycles();
select pg_temp.cycle('hellocycling', 1, pg_temp.walked('failed:RuntimeError', ''));
select pg_temp.cycle('hellocycling', 6, pg_temp.walked('failed:RuntimeError', ''));
select pg_temp.cycle('hellocycling', 11, null);
select is(public.check_shadow() -> 'alerts', '0'::jsonb,
  'shadow の欄が無い周期（shadow が無かった頃）が混ざれば、判断しない');

select pg_temp.reset_cycles();
select pg_temp.cycle('hellocycling', 1, pg_temp.walked('failed:RuntimeError', '', 'old-b3'));
select pg_temp.cycle('hellocycling', 6, pg_temp.walked('failed:RuntimeError', '', 'old-b3'));
select pg_temp.cycle('hellocycling', 11, pg_temp.walked('failed:RuntimeError', '', 'old-b3'));
select is(public.check_shadow() -> 'alerts', '0'::jsonb,
  '**前の shadow の失敗で、今の shadow を責めない**（別の版を歩いた周期は判断しない）');

-- ────────────────────────────────────────────────────────────────
-- shadow：「健全でない」に数えるもの
-- ────────────────────────────────────────────────────────────────
select pg_temp.reset_cycles();
select pg_temp.cycle('hellocycling', 1, pg_temp.walked('failed:APIError', '', null));
select pg_temp.cycle('hellocycling', 6, pg_temp.walked('failed:APIError', '', null));
select pg_temp.cycle('hellocycling', 11, pg_temp.walked('failed:APIError', '', null));
select is(public.check_shadow() -> 'alerts', '1'::jsonb,
  'shadow の行を引けなかった周期（版が null）は、今の shadow の失敗に数える');

select pg_temp.reset_cycles();
select pg_temp.cycle('hellocycling', 1, pg_temp.walked('ok', 'failed:HTTPStatusError'));
select pg_temp.cycle('hellocycling', 6, pg_temp.walked('ok', 'failed:HTTPStatusError'));
select pg_temp.cycle('hellocycling', 11, pg_temp.walked('ok', 'failed:HTTPStatusError'));
select is(public.check_shadow() -> 'alerts', '1'::jsonb,
  '**歩けても予測ログを置けなければ数える**（日次評価が比べられない）');
select is(
  (select last_value -> 'outcomes' ->> 0 from public.alert_state
    where alert_key = 'shadow_failing:hellocycling'),
  'forecast_log:failed:HTTPStatusError', '通知には予測ログのほうの失敗を書く'
);

select pg_temp.reset_cycles();
select pg_temp.cycle('hellocycling', 1, pg_temp.walked('skipped:no_profile', ''));
select pg_temp.cycle('hellocycling', 6, pg_temp.walked('skipped:no_profile', ''));
select pg_temp.cycle('hellocycling', 11, pg_temp.walked('skipped:no_profile', ''));
select is(public.check_shadow() -> 'alerts', '1'::jsonb,
  'プロファイルを読めずに歩かなかった周期（契約 33）も数える');

-- **active が落ちた周期は数えない**（shadow を歩かない。そちらは check_inference）
select pg_temp.reset_cycles();
select pg_temp.cycle('hellocycling', 1, null, 'failed');
select pg_temp.cycle('hellocycling', 6, pg_temp.walked('failed:RuntimeError', ''));
select pg_temp.cycle('hellocycling', 11, pg_temp.walked('failed:RuntimeError', ''));
select pg_temp.cycle('hellocycling', 16, pg_temp.walked('failed:RuntimeError', ''));
select is(public.check_shadow() -> 'alerts', '1'::jsonb,
  'active が落ちた周期は飛ばし、配った周期だけで数える');

-- 系統ごとに判断する
select pg_temp.reset_cycles();
select pg_temp.cycle('hellocycling', 1, pg_temp.walked('ok'));
select pg_temp.cycle('hellocycling', 6, pg_temp.walked('ok'));
select pg_temp.cycle('hellocycling', 11, pg_temp.walked('ok'));
select pg_temp.cycle('docomo-cycle', 3, pg_temp.walked('failed:KeyError', ''));
select pg_temp.cycle('docomo-cycle', 8, pg_temp.walked('failed:KeyError', ''));
select pg_temp.cycle('docomo-cycle', 13, pg_temp.walked('failed:KeyError', ''));
select is(public.check_shadow() -> 'alerts', '1'::jsonb, '片方の系統だけ落ちても鳴る');
select is(
  (select string_agg(alert_key, ',') from public.alert_state where alert_key like 'shadow_failing:%'),
  'shadow_failing:docomo-cycle', '鳴るのは落ちた系統の鍵だけ'
);

-- 閾値は app_config から読む
select pg_temp.reset_cycles();
select pg_temp.cycle('hellocycling', 1, pg_temp.walked('failed:RuntimeError', ''));
select pg_temp.cycle('hellocycling', 6, pg_temp.walked('failed:RuntimeError', ''));
select pg_temp.cycle('hellocycling', 11, pg_temp.walked('failed:RuntimeError', ''));
update public.app_config set value = '5' where key = 'shadow_fail_cycles';
select is(public.check_shadow() -> 'alerts', '0'::jsonb, '閾値を 5 周期にすると、3 周期では鳴らない');
update public.app_config set value = '3' where key = 'shadow_fail_cycles';

-- ────────────────────────────────────────────────────────────────
-- 親に組み込まれている
-- ────────────────────────────────────────────────────────────────
delete from public.job_runs;
select pg_temp.reset_cycles();
select lives_ok('select public.monitor_jobs()', 'monitor_jobs は例外を投げない');
select is(
  (select count(*)::int
     from jsonb_object_keys((select detail -> 'checks' from public.job_runs
                              where job_name = 'monitor_jobs' order by id desc limit 1))),
  11, '検査は 11 になった（0058 の check_forest_freshness・check_shadow）'
);
select is(
  (select detail -> 'checks' -> 'check_forest_freshness' ->> 'forest' from public.job_runs
    where job_name = 'monitor_jobs' order by id desc limit 1),
  'lgbm-v1-test', 'check_forest_freshness が detail に出る'
);
select is(
  (select detail -> 'checks' -> 'check_shadow' ->> 'shadow' from public.job_runs
    where job_name = 'monitor_jobs' order by id desc limit 1),
  'baseline-b3-v0-shadow', 'check_shadow が detail に出る'
);

select * from finish();
rollback;
