-- pgTAP: モデルの登録簿（W4 プラン §6.5、マイグレーション 0038）
--
-- ここで見るのは **DB 側の契約**——表の形・権限・「active は 1 つ」・昇格の入り口。
-- どの版を配るかを推論がどう読むかは `apps/ml` の側（`tests/test_model_registry.py`）。

begin;
select plan(35);

create function pg_temp.candidate(p_version text, p_kind text default 'lightgbm') returns jsonb
language sql as $$
  select public.register_model_version(jsonb_build_object(
    'model_version', p_version, 'kind', p_kind, 'feature_set', 'v3',
    'artifact_path', 'lightgbm/' || p_version || '.json.gz',
    'train_days', jsonb_build_array('2026-09-08', '2026-09-09')));
$$;

-- ────────────────────────────────────────────────────────────────
-- 表の形と権限
-- ────────────────────────────────────────────────────────────────
select has_table('public', 'model_versions', 'model_versions がある');
select columns_are(
  'public', 'model_versions',
  array['model_version', 'kind', 'status', 'feature_set', 'artifact_path',
        'train_days', 'metrics', 'card_path', 'note', 'created_at', 'promoted_at'],
  '列はこの 11 だけ'
);
select col_is_pk('public', 'model_versions', 'model_version', '主キーは版の名前');
select is(
  (select relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relname = 'model_versions'),
  true, 'RLS 有効（CLAUDE.md §5）'
);
select is(has_table_privilege('anon', 'public.model_versions', 'select'), false, '匿名は読めない');
select is(has_table_privilege('service_role', 'public.model_versions', 'select'), true,
  'サービスロールは読める（推論がいまの版を引く）');
select is(has_table_privilege('service_role', 'public.model_versions', 'insert'), true,
  'サービスロールは書ける（学習ジョブが候補を登録する）');
-- **状態を動かす口は渡さない。** 昇格は promote_model_version() だけの仕事
select is(has_table_privilege('service_role', 'public.model_versions', 'update'), false,
  'サービスロールは更新できない（状態を直に動かせない）');
select is(has_table_privilege('service_role', 'public.model_versions', 'delete'), false,
  'サービスロールは削除できない');

-- ────────────────────────────────────────────────────────────────
-- 制約
-- ────────────────────────────────────────────────────────────────
select throws_ok(
  $$insert into public.model_versions
      (model_version, kind, status, feature_set, artifact_path, train_days)
    values ('x', 'xgboost', 'candidate', 'v3', 'p', array['2026-09-09'])$$,
  '23514', null, '知らない kind は入らない'
);
select throws_ok(
  $$insert into public.model_versions
      (model_version, kind, status, feature_set, artifact_path, train_days)
    values ('x', 'lightgbm', 'champion', 'v3', 'p', array['2026-09-09'])$$,
  '23514', null, '知らない status は入らない'
);
select throws_ok(
  $$insert into public.model_versions
      (model_version, kind, status, feature_set, artifact_path, train_days)
    values ('x', 'lightgbm', 'candidate', 'v3', 'p', array[]::text[])$$,
  '23514', null, '学習日が空の版は入らない'
);
select throws_ok(
  $$insert into public.model_versions
      (model_version, kind, status, feature_set, artifact_path, train_days)
    values ('x', 'lightgbm', 'candidate', '  ', 'p', array['2026-09-09'])$$,
  '23514', null, '特徴量の版が空白だけの行は入らない'
);

-- **active は同時に 1 つだけ**（0038 の主眼）
select throws_ok(
  $$insert into public.model_versions
      (model_version, kind, status, feature_set, artifact_path, train_days)
    values ('two-active', 'lightgbm', 'active', 'v3', 'p', array['2026-09-09'])$$,
  '23505', null, '2 つ目の active は入らない'
);
select lives_ok(
  $$insert into public.model_versions
      (model_version, kind, status, feature_set, artifact_path, train_days)
    values ('one-shadow', 'lightgbm', 'shadow', 'v3', 'p', array['2026-09-09'])$$,
  'shadow は 1 つ目なら入る'
);
select throws_ok(
  $$insert into public.model_versions
      (model_version, kind, status, feature_set, artifact_path, train_days)
    values ('two-shadow', 'lightgbm', 'shadow', 'v3', 'p', array['2026-09-09'])$$,
  '23505', null, '2 つ目の shadow は入らない'
);
delete from public.model_versions where model_version = 'one-shadow';

