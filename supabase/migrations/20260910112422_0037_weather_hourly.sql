-- 0037 天気の予報を特徴量から引ける形にする（W4 プラン §6.4、開発プラン §5.3・§6.3）
--
-- 生アーカイブ（`weather-raw` の gzip JSON、0015）は 2026-09-07 15:17 UTC から溜まって
-- いるが、**まだ誰も読んでいない**。ここで「特徴量が引ける形」に落とす。
--
-- 開発プラン §5.3 の DDL（`weather_hourly(cluster_id, ts, ...)`）からの差分と、その理由：
--
--   * **`cluster_id` は無い。** 「約 50 クラスタ」の見積りは実態より 1 桁少なく、
--     実装は緯度 0.05°・経度 0.0625° の格子で **595 セル**になった（0015）。
--     鍵は**格子の整数添字**にする：`round(lat / 0.05)` と `round(lon / 0.0625)`。
--     浮動小数で突き合わせない（応答の座標は float32 で 26.2 が 26.199999 になる）
--   * **1 行 1 時刻ではなく、1 行が「1 発行 × 1 格子」で、時刻は配列。**
--     `station_forecasts`（1 行 1 ポート・水平は配列）と同じ形である。
--     **ローカルに本番の 1 発行（595 格子）を入れて両方を実測した**（2026-09-10）：
--
--       | 形 | 1 行 | 1 発行 | 1 日（24 発行） | 30 日 |
--       |---|---|---|---|---|
--       | 配列（これ） | 372 B | 216 kB | **5.2 MB** | **152 MB** |
--       | 長形式（1 行 1 時刻） | 122 B | 568 kB | 13 MB | 399 MB |
--
--     長形式は**元の生アーカイブ（6.4 MB/日、しかも 72 時間ぶん）の 2 倍**を占める。
--     派生物が一次ソースより大きいのはおかしい
--   * **`source` と `is_forecast` は無い。** 入るのは `jma_msm` の**予報**だけである
--     （W4-05）。列にすると「実況も入り得る」という約束をすることになる
--   * **`precipitation_probability` の列は作らない。** Open-Meteo の JMA モデルには
--     この変数が無く、要求しても **210,630 値すべて null** で返る（2026-09-10 に確認）。
--     常に NULL の列は、学習側に「欠損の扱い」を考えさせるだけ損をする
--   * **`wind_ms` ではなく `wind_kmh`。** Open-Meteo の `wind_speed_10m` の単位は
--     **km/h** である（`hourly_units` で確認）。名前を単位に合わせる（CLAUDE.md §3）
--
-- **配列の添字 k は `issued_hour + k 時間` の予報値**（SQL は 1 始まりなので `[k+1]`）。
-- 先頭を `issued_hour` にそろえてあるので、別に「先頭時刻」の列を持たなくてよい。

-- ────────────────────────────────────────────────────────────────
-- weather_hourly
-- ────────────────────────────────────────────────────────────────
create table public.weather_hourly (
  -- 格子の整数添字。`round(lat / 0.05)` と `round(lon / 0.0625)`
  -- （`packages/shared` の `WEATHER_GRID_LAT_STEP` / `WEATHER_GRID_LON_STEP`）
  cell_lat_idx smallint not null,
  cell_lon_idx smallint not null,
  -- 取得を始めた時刻を「時」に丸めた値（`v_weather_files.forecast_hour`、パスの一部）
  issued_hour  timestamptz not null,
  -- **これが「入手できた時刻」。** 特徴量は `available_at <= t` で引く（W3-06）。
  -- `issued_hour` で引いてはいけない（実測で常に 17.6 分あとに保存されている）
  available_at timestamptz not null,
  -- 直前 1 時間の降水量（mm、Open-Meteo の "Preceding hour sum"）
  precip_mm    real[] not null,
  -- 瞬時値（°C）
  temp_c       real[] not null,
  -- 瞬時値（km/h）
  wind_kmh     real[] not null,
  -- WMO の天気コード（瞬時）。**いまは特徴量に使わない**が、降水量に無い情報
  -- （降水量 0 のときの雲量 0/1/2/3）を持つので残す
  weather_code smallint[] not null,
  primary key (cell_lat_idx, cell_lon_idx, issued_hour),
  constraint weather_hourly_lengths check (
    array_length(precip_mm, 1) = array_length(temp_c, 1)
    and array_length(precip_mm, 1) = array_length(wind_kmh, 1)
    and array_length(precip_mm, 1) = array_length(weather_code, 1)
    and array_length(precip_mm, 1) between 1 and 24
  ),
  -- **入手より前に「発行」はできない。** 逆転した行は取り込みの誤りなので入れさせない
  constraint weather_hourly_available_after_issued check (available_at >= issued_hour)
);

comment on table public.weather_hourly is
  'jma_msm の予報。1 行が「1 発行 × 1 格子」で、時刻は配列（添字 k は issued_hour + k 時間）。特徴量は available_at <= t で引く。';
