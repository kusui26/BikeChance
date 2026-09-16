-- ────────────────────────────────────────────────────────────────
-- 0050 — 実運用の日次評価（`model_daily_metrics`）
-- ────────────────────────────────────────────────────────────────
-- **配った確率を、実際に起きたことと突き合わせた成績**（開発プラン §8.5、
-- W5 プラン §6.12 の PR L）。`/ml/evaluate` が前日ぶんを 1 回で入れる。
--
-- ## オフラインの評価と何が違うのか
--
-- `evaluate_baselines`（`docs/260908_eda_02_baseline.md`）は**抽出した標本**の上で
-- 測る。こちらは**配った全ポート・全サイクル**の上で測る——**標本ではなく全数**である。
--
-- **だから比べるのはオフラインの「重み付き」のほう**である。逆抽出確率の重みは
-- 「標本から母集団の値を推定する」ためのもので、**その推定値と、全数で数えた値が
-- 同じものを指す**。重み無しの平均は標本の偏り（難所を 4 倍濃く取ってある）が
-- そのまま残るので、実運用の値とは**ずれるのが正しい**（W5 プラン §12 の 165）。
--
-- 2026-09-16 の PR I で抽出が一様になったので、**09-16 以降の日はオフラインの
-- 重み付きと重み無しが一致する**——そこから先は選ぶ必要が無くなる（D-25）。
--
-- ## なぜ水平ごとに 1 行なのか（「全水平まとめて」の行を作らない）
--
-- **`h_min` に「全体」を表す番兵を置かない。** 0 は水平として有効な値ではないが、
-- 「0 は観測であって『分からない』ではない」（0047 と同じ規律）を崩す入口になる。
--
-- **まとめた値は読む側が作れる。** Brier も log loss も陽性率も**行の平均**なので、
-- `n` で重み付けして足せば全水平ぶんの値がそのまま出る（バケツも同じ）。
-- **ECE だけは作れない**——が、ECE は水平ごとに見るものである（オフラインの §8 も
-- 水平で割っている）。
--
-- ## 参照は B0（持続）
--
-- BSS の基準はオフラインと同じ **B0**（`eval/harness.py` の「B0 が基準」）。
-- B0 は `bikes(t) >= 1`／`docks(t) >= 1` そのもので、**実測だけから作れる**。
-- 気候値（B2）を参照にするには成果物を読んで引き直す必要があり、**同じ問いに 2 つの
-- 数が出る道**を増やす。W6（LightGBM v1）で「気候値を何割上回るか」が採用基準に
-- なるとき、そこで足すかを決める（W5 プラン §6.12 の実装）。
--
-- ## 大きさ
--
-- 1 日 1 版あたり **2 システム × 2 ターゲット × 10 水平 × 7 バケツ ＝ 280 行**。
-- 1 年で約 10 万行・**10 MB 未満**なので、**保持の規則は置かない**（`run_maintenance`
-- に足さない）。**配った版の成績を消さない**ほうが後から効く。
--
-- `model_versions` への外部キーは意図したもの：**成績の残っている版は消せない。**
-- ────────────────────────────────────────────────────────────────

create table if not exists public.model_daily_metrics (
  model_version text     not null references public.model_versions (model_version),
  -- **JST の暦日。** 予測の基準時刻（`generated_at`）が属する日で切る
  metric_date   date     not null,
  system_id     text     not null references public.systems (system_id),
  target        text     not null,
  h_min         smallint not null,
  -- `eval/dataset.py` の `BUCKET_LABELS` ＋ 全体（`eval/slices.py` の `ALL`）
  bucket        text     not null,
  n             integer  not null,
  -- 実測の陽性率。**Brier の大きさを読むときの基準**になる
  positives     real     not null,
  brier         real     not null,
  log_loss      real     not null,
  -- **判定に使うのは等頻度のほう**（`ECE_QUANTILE_BINS`）。等幅も残す——過去の記録は
  -- すべて等幅で、片方だけにすると「悪くなった」のか「見えるようになった」のかが
  -- 分からなくなる（W5 プラン §6.3）
  ece           real     not null,
  ece_uniform   real     not null,
  brier_b0      real     not null,
  -- B0 からの改善（BSS）。**B0 の Brier が 0 なら測れない**ので NULL
  skill_vs_b0   real,
  primary key (model_version, metric_date, system_id, target, h_min, bucket),
  constraint model_daily_metrics_target check (target in ('bike', 'dock')),
  constraint model_daily_metrics_n_positive check (n > 0),
  constraint model_daily_metrics_h_min_positive check (h_min > 0),
  constraint model_daily_metrics_positives check (positives >= 0 and positives <= 1),
  constraint model_daily_metrics_scores check (brier >= 0 and ece >= 0 and brier_b0 >= 0)
);

