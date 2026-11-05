-- ────────────────────────────────────────────────────────────────
-- 0059 — ポートの「ふだんの傾向」（`station_trend`。W9 プランの W9-02〜05、開発プランの D-47）
-- ────────────────────────────────────────────────────────────────
-- 詳細画面の「ふだんの傾向（7 日 × 24 時間）」（W9-06）が読む。**予測ではない**——いまの台数を
-- 見ない、過去 28 日の統計を校正したものである。来週の水曜も再来週の水曜も同じ数になる。
--
-- ## なぜ 168 時間を毎時書かないのか（D-08 を直した。D-47）
--
-- 中身は**プロファイル（日次）でしか変わらない**ので、毎時 168 時間を書き直しても情報は増えず、
-- 2.2 万ポート × 336 個の UPSERT と Vercel の CPU だけが増える。**日に 1 回、ポートごとの
-- 「曜日種別 × 24 時間」の 72 個を写し、7 日 × 24 時間は読む側（`/v1`）が暦で組む。**
-- 書くのは GitHub Actions の `build_trend`（`build-features.yml` の 00:40 JST の回。サービス
-- ロール）で、Vercel の費用は増えない。
--
-- ## 72 個の並び
--
-- **曜日種別 × 0〜23 時**。曜日種別の並びは **sat・sun_holiday・weekday**（Python の
-- `features/calendar.py` の `DOW_TYPE_ORDER`、TypeScript の `TREND_DOW_TYPE_ORDER`）。
-- 1 時間 ＝ 15 分枠 4 つの和の比、日数は 4 枠の最小。**出せない時間は NULL**（配る側の日数の
-- 下限に届かない、または系統 × ターゲットの組が校正の門を通らなかった。W9-04）。
--
-- ## 大きさ
--
-- 約 2.2 万行 × 3 本の `smallint[72]`（1 本 168 B ほど。TOAST の閾値 2 KB より小さく、行の中に
-- 収まる）で **15 MB 前後**（索引込み）。毎日全行を書き直すので、同じ大きさの死んだ行が 1 日ぶん
-- 出て、autovacuum が片付ける。上限 8 GB・警報 6 GB（開発プラン §4.5）に対して小さい。
--
-- ## 消さない
--
-- 28 日の観測が 1 度も無くなったポートは `p_rows` に入らないが、古い行は残す
-- （`station_capacity_est` と同じ）。**`profile_date` に古さが出る**ので、読む側が古い行を
-- 出さない（`TREND_MAX_AGE_DAYS`）。そもそも現在値が無いポートは詳細が 404 になる。
-- ────────────────────────────────────────────────────────────────

create table if not exists public.station_trend (
  system_id    text        not null,
  station_id   text        not null,
  -- どの日のプロファイルから作ったか（JST の暦日。28 日の窓の最後の日）
  profile_date date        not null,
  -- 借りられる確率 × 1000（72 個。出せない時間は NULL）
  p_bike_x1000 smallint[]  not null,
  -- 返せる確率 × 1000
  p_dock_x1000 smallint[]  not null,
  -- その時間の日数（4 枠の最小。格子点の無い時間は 0）。**画面には出さない**（診断用）
  n_days       smallint[]  not null,
  updated_at   timestamptz not null default now(),
  primary key (system_id, station_id),
  constraint station_trend_station_fkey
    foreign key (system_id, station_id) references public.stations (system_id, station_id),
  -- **72 個の 1 次元の配列**。長さが違うと、読む側の添字が別の時間を指す
  constraint station_trend_shape check (
        array_ndims(p_bike_x1000) = 1 and array_length(p_bike_x1000, 1) = 72
    and array_ndims(p_dock_x1000) = 1 and array_length(p_dock_x1000, 1) = 72
    and array_ndims(n_days) = 1 and array_length(n_days, 1) = 72
  ),
  -- 確率の範囲。**NULL の要素は通る**（`all` は NULL を偽にしない）
  constraint station_trend_probability_range check (
        0 <= all (p_bike_x1000) and 1000 >= all (p_bike_x1000)
    and 0 <= all (p_dock_x1000) and 1000 >= all (p_dock_x1000)
  ),
  constraint station_trend_days_non_negative check (0 <= all (n_days))
);

comment on table public.station_trend is
  'ポートの「ふだんの傾向」（D-47）。プロファイル（28 日）を校正した確率を、曜日種別（sat・sun_holiday・weekday）× 24 時間の 72 個で持つ。build_trend が毎日 1 回写す。予測ではない。';
comment on column public.station_trend.profile_date is
  '作ったプロファイルの JST 暦日（28 日の窓の最後の日）。止まったときに何日ぶん古いかが読める。';
comment on column public.station_trend.p_bike_x1000 is
  '借りられる確率 × 1000。72 個 ＝ 曜日種別（sat・sun_holiday・weekday の順）× 0〜23 時。出せない時間は NULL。';
comment on column public.station_trend.n_days is
  'その時間の日数（15 分枠 4 つの最小）。配る側の下限（平日 3・土日祝 4）に届かない時間は確率が NULL。';