comment on column public.weather_hourly.available_at is
  '予報ファイルを入手できた時刻（v_weather_files.available_at）。特徴量はこれで引く。issued_hour で引いてはいけない。';
comment on column public.weather_hourly.precip_mm is
  '直前 1 時間の降水量（mm）。Open-Meteo の precipitation は "Preceding hour sum" で、添字 k の値は (issued_hour + (k-1) 時間, issued_hour + k 時間] の合計。';
comment on column public.weather_hourly.weather_code is
  'WMO の天気コード（瞬時）。特徴量には使っていない（降水量と重複するが、降水量 0 のときの雲量だけは独自の情報）。';

-- 引き方は 3 つとも `issued_hour` で範囲を切る：発行の一覧・1 日ぶんの読み出し・保守の削除
create index weather_hourly_issued_hour_idx on public.weather_hourly (issued_hour);

alter table public.weather_hourly enable row level security;
revoke all on table public.weather_hourly from anon, authenticated;
grant select, insert, update, delete on table public.weather_hourly to service_role;

-- ────────────────────────────────────────────────────────────────
-- v_weather_pending — まだ取り込んでいない発行
-- ────────────────────────────────────────────────────────────────
-- 取り込みジョブはこれを読むだけでよい。**「何が未処理か」の判断を 1 か所に置く。**
--
-- `n_cells`（要求した格子数）と取り込み済みの行数を比べるので、**途中で落ちた発行も
-- 未処理として出る**（6 分割のうち 3 つだけ取り込めた、など）。取り込みは冪等なので
-- そのまま入れ直せる。
--
-- **時刻で切っていない。** 保持期間（`run_maintenance` が 30 日で消す）を過ぎた発行は
-- ここに永久に居座るので、**読む側が `available_at >= 何日前` を必ず添える**
-- （`jobs/load_weather.py` の `WEATHER_LOAD_LOOKBACK_HOURS`）。ここに日数を書くと
-- 保守側の日数と二重管理になる。
create view public.v_weather_pending as
select f.hour_epoch_s,
       f.forecast_hour as issued_hour,
       f.available_at,
       f.n_cells,
       coalesce(l.n_loaded, 0) as n_loaded
  from public.v_weather_files f
  left join (
    select issued_hour, count(*)::integer as n_loaded
      from public.weather_hourly
     group by issued_hour
  ) l on l.issued_hour = f.forecast_hour
 where f.status = 'ok'
   and coalesce(l.n_loaded, 0) < f.n_cells;

comment on view public.v_weather_pending is
  'まだ weather_hourly に入っていない（または途中までしか入っていない）予報の発行。読む側は available_at の下限を必ず添える。';

revoke all on public.v_weather_pending from anon, authenticated;
grant select on public.v_weather_pending to service_role;

-- ────────────────────────────────────────────────────────────────
-- upsert_weather_hourly
-- ────────────────────────────────────────────────────────────────
-- 1 発行 595 行をまとめて書く。`upsert_forecasts`（0026）と同じ形。
create or replace function public.upsert_weather_hourly(p_rows jsonb)
returns integer
language plpgsql
security definer
set search_path = ''
set statement_timeout = '60s'
as $$
declare
  v_count integer;
begin
  insert into public.weather_hourly as w (
    cell_lat_idx, cell_lon_idx, issued_hour, available_at,
    precip_mm, temp_c, wind_kmh, weather_code
  )
  select r.cell_lat_idx, r.cell_lon_idx, r.issued_hour, r.available_at,
         r.precip_mm, r.temp_c, r.wind_kmh, r.weather_code
    from jsonb_to_recordset(p_rows) as r(
      cell_lat_idx smallint, cell_lon_idx smallint,
      issued_hour timestamptz, available_at timestamptz,
      precip_mm real[], temp_c real[], wind_kmh real[], weather_code smallint[]
    )
  on conflict (cell_lat_idx, cell_lon_idx, issued_hour) do update
     set available_at = excluded.available_at,
         precip_mm    = excluded.precip_mm,
         temp_c       = excluded.temp_c,
         wind_kmh     = excluded.wind_kmh,
         weather_code = excluded.weather_code;
  get diagnostics v_count = row_count;
  return v_count;
end;
$$;

comment on function public.upsert_weather_hourly(jsonb) is
  '予報をまとめて UPSERT する。1 行が「1 発行 × 1 格子」で、時刻は配列。同じ発行を入れ直しても結果は変わらない。';

revoke all on function public.upsert_weather_hourly(jsonb) from public, anon, authenticated;
grant execute on function public.upsert_weather_hourly(jsonb) to service_role;

