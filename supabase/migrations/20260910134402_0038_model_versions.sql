-- 0038 モデルの登録簿（W4 プラン §6.5、開発プラン §5.3・§8.4）
--
-- **「いま何を配っているか」を DB が持つ。** W3 の段 8 では `model_versions` を作らず、
-- 環境変数（`BASELINE_MODEL_VERSION`）で指定していた（W3 プラン §5.10）。配るものが
-- 2 つ以上になる時点が作りどきで、それが PR E である（W4-08）。
--
-- 開発プラン §5.3 の DDL からの差分と、その理由：
--   * `kind` を足した。**ベースラインと LightGBM は成果物の読み方が違う**
--     （前者は当てはめ結果の JSON、後者は Booster の文字列）。パスの接頭辞ではなく
--     列で持つのは、読む側が「どちらの読み方をするか」を分岐する材料だから
--   * `feature_set` は**その版を当てはめたときの特徴量の版**。推論はこれを照合する
--     （CLAUDE.md §2 の原則 4）。**照合の仕方は成果物の種類で違う**：LightGBM は 61 列
--     すべてを読むので一致しなければ配れない。ベースラインが読むのは 6 列
--     （台数・水平・日内分・曜日種別・システム・ポート）で、v0 から v3 まで
--     **1 つも変わっていない**ので、版が違っても値は変わらない
--   * `train_days` は配列。**どの日で作ったか**が版の名前だけでは分からない
--   * `metrics` は jsonb。指標の形は W6 で変わる（校正・スライス別）ので列にしない
--
-- **`active` は同時に 1 つだけ。** `shadow` も 1 つだけ。部分一意索引で機械的に守る。
-- 昇格は `promote_model_version()` だけが行い、**この関数には誰にも grant しない**
-- （CLAUDE.md §6「モデルの `active` 切替・ロールバックは確認なしに実行しない」）。
-- 所有者が psql から呼ぶ操作であって、サービスから叩ける口は作らない。

create table public.model_versions (
  -- 成果物のファイル名にもなる版（`baseline-b3-v0-20260908` / `lgbm-v0-20260910`）
  model_version text primary key,
  -- 成果物の読み方。`baselines/artifact.py` か `models/lightgbm_artifact.py` か
  kind          text not null,
  status        text not null default 'candidate',
  -- **当てはめたときの特徴量の版**（`features/constants.py` の `FEATURE_SET`）
  feature_set   text not null,
  -- `models` バケット内のパス
  artifact_path text not null,
  -- 学習に使った JST の暦日（昇順）
  train_days    text[] not null,
  -- 評価の要約。**形は W6 で変わる**ので列にしない
  metrics       jsonb,
  -- モデルカード（`docs/model_cards/*.md`）
  card_path     text,
  note          text,
  created_at    timestamptz not null default now(),
  -- `active` にした時刻。退役しても消さない（いつ配っていたかの記録）
  promoted_at   timestamptz,
  constraint model_versions_kind check (kind in ('baseline', 'lightgbm')),
  constraint model_versions_status
    check (status in ('candidate', 'shadow', 'active', 'retired')),
  -- **`array_length` は空配列で NULL を返す。** `>= 1` だけだと制約が素通りする
  constraint model_versions_train_days_not_empty
    check (coalesce(array_length(train_days, 1), 0) >= 1),
  constraint model_versions_paths_not_blank
    check (length(btrim(artifact_path)) > 0 and length(btrim(feature_set)) > 0)
);

comment on table public.model_versions is
  'モデルの登録簿。active は同時に 1 つだけ。昇格は promote_model_version() のみ（CLAUDE.md §6）。';
comment on column public.model_versions.kind is
  '成果物の読み方。baseline は当てはめ結果の JSON、lightgbm は Booster の文字列。';
comment on column public.model_versions.feature_set is
  '当てはめたときの特徴量の版。lightgbm は 61 列すべてを読むので、配信時の版と一致しなければ配れない。';

-- **「その述語を満たす行は高々 1 つ」の書き方。** 定数式に一意索引を張る
create unique index model_versions_one_active on public.model_versions ((true))
  where status = 'active';
create unique index model_versions_one_shadow on public.model_versions ((true))
  where status = 'shadow';

alter table public.model_versions enable row level security;
revoke all on table public.model_versions from anon, authenticated;
-- **読むのは推論**（いま配る版を引く）。**書くのは学習ジョブ**（候補を登録する）。
-- 更新と削除は渡さない：状態を動かすのは `promote_model_version()` だけである。
--
-- **`revoke` を先に書く。** 0006 が `service_role` に既定権限を入れてあるので、
-- `grant select, insert` だけでは update / delete が付いたままになる（pgTAP が捕まえた）。
revoke all on table public.model_versions from service_role;
grant select, insert on table public.model_versions to service_role;

-- ────────────────────────────────────────────────────────────────
-- register_model_version — 候補を登録する
-- ────────────────────────────────────────────────────────────────
-- **登録は `candidate` か `retired` しか作れない。** `active` と `shadow` にするのは
-- 昇格の仕事で、そこは人が確認して行う。学習ジョブが自分で配信を切り替えられない。
create or replace function public.register_model_version(p_row jsonb)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_status text := coalesce(p_row->>'status', 'candidate');
  v_version text := p_row->>'model_version';
