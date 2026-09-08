-- 0026 予測テーブルと推論の記録（W3 プラン §5.10、開発プラン §5.3・§8.2）
--
-- **ここで初めて予測系のテーブルを作る。** W1-10 の「使う予定のない空テーブルを先に
-- 作らない」に従い、実際に書き始める段まで待った。
--
-- 開発プラン §5.3 の DDL からの差分：
--   * `station_key integer references stations` は実装に存在しない。台帳の主キーは
--     `(system_id, station_id)` なので、こちらに合わせる（0003 以来の実装差分）
--   * `bikes_q10 / q50 / q90`（台数の予測区間）は v1.1 の列なので作らない
--   * `inference_log` に `status` と `finished_at` を足す。**掴んでから書き終える**
--     形（`job_runs` と同じ）にするため
--   * `model_versions` は作らない（W3 プラン §8。W4 でモデルを登録し始めてから）
--
-- **確率は ×1000 の整数で持つ。** 20,745 行 × 10 水平 × 2 指標を倍精度で持つと
-- 3.3 MB、smallint なら 830 kB。確率の分解能は 0.001 で足りる（表示は 10% 刻み）。

-- ────────────────────────────────────────────────────────────────
-- 予測
-- ────────────────────────────────────────────────────────────────
-- **1 ポート 1 行で、水平は配列。** 1 行 1 水平にすると 20 万行を 5 分毎に書き換える
-- ことになる。配列なら 20,745 行で、更新は HOT に乗る（`fillfactor = 50`）。
create table public.station_forecasts (
  system_id        text not null,
  station_id       text not null,
  generated_at     timestamptz not null,
  -- 予測の基準になった観測時刻。**これで鮮度を判断する**（`generated_at` ではない）
  base_observed_at timestamptz not null,
  model_version    text not null,
  horizons_min     smallint[] not null,
  p_bike_x1000     smallint[] not null,
  p_dock_x1000     smallint[] not null,
  -- 0: 参考 / 1: 低 / 2: 中 / 3: 高（データ鮮度・気候値の有無）
  confidence       smallint not null,
  primary key (system_id, station_id),
  foreign key (system_id, station_id) references public.stations (system_id, station_id),
  constraint station_forecasts_confidence_range check (confidence between 0 and 3),
  constraint station_forecasts_lengths check (
    array_length(horizons_min, 1) = array_length(p_bike_x1000, 1)
    and array_length(horizons_min, 1) = array_length(p_dock_x1000, 1)
  ),
  constraint station_forecasts_probability_range check (
    0 <= all (p_bike_x1000) and 1000 >= all (p_bike_x1000)
    and 0 <= all (p_dock_x1000) and 1000 >= all (p_dock_x1000)
  )
) with (fillfactor = 50);

comment on table public.station_forecasts is
  'ポート毎の短期予測。1 行 1 ポートで、水平は配列。5 分毎に全件 UPSERT する（開発プラン §8.1）。';
comment on column public.station_forecasts.base_observed_at is
  '予測の基準になった観測時刻。鮮度はこれで判断する（generated_at は計算した時刻）。';
comment on column public.station_forecasts.confidence is
  '0 参考 / 1 低 / 2 中 / 3 高。鮮度と気候値の有無で決まる（W3 の段 8 では気候値が無いので最大 2）。';

alter table public.station_forecasts enable row level security;
revoke all on table public.station_forecasts from anon, authenticated;

-- ────────────────────────────────────────────────────────────────
-- 推論の記録
-- ────────────────────────────────────────────────────────────────
-- **`(system_id, base_observed_at)` の一意制約が二重推論を止める。**
-- 開発プラン §8.2 は `pg_try_advisory_lock` の併用も書いているが、**PostgREST 経由では
-- 効かない**：セッションのロックは接続プールに返された後の扱いが決まらず、
-- トランザクションのロックは 1 リクエストで消える（W3 プラン §12 の 103）。
-- 掴むのは「行を 1 つ入れられたか」で、これは接続をまたいで正しく効く。
create table public.inference_log (
  id               bigint generated always as identity primary key,
  system_id        text not null references public.systems (system_id),
  generated_at     timestamptz not null default now(),
  finished_at      timestamptz,
  base_observed_at timestamptz not null,
  model_version    text not null,
  status           text not null default 'running',
  n_rows           integer not null default 0,
  duration_ms      integer,
  error            text,
  unique (system_id, base_observed_at),
  constraint inference_log_status check (status in ('running', 'ok', 'failed', 'skipped'))
);

comment on table public.inference_log is
  '推論 1 回ぶんの記録。(system_id, base_observed_at) の一意制約が二重推論を止める（開発プラン §8.2）。';