-- 「この日の全システム」で引く（主キーの先頭は版なので別に要る）
create index if not exists model_daily_metrics_date_idx
  on public.model_daily_metrics (metric_date, system_id);

comment on table public.model_daily_metrics is
  '配った確率の日次成績（実運用 Brier）。全ポート・全サイクルの全数なので抽出の重みは無い。'
  'オフラインと比べるときは「重み付き」のほうと並べる（どちらも母集団の値を指す）。'
  '水平ごとに 1 行で、全水平の値は n で重み付けして足せば出る。';
comment on column public.model_daily_metrics.metric_date is
  'JST の暦日。予測の基準時刻（forecast-log の覚え書きの generated_at）が属する日。';
comment on column public.model_daily_metrics.bucket is
  '基準時刻の台数バケツ（0 / 1 / 2 / 3-5 / 6-10 / 11+）と、割らない「全体」。';
comment on column public.model_daily_metrics.brier_b0 is
  '同じ行の上での B0（持続）の Brier。BSS の基準はオフラインと揃えて B0 にしてある。';

alter table public.model_daily_metrics enable row level security;
revoke all on table public.model_daily_metrics from anon, authenticated;
grant select, insert, update, delete on table public.model_daily_metrics to service_role;

-- ────────────────────────────────────────────────────────────────
-- 書き込みは RPC 1 つ（`upsert_forecasts` と同じ形）
-- ────────────────────────────────────────────────────────────────
-- **同じ日を 2 回走らせても行が増えない**（W5 プラン §6.12 の完了条件 2）。
-- 主キーで衝突させ、値を入れ替える。

create or replace function public.upsert_model_daily_metrics(p_rows jsonb)
returns integer
language plpgsql
security definer
set search_path = ''
set statement_timeout = '60s'
as $$
declare
  v_count integer;
begin
  insert into public.model_daily_metrics as m (
    model_version, metric_date, system_id, target, h_min, bucket,
    n, positives, brier, log_loss, ece, ece_uniform, brier_b0, skill_vs_b0
  )
  select r.model_version, r.metric_date, r.system_id, r.target, r.h_min, r.bucket,
         r.n, r.positives, r.brier, r.log_loss, r.ece, r.ece_uniform, r.brier_b0, r.skill_vs_b0
    from jsonb_to_recordset(p_rows) as r(
      model_version text, metric_date date, system_id text, target text,
      h_min smallint, bucket text, n integer, positives real, brier real,
      log_loss real, ece real, ece_uniform real, brier_b0 real, skill_vs_b0 real
    )
  on conflict (model_version, metric_date, system_id, target, h_min, bucket) do update
     set n           = excluded.n,
         positives   = excluded.positives,
         brier       = excluded.brier,
         log_loss    = excluded.log_loss,
         ece         = excluded.ece,
         ece_uniform = excluded.ece_uniform,
         brier_b0    = excluded.brier_b0,
         skill_vs_b0 = excluded.skill_vs_b0;
  get diagnostics v_count = row_count;
  return v_count;
end;
$$;

comment on function public.upsert_model_daily_metrics(jsonb) is
  '日次評価の結果をまとめて UPSERT する。主キーで衝突させるので、同じ日を 2 回測っても行は増えない。';

revoke all on function public.upsert_model_daily_metrics(jsonb) from public, anon, authenticated;
grant execute on function public.upsert_model_daily_metrics(jsonb) to service_role;

-- ────────────────────────────────────────────────────────────────
-- 見張り（0020 の `check_jobs_missing`）
-- ────────────────────────────────────────────────────────────────
-- **閾値は他の日次ジョブと揃える**：`expected_every = 1 日`、`missing_after = 30 時間`。
-- **`cron_job_name` は NULL**——pg_cron ではなく Vercel Cron なので、0022 の
-- 「pg_cron に登録されているか」の検査の対象ではない（`build_reference` と同じ扱い）。
--
-- **止まっても配信は無事である。** 止まると**測っていない日**が増えるだけだが、
-- 予測ログは 12 か月残るので**後から測り直せる**（`?date_text=` で日を指定できる）。
-- それでも鳴らすのは、**測っていないことに気づかないまま週次ダイジェストを読む**のが
-- いちばん困るからである。

insert into public.monitored_jobs
  (job_name, expected_every, missing_after, is_active, cron_job_name, note)
values
  ('evaluate_daily', interval '1 day', interval '30 hours', true, null,
   'Vercel Cron 04:40 JST。前日ぶんの実運用 Brier を model_daily_metrics に入れる。'
   '止まっても配信は無事で、予測ログ（12 か月）から後で測り直せる（?date= で指定）')
on conflict (job_name) do update
  set expected_every = excluded.expected_every,
      missing_after  = excluded.missing_after,
      is_active      = excluded.is_active,
      cron_job_name  = excluded.cron_job_name,
      note           = excluded.note;