-- ────────────────────────────────────────────────────────────────
-- register_model_version
-- ────────────────────────────────────────────────────────────────
select has_function('public', 'register_model_version', 'register_model_version がある');
select is(
  (select p.prosecdef from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'register_model_version'),
  true, 'security definer'
);
select is(
  (select p.proconfig @> array['search_path=""'] from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'register_model_version'),
  true, 'search_path = '''''
);
select is(
  has_function_privilege('service_role', 'public.register_model_version(jsonb)', 'execute'),
  true, 'サービスロールは登録できる'
);
select is(
  has_function_privilege('anon', 'public.register_model_version(jsonb)', 'execute'),
  false, '匿名は登録できない'
);

select is(pg_temp.candidate('lgbm-a') ->> 'status', 'candidate', '候補として登録できる');
select is(
  (select status from public.model_versions where model_version = 'lgbm-a'),
  'candidate', '状態は candidate'
);
select lives_ok($$select pg_temp.candidate('lgbm-a')$$, '同じ版を登録し直せる（冪等）');
select is((select count(*)::int from public.model_versions where model_version = 'lgbm-a'), 1,
  '行は増えない');

-- **学習ジョブが自分で配信を切り替えられない**
select throws_ok(
  $$select public.register_model_version(jsonb_build_object(
      'model_version', 'sneaky', 'kind', 'lightgbm', 'status', 'active',
      'feature_set', 'v3', 'artifact_path', 'p',
      'train_days', jsonb_build_array('2026-09-09')))$$,
  '22023', null, '登録で active にはできない'
);
-- **配信中の版は上書きさせない**
select throws_ok(
  $$select public.register_model_version(jsonb_build_object(
      'model_version', 'baseline-b3-v0-20260908', 'kind', 'baseline', 'status', 'candidate',
      'feature_set', 'v9', 'artifact_path', 'p',
      'train_days', jsonb_build_array('2026-09-09')))$$,
  '55006', null, '配信中の版は登録し直せない'
);

-- ────────────────────────────────────────────────────────────────
-- promote_model_version — **誰にも grant しない**（CLAUDE.md §6）
-- ────────────────────────────────────────────────────────────────
select has_function('public', 'promote_model_version', 'promote_model_version がある');
select is(
  has_function_privilege('service_role', 'public.promote_model_version(text, text)', 'execute'),
  false, '**サービスロールは昇格できない**（人が psql から行う）'
);
select is(
  has_function_privilege('anon', 'public.promote_model_version(text, text)', 'execute'),
  false, '匿名は昇格できない'
);
select throws_ok(
  $$select public.promote_model_version('知らない版')$$,
  '22023', null, '登録されていない版は昇格できない'
);
select throws_ok(
  $$select public.promote_model_version('lgbm-a', 'candidate')$$,
  '22023', null, '昇格先は active か shadow だけ'
);

select is(
  public.promote_model_version('lgbm-a') ->> 'retired', 'baseline-b3-v0-20260908',
  '前の active は retired になる'
);
select is(
  (select status from public.model_versions where model_version = 'baseline-b3-v0-20260908'),
  'retired', '入れ替わっている'
);
select isnt(
  (select promoted_at from public.model_versions where model_version = 'lgbm-a'),
  null, '昇格した時刻が入る'
);

-- ────────────────────────────────────────────────────────────────
-- いま配っている版が登録されている（W4-08）
-- ────────────────────────────────────────────────────────────────
select * from finish();
rollback;