alter table public.station_trend enable row level security;
revoke all on table public.station_trend from anon, authenticated;
grant select, insert, update, delete on table public.station_trend to service_role;

-- ────────────────────────────────────────────────────────────────
-- まとめて書く（`upsert_station_trend`）
-- ────────────────────────────────────────────────────────────────
-- **表に直接 POST させない。** 書き込みは RPC を通す（`upsert_capacity_est` と同じ形）。
-- 約 2.2 万行を 1 日 1 回、`build_trend` が 1,000 行ずつ送る。
--
-- **古い版で上書きしない。** 欠けた日を手で埋め直す回（`workflow_dispatch` に日付を渡す）が
-- 古いプロファイルで写しても、新しい行は残る（同じ版なら置き直す。冪等）。
-- 戻り値は**実際に書いた行数**で、古い版で弾いた行は数えない。

create or replace function public.upsert_station_trend(p_rows jsonb)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_count integer;
begin
  insert into public.station_trend as t
    (system_id, station_id, profile_date, p_bike_x1000, p_dock_x1000, n_days, updated_at)
  select r.system_id, r.station_id, r.profile_date, r.p_bike_x1000, r.p_dock_x1000, r.n_days, now()
    from jsonb_to_recordset(p_rows) as r(
      system_id text, station_id text, profile_date date,
      p_bike_x1000 smallint[], p_dock_x1000 smallint[], n_days smallint[]
    )
  on conflict (system_id, station_id) do update
     set profile_date = excluded.profile_date,
         p_bike_x1000 = excluded.p_bike_x1000,
         p_dock_x1000 = excluded.p_dock_x1000,
         n_days       = excluded.n_days,
         updated_at   = excluded.updated_at
   where t.profile_date <= excluded.profile_date;
  get diagnostics v_count = row_count;
  return v_count;
end;
$$;

comment on function public.upsert_station_trend(jsonb) is
  'ふだんの傾向をまとめて UPSERT する。build_trend が毎日 1 回送る。古い profile_date で新しい行を上書きしない。台帳に無いポートは外部キーで弾く。';

revoke all on function public.upsert_station_trend(jsonb) from public, anon, authenticated;
-- **本番は「新規オブジェクトの自動公開」が OFF**（W3 プラン §12 の 90）。明示的に付ける
grant execute on function public.upsert_station_trend(jsonb) to service_role;

-- 止まったら鳴らす（0036 の仕組み）。**プロファイルと同じ閾値**（同じワークフローで続けて走る）
insert into public.monitored_jobs (job_name, expected_every, missing_after, note, cron_job_name)
values ('build_trend', interval '1 day', interval '30 hours',
        'GitHub Actions（build-features.yml）00:40 JST の回、落ちたら 06:00 の回。ふだんの傾向を station_trend に写す。止まっても前の版が残り、読む側は TREND_MAX_AGE_DAYS を過ぎた版を出さない（作り直しは build-features.yml を日付なしで手で起動）。',
        null)
    on conflict (job_name) do update
   set expected_every = excluded.expected_every,
       missing_after  = excluded.missing_after,
       note           = excluded.note,
       cron_job_name  = excluded.cron_job_name;

-- ────────────────────────────────────────────────────────────────
-- 公開ビュー
-- ────────────────────────────────────────────────────────────────
-- **基底テーブルは公開経路から名指ししない**（0018 と同じ境界）。**停止したシステムは出さない**
-- （`v1_station_hourly` と同じ。0046）。`n_days` は出さない（画面に要らない。要るようになったら
-- 末尾に足す——`create or replace view` は列の間に挿せない。0045 の注記）。
create or replace view public.v1_station_trend as
select t.system_id, t.station_id, t.profile_date, t.p_bike_x1000, t.p_dock_x1000
  from public.station_trend t
  join public.systems sy
    on sy.system_id = t.system_id and sy.is_active;

comment on view public.v1_station_trend is
  '公開 API 用。ポートの「ふだんの傾向」（72 個 ＝ 曜日種別 sat・sun_holiday・weekday × 0〜23 時。出せない時間は NULL）。停止中システムは除く。';

grant select on public.v1_station_trend to anon;
grant select on public.v1_station_trend to service_role;

-- **祝日も公開ビューで渡す。** `/v1` が 7 日 × 24 時間を組むとき、その日の曜日種別を
-- `packages/shared` の `dayType`（Python の `features/calendar.py` とゴールデンで突き合わせて
-- ある）で決めるのに要る。**暦の規則を SQL に 3 つめとして書かない**——年末年始とお盆は
-- 規則で、表にあるのは内閣府 CSV の祝日だけである（0023）。祝日は公開の情報で、出典の表示は
-- 既にある（`attribution.ts` の `cao-holidays`）。
create or replace view public.v1_holidays as
select h.holiday_date
  from public.jp_holidays h;

comment on view public.v1_holidays is
  '公開 API 用。国民の祝日・休日の日付（内閣府 CSV）。年末年始・お盆は暦の規則なので入っていない。';

grant select on public.v1_holidays to anon;
grant select on public.v1_holidays to service_role;

notify pgrst, 'reload schema';
