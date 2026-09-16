-- pgTAP: 実運用の日次評価（マイグレーション 0050、W5 プラン §6.12 の PR L）
--
-- ここで固定したい契約は 6 つ。
--   1. **匿名は触れない**（表も RPC も。CLAUDE.md §5）
--   2. **同じ日を 2 回入れても行が増えない**（主キーで衝突する。完了条件 2）
--   3. **2 度目は値が入れ替わる**（古い数字が残らない）
--   4. **版・システム・ターゲット・バケツは検査で縛る**（`n > 0`・確率は 0〜1）
--   5. **成績の残っている版は消せない**（外部キーは意図したもの）
--   6. **見張りに入っている**（`evaluate_daily` が 30 時間で鳴る。完了条件 4）
--
-- 前提条件はこのテストが自分で作る。**行単位の検査は必ず `model_version` で絞る。**

begin;
select plan(21);

-- ────────────────────────────────────────────────────────────────
-- 形と権限
-- ────────────────────────────────────────────────────────────────
select has_table('public', 'model_daily_metrics', 'model_daily_metrics がある');
select is(
  (select relrowsecurity
     from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relname = 'model_daily_metrics'),
  true, 'RLS が有効（CLAUDE.md §5）'
);
select ok(not has_table_privilege('anon', 'public.model_daily_metrics', 'select'),
          '匿名は成績を読めない');
select ok(not has_function_privilege('anon', 'public.upsert_model_daily_metrics(jsonb)', 'execute'),
          '匿名は書き込みの RPC を呼べない');
select ok(has_function_privilege('service_role', 'public.upsert_model_daily_metrics(jsonb)', 'execute'),
          'service_role は書き込みの RPC を呼べる');

-- ────────────────────────────────────────────────────────────────
-- 前提データ
-- ────────────────────────────────────────────────────────────────
delete from public.model_daily_metrics where model_version like 't-md-%';
delete from public.model_versions where model_version like 't-md-%';

insert into public.systems
  (system_id, display_name, operator_name, gbfs_base_url, expected_cadence_s, poll_interval_s,
   lock_key, is_active, capacity_is_dynamic)
values ('t-md-a', '評価用', '事業者', 'https://example.test/gbfs', 300, 60, 9601, true, false)
on conflict (system_id) do nothing;

insert into public.model_versions
  (model_version, kind, status, feature_set, artifact_path, train_days)
values ('t-md-v1', 'baseline', 'retired', 'v3', 'baseline/t-md-v1.json.gz', array['2026-09-06']);

-- ────────────────────────────────────────────────────────────────
-- 冪等（完了条件 2）
-- ────────────────────────────────────────────────────────────────
select is(
  public.upsert_model_daily_metrics(jsonb_build_array(
    jsonb_build_object(
      'model_version', 't-md-v1', 'metric_date', '2026-09-07', 'system_id', 't-md-a',
      'target', 'bike', 'h_min', 5, 'bucket', '0', 'n', 100, 'positives', 0.8,
      'brier', 0.02, 'log_loss', 0.1, 'ece', 0.01, 'ece_uniform', 0.012,
      'brier_b0', 0.04, 'skill_vs_b0', 0.5)
  )),
  1, '1 行入る'
);
select is(
  (select count(*)::int from public.model_daily_metrics where model_version = 't-md-v1'),
  1, '1 行だけ'
);

-- **同じ主キーで 2 度目。** 値を変えて入れ替わることまで見る
select is(
  public.upsert_model_daily_metrics(jsonb_build_array(
    jsonb_build_object(
      'model_version', 't-md-v1', 'metric_date', '2026-09-07', 'system_id', 't-md-a',
      'target', 'bike', 'h_min', 5, 'bucket', '0', 'n', 120, 'positives', 0.7,
      'brier', 0.03, 'log_loss', 0.2, 'ece', 0.02, 'ece_uniform', 0.022,
      'brier_b0', 0.05, 'skill_vs_b0', 0.4)
  )),
  1, '2 度目も 1 行'
);
select is(
  (select count(*)::int from public.model_daily_metrics where model_version = 't-md-v1'),
  1, '**行は増えない**（同じ日を 2 回測っても）'
);
select is(
  (select n from public.model_daily_metrics where model_version = 't-md-v1'),
  120, '**値は入れ替わる**（古い数字が残らない）'
);