begin
  if v_status not in ('candidate', 'retired') then
    raise exception '登録できるのは candidate か retired だけです: %', v_status
      using errcode = '22023';
  end if;

  insert into public.model_versions
    (model_version, kind, status, feature_set, artifact_path, train_days,
     metrics, card_path, note)
  values (
    v_version,
    p_row->>'kind',
    v_status,
    p_row->>'feature_set',
    p_row->>'artifact_path',
    array(select jsonb_array_elements_text(p_row->'train_days')),
    p_row->'metrics',
    p_row->>'card_path',
    p_row->>'note'
  )
  on conflict (model_version) do update
     set kind = excluded.kind,
         feature_set = excluded.feature_set,
         artifact_path = excluded.artifact_path,
         train_days = excluded.train_days,
         metrics = excluded.metrics,
         card_path = excluded.card_path,
         note = excluded.note
     -- **配信中の版は上書きさせない。** 同じ名前で中身を差し替えると、
     -- `station_forecasts.model_version` が指す先が変わってしまう
     where public.model_versions.status in ('candidate', 'retired');

  if not found then
    raise exception '配信中（active / shadow）の版は登録し直せません: %', v_version
      using errcode = '55006';
  end if;
  return jsonb_build_object('model_version', v_version, 'status', v_status);
end;
$$;

comment on function public.register_model_version(jsonb) is
  '候補を登録する。candidate / retired しか作れず、配信中（active / shadow）の版は上書きできない。';

-- ────────────────────────────────────────────────────────────────
-- promote_model_version — 配信を切り替える（**人が確認して行う**）
-- ────────────────────────────────────────────────────────────────
-- CLAUDE.md §6：「モデルの `active` 切替・ロールバックは確認なしに実行しない」。
-- **誰にも grant しない。** 所有者が psql から呼ぶ。サービスから叩ける口を作らない。
--
-- いまの `active` は `retired` に落とす。**1 トランザクションで入れ替える**ので、
-- 「active が 0 個」の瞬間も「2 個」の瞬間も外から見えない。
create or replace function public.promote_model_version(
  p_version text,
  p_status  text default 'active'
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_previous text;
  v_row public.model_versions;
begin
  if p_status not in ('active', 'shadow') then
    raise exception '昇格先は active か shadow です: %', p_status using errcode = '22023';
  end if;
  select * into v_row from public.model_versions where model_version = p_version;
  if not found then
    raise exception '登録されていない版です: %', p_version using errcode = '22023';
  end if;

  update public.model_versions set status = 'retired'
   where status = p_status and model_version <> p_version
  returning model_version into v_previous;

  update public.model_versions
     set status = p_status,
         promoted_at = case when p_status = 'active' then now() else promoted_at end
   where model_version = p_version;

  return jsonb_build_object(
    'model_version', p_version, 'status', p_status, 'retired', v_previous);
end;
$$;

comment on function public.promote_model_version(text, text) is
  '配信を切り替える。前の版は retired にする。**誰にも grant しない**（所有者が psql から呼ぶ。CLAUDE.md §6）。';

revoke all on function public.register_model_version(jsonb) from public, anon, authenticated;
grant execute on function public.register_model_version(jsonb) to service_role;

-- **昇格は誰にも渡さない。** ローカルは既定権限で `service_role` に EXECUTE が付くので、
-- 明示的に剥がす（本番は「新規オブジェクトの自動公開」が OFF なので既定では付かないが、
-- 環境によって権限が違う状態を残さない。W3 プラン §12 の 90 の裏返し）
revoke all on function public.promote_model_version(text, text)
  from public, anon, authenticated, service_role;

-- ────────────────────────────────────────────────────────────────
-- いま配っている版を登録する（W4-08）
-- ────────────────────────────────────────────────────────────────
-- **登録の無いモデルを配っている状態を残さない**（CLAUDE.md §6）。
--
-- `feature_set` は成果物に書いてある値をそのまま入れる：**v0 である**。配信側の
-- `FEATURE_SET` は v3 まで進んでいるが、**B0〜B3 が読む 6 列は 1 つも変わっていない**
-- ので、値は変わらない（v1 は容量まわり、v2 は `minutes_since_last_change`、
-- v3 は天気の 4 列で、どれもベースラインは読まない）。
--
-- `metrics` は `docs/260908_eda_02_baseline.md` の §2（重み付き Brier、全体）。
-- **これは「配線が通っているか」の確認であって精度の評価ではない**（学習 1 日、
-- B2 は 100% が B1 に落ちている）。測り直しは 9/16 以降。
insert into public.model_versions
  (model_version, kind, status, feature_set, artifact_path, train_days,
   metrics, card_path, note, promoted_at)
values (
  'baseline-b3-v0-20260908',
  'baseline',
  'active',
  'v0',
  'baseline/baseline-b3-v0-20260908.json.gz',
  array['2026-09-06', '2026-09-07', '2026-09-08'],
  jsonb_build_object(
    'source', 'docs/260908_eda_02_baseline.md',
    'split', '学習 2026-09-06 / パージ 2026-09-07 / 検証 2026-09-08',
    'n_eval', 1487577,
    'brier_weighted', jsonb_build_object(
      'docomo-cycle/bike', 0.05369, 'hellocycling/bike', 0.03071,
      'docomo-cycle/dock', 0.01821, 'hellocycling/dock', 0.04314),
    'caveat', '学習 1 日。B2 は 100% が B1 に落ちている。精度の評価ではない'),
  'docs/260908_eda_02_baseline.md',
  '2026-09-08 20:59 JST から配信中。W3 の段 8 では環境変数で指定していた版を、0038 で登録簿に移した',
  '2026-09-08T11:59:00Z'
)
on conflict (model_version) do nothing;

notify pgrst, 'reload schema';