create index inference_log_recent_idx on public.inference_log (system_id, generated_at desc);

alter table public.inference_log enable row level security;
revoke all on table public.inference_log from anon, authenticated;

-- ────────────────────────────────────────────────────────────────
-- RPC
-- ────────────────────────────────────────────────────────────────
-- **掴めたときだけ id を返す。** 同じ観測時刻に対する 2 度目は `claimed = false` で、
-- 呼ぶ側は何もせずに 200 を返す（Vercel Cron の二重起動は無害になる）。
create or replace function public.begin_inference(
  p_system_id        text,
  p_base_observed_at timestamptz,
  p_model_version    text
)
returns jsonb
language plpgsql
security definer
set search_path = ''
set statement_timeout = '30s'
as $$
declare
  v_id bigint;
begin
  if not exists (select 1 from public.systems where system_id = p_system_id) then
    raise exception '未知のシステムです: %', p_system_id using errcode = '22023';
  end if;

  insert into public.inference_log (system_id, base_observed_at, model_version)
  values (p_system_id, p_base_observed_at, p_model_version)
  on conflict (system_id, base_observed_at) do nothing
  returning id into v_id;

  if v_id is null then
    return jsonb_build_object('claimed', false);
  end if;
  return jsonb_build_object('claimed', true, 'id', v_id);
end;
$$;

comment on function public.begin_inference(text, timestamptz, text) is
  '推論を掴む。同じ base_observed_at が既にあれば claimed = false を返す（二重推論を止める）。';

create or replace function public.finish_inference(
  p_id          bigint,
  p_status      text,
  p_n_rows      integer,
  p_duration_ms integer,
  p_error       text default null
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  update public.inference_log
     set status = p_status,
         finished_at = clock_timestamp(),
         n_rows = p_n_rows,
         duration_ms = p_duration_ms,
         error = left(p_error, 500)
   where id = p_id;
end;
$$;

comment on function public.finish_inference(bigint, text, integer, integer, text) is
  '推論の結果を記録する。finished_at は clock_timestamp()（now() はトランザクション開始時刻で動かない）。';

-- **予測はまとめて書く。** 20,745 行を 1 行ずつ往復すると話にならない。
-- `jsonb` の配列で受け、1 トランザクションで UPSERT する。
create or replace function public.upsert_forecasts(p_rows jsonb)
returns integer
language plpgsql
security definer
set search_path = ''
set statement_timeout = '60s'
as $$
declare
  v_count integer;
begin
  insert into public.station_forecasts as f (
    system_id, station_id, generated_at, base_observed_at,
    model_version, horizons_min, p_bike_x1000, p_dock_x1000, confidence
  )
  select r.system_id, r.station_id, r.generated_at, r.base_observed_at,
         r.model_version, r.horizons_min, r.p_bike_x1000, r.p_dock_x1000, r.confidence
    from jsonb_to_recordset(p_rows) as r(
      system_id text, station_id text, generated_at timestamptz, base_observed_at timestamptz,
      model_version text, horizons_min smallint[], p_bike_x1000 smallint[],
      p_dock_x1000 smallint[], confidence smallint
    )
  on conflict (system_id, station_id) do update
     set generated_at = excluded.generated_at,
         base_observed_at = excluded.base_observed_at,
         model_version = excluded.model_version,
         horizons_min = excluded.horizons_min,
         p_bike_x1000 = excluded.p_bike_x1000,
         p_dock_x1000 = excluded.p_dock_x1000,
         confidence = excluded.confidence;
  get diagnostics v_count = row_count;
  return v_count;
end;
$$;

comment on function public.upsert_forecasts(jsonb) is
  '予測をまとめて UPSERT する。1 ポート 1 行で、水平は配列。台帳に無いポートは外部キーで弾く。';

revoke all on function public.begin_inference(text, timestamptz, text) from public, anon, authenticated;
revoke all on function public.finish_inference(bigint, text, integer, integer, text)
  from public, anon, authenticated;
revoke all on function public.upsert_forecasts(jsonb) from public, anon, authenticated;

-- **本番は「新規オブジェクトの自動公開」が OFF** で、関数の EXECUTE が service_role に
-- 既定で付かない（W3 プラン §12 の 90）。PostgREST から呼ぶので明示的に付ける。
grant execute on function public.begin_inference(text, timestamptz, text) to service_role;
grant execute on function public.finish_inference(bigint, text, integer, integer, text) to service_role;
grant execute on function public.upsert_forecasts(jsonb) to service_role;

notify pgrst, 'reload schema';