-- **主キーの一部が違えば別の行になる。** 水平・バケツ・ターゲットで割れていること
select is(
  public.upsert_model_daily_metrics(jsonb_build_array(
    jsonb_build_object(
      'model_version', 't-md-v1', 'metric_date', '2026-09-07', 'system_id', 't-md-a',
      'target', 'dock', 'h_min', 5, 'bucket', '0', 'n', 90, 'positives', 0.6,
      'brier', 0.03, 'log_loss', 0.2, 'ece', 0.02, 'ece_uniform', 0.02,
      'brier_b0', 0.05, 'skill_vs_b0', 0.4),
    jsonb_build_object(
      'model_version', 't-md-v1', 'metric_date', '2026-09-07', 'system_id', 't-md-a',
      'target', 'bike', 'h_min', 60, 'bucket', '全体', 'n', 900, 'positives', 0.6,
      'brier', 0.06, 'log_loss', 0.3, 'ece', 0.03, 'ece_uniform', 0.03,
      'brier_b0', 0.09, 'skill_vs_b0', null)
  )),
  2, 'ターゲットと水平が違えば別の行'
);
select is(
  (select count(*)::int from public.model_daily_metrics where model_version = 't-md-v1'),
  3, '3 行になった'
);
select is(
  (select skill_vs_b0 from public.model_daily_metrics
    where model_version = 't-md-v1' and h_min = 60),
  null, '**B0 の Brier が 0 なら改善は NULL**（発散させない）'
);

-- ────────────────────────────────────────────────────────────────
-- 検査（値の形を型で縛る）
-- ────────────────────────────────────────────────────────────────
prepare bad_target as
  select public.upsert_model_daily_metrics(jsonb_build_array(jsonb_build_object(
    'model_version', 't-md-v1', 'metric_date', '2026-09-07', 'system_id', 't-md-a',
    'target', 'both', 'h_min', 5, 'bucket', '0', 'n', 1, 'positives', 0.5,
    'brier', 0.1, 'log_loss', 0.1, 'ece', 0.1, 'ece_uniform', 0.1, 'brier_b0', 0.1,
    'skill_vs_b0', 0.0)));
select throws_ok('bad_target', '23514', null, 'ターゲットは bike と dock だけ');

prepare empty_group as
  select public.upsert_model_daily_metrics(jsonb_build_array(jsonb_build_object(
    'model_version', 't-md-v1', 'metric_date', '2026-09-07', 'system_id', 't-md-a',
    'target', 'bike', 'h_min', 5, 'bucket', '1', 'n', 0, 'positives', 0.5,
    'brier', 0.1, 'log_loss', 0.1, 'ece', 0.1, 'ece_uniform', 0.1, 'brier_b0', 0.1,
    'skill_vs_b0', 0.0)));
select throws_ok('empty_group', '23514', null, '**0 行の成績は書かない**（測っていない）');

prepare impossible_rate as
  select public.upsert_model_daily_metrics(jsonb_build_array(jsonb_build_object(
    'model_version', 't-md-v1', 'metric_date', '2026-09-07', 'system_id', 't-md-a',
    'target', 'bike', 'h_min', 5, 'bucket', '1', 'n', 10, 'positives', 1.5,
    'brier', 0.1, 'log_loss', 0.1, 'ece', 0.1, 'ece_uniform', 0.1, 'brier_b0', 0.1,
    'skill_vs_b0', 0.0)));
select throws_ok('impossible_rate', '23514', null, '陽性率は 0〜1');

prepare unknown_version as
  select public.upsert_model_daily_metrics(jsonb_build_array(jsonb_build_object(
    'model_version', 't-md-missing', 'metric_date', '2026-09-07', 'system_id', 't-md-a',
    'target', 'bike', 'h_min', 5, 'bucket', '0', 'n', 1, 'positives', 0.5,
    'brier', 0.1, 'log_loss', 0.1, 'ece', 0.1, 'ece_uniform', 0.1, 'brier_b0', 0.1,
    'skill_vs_b0', 0.0)));
select throws_ok('unknown_version', '23503', null, '登録されていない版では書けない');

-- **成績の残っている版は消せない。** 「いつ何を配っていたか」を落とさないための縛り
prepare drop_measured_version as delete from public.model_versions where model_version = 't-md-v1';
select throws_ok('drop_measured_version', '23503', null, '成績の残っている版は消せない');

-- ────────────────────────────────────────────────────────────────
-- 見張り（完了条件 4）
-- ────────────────────────────────────────────────────────────────
select is(
  (select missing_after from public.monitored_jobs where job_name = 'evaluate_daily'),
  interval '30 hours', 'evaluate_daily は 30 時間で鳴る'
);
select is(
  (select is_active from public.monitored_jobs where job_name = 'evaluate_daily'),
  true, 'evaluate_daily の見張りは有効'
);
select is(
  (select cron_job_name from public.monitored_jobs where job_name = 'evaluate_daily'),
  null, '**pg_cron ではない**（Vercel Cron なので 0022 の検査の対象外）'
);

select * from finish();
rollback;
