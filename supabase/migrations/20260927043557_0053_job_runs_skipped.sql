-- ────────────────────────────────────────────────────────────────
-- 0053 — `job_runs.status` に `skipped` を足す（W6 プランの所見 207、開発プランの D-38）
-- ────────────────────────────────────────────────────────────────
-- 日次評価（`evaluate_daily`）は、予測ログが 2 システムとも無い日を `skipped` で閉じる
-- （W5 プランの PR L の完了条件 5「ログの無い日は skipped で終わる。失敗にしない」）。
-- **0003 の `job_runs_status_valid` は `running`・`ok`・`failed` しか受けない。**
-- `job_finished` が弾かれ（23514）、Python の記録口はその失敗を飲むので、行は
-- **`running` のまま残る**——見張りから見ると「殺された回」と区別がつかない。
-- 本番ではまだ起きていない（9/18 に Actions へ移ってからの 14 回はすべて ok。9/27 に確認）。
--
-- **広げるだけ。** 既存の行はすべて新しい制約を満たす。列・既定値・関数は変えない。
-- 外して付け直すのは 1 つの `alter table` の中で行う（あいだに制約の無い瞬間を作らない）。
--
-- **監視も変えない。** `check_jobs_missing`（0020）は `status = 'ok'` だけを成功と数える
-- ので、**`skipped` が続けば 30 時間で鳴る**。これは意図どおりである：推論と予測ログの
-- 見張り（`inference_stale`・`forecast_log_failed`）が鳴るのは上流が止まったときだけで、
-- **評価の側の不具合でログが見えない日は、ここでしか気づけない**（0050 が見張りを置いた
-- 理由と同じ）。`check_jobs_failed` は `failed` だけを見るので、`skipped` では鳴らない。
--
-- **Python の記録口（`jobs/recording.py` の `FINISHED_STATUSES`）は、この制約の最後の定義と
-- 検査で突き合わせる**（`tests/test_recording.py`）。値を足すときは両方を同じ PR で変える。
-- ────────────────────────────────────────────────────────────────

alter table public.job_runs
  drop constraint if exists job_runs_status_valid,
  add constraint job_runs_status_valid
    check (status in ('running', 'ok', 'failed', 'skipped'));

comment on column public.job_runs.status is
  'running（実行中）/ ok / failed / skipped（やることが無かった回。いまは evaluate_daily の予測ログが 2 システムとも無い日だけ。0053）。監視（check_jobs_missing）は ok だけを成功と数える。';

-- **障害のときに最初に読まれる場所**に、`skipped` の読み方を足す（0051 と同じ理由）。
update public.monitored_jobs
   set note = 'GitHub Actions（evaluate-daily.yml）06:40 JST。前日ぶんの実運用 Brier を '
              'model_daily_metrics に入れる。Vercel では山 2.26 GB が枠（2 GB）に入らず '
              '2026-09-17・18 が死んだので移した（D-26）。止まっても配信は無事で、'
              '予測ログ（12 か月）から後で測り直せる（workflow_dispatch の date 入力）。'
              '予測ログが 2 システムとも無い日は skipped で閉じる。ok に数えないので、続けば '
              '30 時間で鳴る（評価の側の不具合でログが見えない日も、ここで気づく。0053）'
 where job_name = 'evaluate_daily';

notify pgrst, 'reload schema';
