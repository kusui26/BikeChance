-- pgTAP: 暦と天気アーカイブの索引（W3 プラン §5.6、マイグレーション 0023）
--
-- 暦の**規則**（年末年始・お盆・飛び石・月末営業日）はここでは見ない。あれは
-- `packages/shared/src/calendar.ts` と `apps/ml/bikechance_ml/features/calendar.py` の
-- 2 実装で、`fixtures/calendar/day_type_golden.csv` を挟んで突き合わせてある。
-- ここで見るのは **DB 側の契約**——表の形・権限・入れ替えの安全弁・ビューの写像。

begin;
select plan(32);

delete from public.jp_holidays;
delete from public.job_runs;
delete from public.alert_state;

create function pg_temp.rows_json(p_count integer) returns jsonb language sql as $$
  select jsonb_agg(jsonb_build_object(
           'd', to_char(date '2026-01-01' + n, 'YYYY-MM-DD'),
           'n', 'テスト' || n))
    from generate_series(0, p_count - 1) n;
$$;

-- ────────────────────────────────────────────────────────────────
-- 表の形と権限
-- ────────────────────────────────────────────────────────────────
select has_table('public', 'jp_holidays', 'jp_holidays がある');
select col_is_pk('public', 'jp_holidays', 'holiday_date', '主キーは日付');
select is(
  (select relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relname = 'jp_holidays'),
  true, 'RLS 有効（CLAUDE.md §5）'
);
select is(has_table_privilege('anon', 'public.jp_holidays', 'select'), false, '匿名は読めない');
select throws_ok(
  $$insert into public.jp_holidays values ('2026-01-01', '  ')$$,
  '23514', null, '名称が空白だけの行は制約で弾く'
);

-- **年末年始とお盆の列を持たない**（暦の規則なので表に入れない。§12 の 87）
select hasnt_column('public', 'jp_holidays', 'kind', '種別の列は持たない（規則は表に入れない）');

