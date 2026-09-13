-- ────────────────────────────────────────────────────────────────
-- 0046 — ポート別・1 時間ごとの実績（`station_hourly`）
-- ────────────────────────────────────────────────────────────────
-- 詳細画面の「直近 24 時間」（開発プラン §9.2、W5 プラン §6.6 の PR F）が読む。
--
-- ## なぜビューではなく表なのか（**測ってから決めた**）
--
-- 素直なビュー（`status_snapshots` の配列を要求のたびに開く）を本番で測ると、
-- **1 ポート 1 日ぶんで 616 ms** だった。行数は少ない（24 時間で HELLO 288 行・
-- ドコモ 1,040 行）が、**1 行が 4 本 × 約 15,000 要素の配列を抱えていて、TOAST の
-- 展開がそのまま効く**。しかも PR H（お気に入り）は**このエンドポイントを 20 件まとめて
-- 叩く**ので、1 画面で 12 秒の DB 時間になる（W5 プラン §12 の 151）。
--
-- **毎時 1 回まとめておけば、読む側は索引 1 本で済む。** 集計そのものは本番で
-- **3.5 秒**（1 時間ぶん・両システム・約 43 万行の unnest）で、背景の仕事としては軽い。
--
-- ## 何を数えるか
--
-- **貸出と返却の両方が観測できた格子点だけを数える**（`features/profile.py` の
-- `usable_points` と同じ規律）。片方だけ観測できた点を数えると、`bikes_mean` と
-- `docks_mean` が別の母数の平均になる（W5 プラン §12 の 138 と同じ形）。
--
-- **観測が 1 度も無かった時間帯は行を作らない。** 0 を入れると「0 台だった」に見える。
--
-- ## 保持（26 時間）
--
-- **`run_maintenance`（日次）には置かない。** 保持が 26 時間なので、日次で掃除すると
-- 最大 48 時間ぶん溜まる。**書く側と同じ毎時の仕事で掃除する**ほうが、表の大きさが
-- 作りから決まる。26 は「24 時間 ＋ 集計の遅れ 2 時間」である。
--
-- 見込み：20,700 ポート × 26 時間 ＝ 約 54 万行・**70 MB**（索引込み）。本番の DB は
-- いま 260 MB で、上限 8 GB・警報 6 GB（開発プラン §4.5）。
-- ────────────────────────────────────────────────────────────────

create table if not exists public.station_hourly (
  system_id  text        not null,
  station_id text        not null,
  -- その時間の始まり（UTC で切る。表示の暦は読む側が決める）
  hour_start timestamptz not null,
  -- **貸出と返却の両方が観測できた**スナップショットの数
  n          smallint    not null,
  bikes_mean real        not null,
  docks_mean real        not null,
  primary key (system_id, station_id, hour_start),
  constraint station_hourly_n_positive check (n > 0),
  constraint station_hourly_means_non_negative check (bikes_mean >= 0 and docks_mean >= 0),
  constraint station_hourly_hour_aligned check (hour_start = date_trunc('hour', hour_start))
);

-- 掃除は `hour_start` で引く（主鍵の先頭は `system_id` なので別に要る）
create index if not exists station_hourly_hour_idx on public.station_hourly (hour_start);

comment on table public.station_hourly is
  'ポート別・1 時間ごとの実績（直近 26 時間）。status_snapshots の配列を毎時まとめたもの。貸出と返却の両方が観測できた点だけを数える。観測の無い時間帯は行を作らない。';
comment on column public.station_hourly.n is
  'その時間に、貸出と返却の両方が観測できたスナップショットの数。片方だけの点は数えない。';

alter table public.station_hourly enable row level security;
revoke all on table public.station_hourly from anon, authenticated;
grant select, insert, update, delete on table public.station_hourly to service_role;

-- ────────────────────────────────────────────────────────────────
-- 毎時の集計（`rollup_station_hourly`）
-- ────────────────────────────────────────────────────────────────
-- **完了した時間だけを作る。** いま進行中の時間を入れると、途中経過が「その時間の
-- 平均」として配られる。
--
-- **既定で 2 時間ぶんを作り直す**（冪等）。1 回飛んでも次の回で埋まり、同じ回を 2 度
-- 走らせても結果が変わらない。さかのぼるときは引数を大きくする
-- （`select public.rollup_station_hourly(24)`）。
--
-- **アドバイザリロックで同時実行を防ぐ**（CLAUDE.md §2 の原則 2）。鍵は `run_maintenance`
-- と同じ名前空間（8423）の別の番号。

create or replace function public.rollup_station_hourly(p_hours integer default 2)
returns jsonb
language plpgsql
security definer
set search_path = ''
set statement_timeout = '5min'
as $$
declare
  v_run     bigint;
  v_from    timestamptz;
  v_to      timestamptz;
  v_written integer;
  v_pruned  integer;