-- ────────────────────────────────────────────────────────────────
-- 保守 — 30 日より古い予報を消す
-- ────────────────────────────────────────────────────────────────
-- **一次ソースは Storage の生 gzip JSON**（0015）で、この表はそこから作り直せる派生物
-- である。だから保持は「作り直しに使う窓」だけあればよい。30 日は `feed_fetch_log` と
-- 同じで、学習サンプルの作り直しに十分に足りる。
--
-- 容量：595 格子 × 24 発行 = 14,280 行/日、**約 5.2 MB/日**（実測）。30 日で
-- **約 152 MB**（開発プラン §4.5a の容量に加算した）。
--
-- **`v_weather_pending` の読み手はこれより短い窓で引く**こと。消えた発行が
-- 「未処理」として蘇り、取り込み → 削除を毎日繰り返すのを避けるため。
create or replace function public.run_maintenance(p_keep_days integer default 60)
returns jsonb
language plpgsql
security definer
set search_path = ''
set statement_timeout = '10min'
as $$
declare
  v_run       bigint;
  v_created   integer;
  v_dropped   integer;
  v_logs      integer;
  v_closed    integer;
  v_inference integer;
  v_weather   integer;
  v_details   integer := 0;
  v_result    jsonb;
begin
  if not pg_try_advisory_xact_lock(8423, 3) then
    return jsonb_build_object('status', 'locked');
  end if;
  v_run := public.job_started('maintain_partitions');

  begin
    v_created := public.ensure_snapshot_partitions(2);
    v_dropped := public.drop_expired_snapshot_partitions(p_keep_days);

    delete from public.feed_fetch_log where fetched_at < now() - interval '30 days';
    get diagnostics v_logs = row_count;

    -- **閉じられなかった行を閉じる**（0032）。消すのではなく `failed` にする
    update public.inference_log
       set status = 'failed',
           finished_at = clock_timestamp(),
           error = 'never_finished'
     where status = 'running' and generated_at < now() - interval '1 hour';
    get diagnostics v_closed = row_count;

    -- **`ok` だけを消す。** 失敗・二重抑止・閉じられなかった行は残す（0031 の冒頭）
    delete from public.inference_log
     where status = 'ok' and generated_at < now() - interval '90 days';
    get diagnostics v_inference = row_count;

    -- 天気は派生物。生 gzip JSON から作り直せる（0037）
    delete from public.weather_hourly where issued_hour < now() - interval '30 days';
    get diagnostics v_weather = row_count;

    -- cron.job_run_details は自動削除されない。毎分ジョブで月 4.3 万行たまる（§4.3 の 3）
    begin
      delete from cron.job_run_details where end_time < now() - interval '7 days';
      get diagnostics v_details = row_count;
    exception when insufficient_privilege then
      v_details := -1;  -- 権限が無い環境では諦める（ローカルなど）
    end;

    v_result := jsonb_build_object(
      'partitions_created', v_created, 'partitions_dropped', v_dropped,
      'fetch_logs_deleted', v_logs, 'inference_runs_closed', v_closed,
      'inference_logs_deleted', v_inference, 'weather_rows_deleted', v_weather,
      'cron_details_deleted', v_details);
    perform public.job_finished(v_run, 'ok', v_result);
    return v_result;
  exception when others then
    perform public.job_finished(v_run, 'failed',
      jsonb_build_object('error', sqlerrm, 'code', sqlstate));
    return jsonb_build_object('status', 'failed', 'error', sqlerrm);
  end;
end;
$$;

comment on function public.run_maintenance(integer) is
  'パーティションの作成・削除、取得ログ 30 日超・推論ログ（ok のみ）90 日超・天気 30 日超・cron.job_run_details 7 日超の削除、閉じられなかった推論（1 時間超の running）を failed にする。';

revoke all on function public.run_maintenance(integer) from public, anon, authenticated;

-- ────────────────────────────────────────────────────────────────
-- 監視 — 取り込みジョブを見張る
-- ────────────────────────────────────────────────────────────────
-- **ジョブを足したら `monitored_jobs` にも足す**（W4 プラン §12 の 116 で決めた規則）。
-- 毎時なので、`missing_after` は 3 時間。1 回落ちただけでは鳴らず、2 回続けば鳴る。
-- 止まっても取りこぼしは次の実行が拾う（`v_weather_pending` を見て入れ直す）ので、
-- 少し鈍くてよい。
--
-- **`cron_job_name` は NULL。** pg_cron ではなく Vercel Cron である（0022 の検査の
-- 対象ではない。`archive_weather` と同じ扱い）。
insert into public.monitored_jobs
  (job_name, expected_every, missing_after, is_active, cron_job_name, note)
values
  ('load_weather', interval '1 hour', interval '3 hours', true, null,
   'Vercel Cron 毎時 :25 UTC。weather-raw の gzip JSON を weather_hourly に取り込む。'
   '止まると天気の特徴量が古い発行のままになる（v_weather_pending に溜まる）')
on conflict (job_name) do update
  set expected_every = excluded.expected_every,
      missing_after  = excluded.missing_after,
      is_active      = excluded.is_active,
      cron_job_name  = excluded.cron_job_name,
      note           = excluded.note;

notify pgrst, 'reload schema';