-- ────────────────────────────────────────────────────────────────
-- 入れ替えの安全弁
-- ────────────────────────────────────────────────────────────────
select has_function('public', 'replace_jp_holidays', 'replace_jp_holidays がある');
select is(
  (select p.prosecdef from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'replace_jp_holidays'),
  true, 'security definer'
);
select is(
  (select p.proconfig @> array['search_path=""'] from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'replace_jp_holidays'),
  true, 'search_path = '''''
);
select is(
  has_function_privilege('anon', 'public.replace_jp_holidays(jsonb)', 'execute'),
  false, '匿名は呼べない'
);

-- **`where true` を省くと PostgREST 経由で 21000 になる**（authenticator は safeupdate を
-- session_preload_libraries に持つ）。pgTAP は postgres で動くので再現できないため、
-- 実装のほうを直接見張る（§12 の 88）
select matches(
  (select p.prosrc from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'replace_jp_holidays'),
  'delete from public\.jp_holidays where',
  'DELETE に WHERE がある（safeupdate 下でも通る）'
);

select throws_ok(
  $$select public.replace_jp_holidays('{}'::jsonb)$$,
  'P0001', null, '配列でなければ拒む'
);
select throws_ok(
  $$select public.replace_jp_holidays('[]'::jsonb)$$,
  'P0001', null, '**空の配列で表を空にしない**'
);
select throws_ok(
  $$select public.replace_jp_holidays(pg_temp.rows_json(99))$$,
  'P0001', null, '100 行未満は拒む（取得の失敗で表を壊さない）'
);
select is((select count(*)::int from public.jp_holidays), 0, '拒んだときは 1 行も入っていない');

select lives_ok(
  $$select public.replace_jp_holidays(pg_temp.rows_json(100))$$,
  '100 行なら通る'
);
select is((select count(*)::int from public.jp_holidays), 100, '100 行入った');

-- 入れ替え（前の中身は残らない）
select is(
  public.replace_jp_holidays(pg_temp.rows_json(150)) -> 'before', '100'::jsonb,
  '入れ替え前の件数を返す'
);
select is((select count(*)::int from public.jp_holidays), 150, '**前の中身は残らない**（追記ではない）');

-- ────────────────────────────────────────────────────────────────
-- 参照データの期限（0020 の検査が生き返る）
-- ────────────────────────────────────────────────────────────────
delete from public.jp_holidays where true;
delete from public.alert_state;
select is(
  public.check_reference_data() -> 'skipped', 'true'::jsonb,
  '表が空なら飛ばす（0 件を「期限切れ」と読まない）'
);

insert into public.jp_holidays values (current_date + 441, '遠い祝日');
select is(public.check_reference_data() -> 'days_left', '441'::jsonb, '残り日数を数える');
select is(public.check_reference_data() -> 'alerts', '0'::jsonb, '十分先まであれば鳴らない');

delete from public.jp_holidays where true;
insert into public.jp_holidays values (current_date + 30, '近い祝日');
select ok(
  public.check_reference_data() -> 'alerts' = '1'::jsonb,
  '残り 90 日を切ったら鳴る（静かに祝日が消えるのが一番まずい）'
);

-- ────────────────────────────────────────────────────────────────
-- 天気アーカイブの入手時刻
-- ────────────────────────────────────────────────────────────────
select has_view('public', 'v_weather_files', 'v_weather_files がある');
select is(has_table_privilege('anon', 'public.v_weather_files', 'select'), false, '匿名は読めない');

delete from public.job_runs;
-- 本番と同じ形の行を 2 つ。**取得は毎正時の 18 分後**（実測 17.64 分）
insert into public.job_runs (job_name, started_at, finished_at, status, detail) values
  ('archive_weather', timestamptz '2026-09-08 00:17:34+09', timestamptz '2026-09-08 00:17:38+09',
   'ok', '{"hour_epoch_s": 1788793200, "n_saved": 6, "n_failed": 0, "n_cells": 595}'),
  ('archive_weather', timestamptz '2026-09-08 01:17:34+09', timestamptz '2026-09-08 01:17:38+09',
   'ok', '{"hour_epoch_s": 1788796800, "n_saved": 6, "n_failed": 0, "n_cells": 595}');
-- 混ざってはいけない行
insert into public.job_runs (job_name, started_at, finished_at, status, detail) values
  ('compact_parquet', now(), now(), 'ok', '{"hour_epoch_s": 1788793200}'),
  ('archive_weather', now(), now(), 'failed', '{"error": "boom"}'),
  ('archive_weather', now(), null, 'running', '{"hour_epoch_s": 1788800400}');

select is((select count(*)::int from public.v_weather_files), 2, '完了した archive_weather だけが載る');
select is(
  (select count(*)::int from public.v_weather_files where hour_epoch_s = 1788800400),
  0, '**まだ終わっていない行は載せない**（入手できていない）'
);
select is(
  (select count(*)::int from public.job_runs r
    where r.job_name = 'compact_parquet' and r.detail ? 'hour_epoch_s'),
  1, '他のジョブが同じキーを持っていても'
);
select is(
  (select count(*)::int from public.v_weather_files v
     join public.job_runs r on r.job_name = 'compact_parquet'
    where v.hour_epoch_s = (r.detail->>'hour_epoch_s')::bigint and v.available_at = r.finished_at),
  0, '他のジョブは混ざらない'
);

-- **これが要点。** hour_epoch と available_at は 18 分ずれる
select is(
  (select round(extract(epoch from available_at - forecast_hour) / 60)::int
     from public.v_weather_files where hour_epoch_s = 1788793200),
  18, 'available_at は予報時刻の約 18 分後（hour_epoch で引いてはいけない）'
);
select is(
  (select hour_epoch_s from public.v_weather_files
    where status = 'ok' and available_at <= timestamptz '2026-09-08 01:30:00+09'
    order by available_at desc limit 1),
  1788796800::bigint, '「t 時点で入手できていた最新」を引ける'
);
select is(
  (select hour_epoch_s from public.v_weather_files
    where status = 'ok' and available_at <= timestamptz '2026-09-08 01:10:00+09'
    order by available_at desc limit 1),
  1788793200::bigint, '**01:10 の時点ではまだ 01 時の予報は入手できていない**（18 分後に届く）'
);

select * from finish();
rollback;
