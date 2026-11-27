-- ────────────────────────────────────────────────────────────────
-- 0065 — 日次の枠を起こす仕事（`/api/jobs/daily-dispatch`）を見張りに入れる
--        （開発プランの D-50、W13 プランの W13-07・§4.6。W13 の PR D）
-- ────────────────────────────────────────────────────────────────
-- GitHub Actions の日次の定時（特徴量 00:40・06:00、日次評価 06:40 JST）は 4〜11 時間遅れる
-- （W12 プランの §10.2 の 6）。10:15・10:35 JST の Vercel Cron が、来ていない枠だけを
-- `workflow_dispatch` で起こし、`job_runs` に `dispatch_daily` として残す。起こしたときは
-- `detail.dispatched` にワークフローの名前を書く——**同じ日に同じものを 2 度起こさない鍵**。
--
--   * **起こしても起こさなくても `ok`**（そろっていて何もしなかった回も、仕事は果たした）。
--     同時に呼ばれた回だけが `skipped`（0053 の「やることが無かった回」）
--   * **見張りは日次のジョブと同じ**：`expected_every = 1 日`、`missing_after = 30 時間`。
--     1 日 2 回起動するので、鳴るのは 2 回とも落ちた（来なかった）日
--   * **`failure_note` は付けない**（0041：再実行では取り返せない失敗にだけ書く）。起こせなかった
--     枠は手で起こせば取り返せる。人がすること（GitHub のトークンの期限と権限を確かめ、来ていない
--     枠を手で起こす）は `note` とデータ辞書に書く
--
-- `cron_job_name` は NULL（pg_cron ではなく Vercel Cron。`archive_weather` と同じ扱い）。

insert into public.monitored_jobs
  (job_name, expected_every, missing_after, is_active, cron_job_name, note)
values
  ('dispatch_daily', interval '1 day', interval '30 hours', true, null,
   'Vercel Cron 10:15・10:35 JST（DAILY_DISPATCH_CRONS。D-50）。GitHub Actions の日次の枠'
   '（build-features.yml・evaluate-daily.yml）が来ていなければ workflow_dispatch で起こす。'
   '起こしたワークフローは detail.dispatched（同じ日に 2 度は起こさない）。'
   '落ちたら GitHub のトークン（GITHUB_DISPATCH_TOKEN）の期限と権限を確かめ、来ていない枠を手で起こす'
   '（gh workflow run build-features.yml、済んでから evaluate-daily.yml）')
on conflict (job_name) do update
  set expected_every = excluded.expected_every,
      missing_after  = excluded.missing_after,
      is_active      = excluded.is_active,
      cron_job_name  = excluded.cron_job_name,
      note           = excluded.note;

comment on column public.job_runs.status is
  'running（実行中）/ ok / failed / skipped（やることが無かった回：evaluate_daily の予測ログが 2 システムとも無い日（0053）、天気の取り直しの回でその時間の発行が保存済みだったとき（0063）、日次の枠を起こす回で同時に呼ばれた回（0065））。監視（check_jobs_missing）は ok だけを成功と数える。';