begin
  if p_hours < 1 or p_hours > 720 then
    raise exception 'p_hours は 1〜720 です: %', p_hours using errcode = '22023';
  end if;
  -- **掴めなければ何もしない**（前の回がまだ走っている）
  if not pg_try_advisory_xact_lock(8423, 6) then
    return jsonb_build_object('status', 'locked');
  end if;
  v_run := public.job_started('rollup_station_hourly');

  v_to   := date_trunc('hour', now());
  v_from := v_to - make_interval(hours => p_hours);

  insert into public.station_hourly (system_id, station_id, hour_start, n, bikes_mean, docks_mean)
  select ss.system_id,
         st.station_id,
         date_trunc('hour', ss.observed_at) as hour_start,
         count(*)::smallint                 as n,
         avg(v.bikes)::real                 as bikes_mean,
         avg(v.docks)::real                 as docks_mean
    from public.status_snapshots ss
    -- 配列は `stations.idx` の順（0 起点）で並ぶ。`arr[idx + 1]` がそのポートの値
    cross join lateral unnest(ss.bikes, ss.docks) with ordinality as v(bikes, docks, pos)
    join public.stations st
      on st.system_id = ss.system_id and st.idx = v.pos - 1
   where ss.observed_at >= v_from
     and ss.observed_at <  v_to
     -- **両方が観測できた点だけ。** -1 は「登録済みだが観測されていない」
     and v.bikes >= 0
     and v.docks >= 0
   group by 1, 2, 3
      on conflict (system_id, station_id, hour_start) do update
      set n = excluded.n, bikes_mean = excluded.bikes_mean, docks_mean = excluded.docks_mean;
  get diagnostics v_written = row_count;

  -- **書く側と同じ回で掃除する**（保持 26 時間 ＝ 24 ＋ 集計の遅れ 2）
  delete from public.station_hourly where hour_start < v_to - interval '26 hours';
  get diagnostics v_pruned = row_count;

  perform public.job_finished(
    v_run, 'ok',
    jsonb_build_object('from', v_from, 'to', v_to, 'written', v_written, 'pruned', v_pruned));
  return jsonb_build_object('status', 'ok', 'written', v_written, 'pruned', v_pruned);
end;
$$;

comment on function public.rollup_station_hourly(integer) is
  'status_snapshots を 1 時間ごとにまとめて station_hourly に入れ、26 時間より古い行を消す。完了した時間だけを作り、既定で直近 2 時間ぶんを作り直す（冪等）。';

revoke all on function public.rollup_station_hourly(integer) from public, anon, authenticated;

-- **毎時 :12。** :07（Parquet 化）と :17（天気）の間に置いて、重ならないようにする
select cron.schedule('rollup_station_hourly', '12 * * * *',
                     $$select public.rollup_station_hourly()$$);

-- 止まったら鳴らす（0036 の仕組み）。**2 回落ちたら**気づける幅にする
insert into public.monitored_jobs (job_name, expected_every, missing_after, note, cron_job_name)
values ('rollup_station_hourly', interval '1 hour', interval '3 hours',
        'pg_cron 毎時 :12。status_snapshots を 1 時間ごとにまとめる。止まると詳細画面の「直近 24 時間」に穴が開く（作り直しは select public.rollup_station_hourly(24)）。',
        'rollup_station_hourly')
    on conflict (job_name) do update
   set expected_every = excluded.expected_every,
       missing_after  = excluded.missing_after,
       note           = excluded.note,
       cron_job_name  = excluded.cron_job_name;

-- ────────────────────────────────────────────────────────────────
-- 公開ビュー
-- ────────────────────────────────────────────────────────────────
-- **基底テーブルは公開経路から名指ししない**（0018 と同じ境界）。列は素通しだが、
-- 名前を `v1_` に揃えることで「外から読んでよいもの」が一覧で分かる状態を保つ。

-- **停止したシステムは出さない。** `v1_stations_current` と `v1_station_neighbors` が
-- そうしているので揃える（0018 の 3 と同じ理由）——片方だけが答えると、詳細画面が
-- 「そのポートは無い」と「直近 24 時間はこれ」を同時に返すことになる。
create or replace view public.v1_station_hourly as
select h.system_id, h.station_id, h.hour_start, h.n, h.bikes_mean, h.docks_mean
  from public.station_hourly h
  join public.systems sy
    on sy.system_id = h.system_id and sy.is_active;

comment on view public.v1_station_hourly is
  '公開 API 用。ポート別・1 時間ごとの実績（直近 26 時間）。観測の無い時間帯は行が無い。停止中システムは除く。';

grant select on public.v1_station_hourly to anon;
grant select on public.v1_station_hourly to service_role;

notify pgrst, 'reload schema';
