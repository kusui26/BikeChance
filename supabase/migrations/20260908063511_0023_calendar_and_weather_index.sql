-- 0023 暦と天気アーカイブの索引（W3 プラン §5.6）
--
-- 特徴量が基準時刻 `t` から引く「時刻の参照データ」を 2 つ用意する。どちらも小さく、
-- 収集にも配信にも触らない。
--
--   jp_holidays       内閣府「国民の祝日について」CSV の中身そのもの
--   v_weather_files   予報ファイルが**いつ入手できたか**の索引
--
-- **年末年始（12/29〜1/3）とお盆（8/13〜16）はここに入れない。** 暦の規則で決まるので、
-- 導けるものを表に持たない。規則は `packages/shared/src/calendar.ts` と
-- `apps/ml/bikechance_ml/features/calendar.py` の 2 つにあり、ゴールデンで突き合わせる
-- （W3 プラン §12 の 87）。

-- ────────────────────────────────────────────────────────────────
-- 祝日
-- ────────────────────────────────────────────────────────────────
create table public.jp_holidays (
  holiday_date date primary key,
  -- CSV の「国民の祝日・休日名称」。「元日」などの本来の祝日と、振替休日・国民の休日を
  -- 表す「休日」の両方が入る。**名称で区別できるので種別の列は持たない**
  name         text not null,
  constraint jp_holidays_name_not_blank check (length(btrim(name)) > 0)
);

comment on table public.jp_holidays is
  '内閣府「国民の祝日について」CSV の中身。年末年始とお盆は暦の規則なのでここには入れない（W3 プラン §12 の 87）。scripts/import-holidays.ts が入れ替える。';
comment on column public.jp_holidays.name is
  '「元日」などの祝日名と、振替休日・国民の休日を表す「休日」。名称で区別できる。';

alter table public.jp_holidays enable row level security;
revoke all on table public.jp_holidays from anon, authenticated;

-- **入れ替えは 1 つの RPC で行う。** PostgREST の 1 リクエストは 1 トランザクションなので、
-- 「消してから入れる」を安全にやるにはこれしかない。delete と insert を別々のリクエストに
-- すると、その間だけ祝日が 0 件の状態ができ、そこで特徴量を作ると**全部平日**になる。
--
-- **空にしてしまう事故を関数の中で止める。** CSV の取得が壊れて 0 行を渡しても、
-- 100 行未満は受け付けない（1955 年からの収録で 1,000 行以上あるのが正常）。
create or replace function public.replace_jp_holidays(p_rows jsonb)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_before integer;
  v_after  integer;
begin
  if jsonb_typeof(p_rows) is distinct from 'array' then
    raise exception '祝日は配列で渡してください';
  end if;
  if jsonb_array_length(p_rows) < 100 then
    raise exception '祝日の行が少なすぎます（%）。取得の失敗で表を空にしない', jsonb_array_length(p_rows);
  end if;

  select count(*) into v_before from public.jp_holidays;
  -- **`where true` を省かない。** Supabase は PostgREST 経由のセッションで
  -- `safeupdate` を有効にしており、WHERE の無い DELETE は 21000 で弾かれる
  -- （`security definer` でもセッションの設定なので効く。W3 プラン §12 の 88）
  delete from public.jp_holidays where true;
  insert into public.jp_holidays (holiday_date, name)
  select (row->>'d')::date, row->>'n' from jsonb_array_elements(p_rows) row;
  select count(*) into v_after from public.jp_holidays;

  return jsonb_build_object(
    'before', v_before, 'after', v_after,
    'min', (select min(holiday_date) from public.jp_holidays),
    'max', (select max(holiday_date) from public.jp_holidays));
end;
$$;

comment on function public.replace_jp_holidays(jsonb) is
  '祝日を丸ごと入れ替える。1 トランザクションで行い、100 行未満は受け付けない（取得の失敗で表を空にしないため）。';

revoke all on function public.replace_jp_holidays(jsonb) from public, anon, authenticated;

-- ────────────────────────────────────────────────────────────────
-- 天気アーカイブの入手時刻
-- ────────────────────────────────────────────────────────────────
-- **`hour_epoch` は「入手できた時刻」ではない。** 取得を始めた時刻を「時」に丸めた値で、
-- 実際に保存されるのは cron の分（`17 * * * *`）＝ `hour_epoch + 約 18 分`（実測で
-- 17.637〜17.658 分、ばらつき 0.02 分）。「`hour_epoch <= t` の最新」で引くと、`t` が
-- 毎正時から 18 分の間にあるとき**まだ発行されていない予報**を使う（5 分グリッド点の 30%）。
--
-- W2 プラン §14.4 は「W3 は保守的な既定（`hour_epoch + 1 時間`）、W4 で厳密版」と
-- していたが、材料（`job_runs.finished_at` と `detail.hour_epoch_s`）が全行に揃って
-- いることを実測で確認したので、**先に厳密版を作る**（W3-06）。ビュー 1 本で足りる。
create view public.v_weather_files as
select (r.detail->>'hour_epoch_s')::bigint as hour_epoch_s,
       to_timestamp((r.detail->>'hour_epoch_s')::bigint) as forecast_hour,
       -- **これが「入手できた時刻」。** 特徴量は available_at <= t で引く
       r.finished_at                       as available_at,
       (r.detail->>'n_saved')::integer     as n_saved,
       (r.detail->>'n_failed')::integer    as n_failed,
       (r.detail->>'n_cells')::integer     as n_cells,
       r.status                            as status
  from public.job_runs r
 where r.job_name = 'archive_weather'
   and r.detail ? 'hour_epoch_s'
   and r.finished_at is not null;

comment on view public.v_weather_files is
  '天気の予報ファイルが「いつ入手できたか」。特徴量は available_at <= t で引く。hour_epoch_s で引いてはいけない（W3 プラン §9.4）。';

revoke all on public.v_weather_files from anon, authenticated;

notify pgrst, 'reload schema';
