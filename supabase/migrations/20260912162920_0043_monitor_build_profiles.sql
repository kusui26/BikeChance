-- 0043 ポートプロファイルの日次生成を監視に入れる（W5 プラン §6.2 の PR B）
--
-- **`(ポート, 曜日種別, 15 分枠)` の集計を毎日作る。** 気候値（B2）と `prof_*` の
-- 特徴量は**同じ量を別の名前で呼んでいる**ので、表を 1 つにして両方がそこから引く
-- （W5-01）。同じ PR で `.github/workflows/build-features.yml` に 1 段足した
-- （**学習サンプルの後**。2 つは互いに独立で、プロファイルが読まれるのは翌日である。
-- 順序は「取り返しがつかない順」で決めてある）。ここは見張りの登録だけを行う。
--
-- **止まると、転がしの鎖が切れる。** このジョブは
-- `profile(D) = profile(D-1) + daily(D) - daily(D-28)` で作るので、**1 日落ちると
-- 翌日は「前日の版が無い」状態になり、当日だけの薄い表ができる**（`carried: false` が
-- `job_runs.detail` に出る）。作り直しは `status_snapshots` の 60 日と
-- `gbfs-parquet` の毎時ファイル（無期限）から効くので取り返せるが、**古い日から順に
-- 回し直す**必要がある——だから「気づかないまま何日も進む」のを避ける。
--
-- **`cron_job_name` は NULL。** pg_cron でも Vercel Cron でもなく GitHub Actions なので、
-- 0022 の「pg_cron に登録されているか」の検査の対象ではない（`build_features` と同じ扱い）。
--
-- **閾値は他の日次ジョブと揃える**：`expected_every = 1 日`、`missing_after = 30 時間`。
-- GitHub の `schedule` は実測で 1 時間 55 分遅れる（W4 の PR J）ので、**日次の素の間隔
-- 24 時間に対して余裕は 6 時間**である。`check_jobs_missing` は `added_at + missing_after`
-- を過ぎるまでこの行を見ないので、**登録した直後に鳴ることはない**（0020）。

insert into public.monitored_jobs
  (job_name, expected_every, missing_after, is_active, cron_job_name, note)
values
  ('build_profiles', interval '1 day', interval '30 hours', true, null,
   'GitHub Actions（build_features と同じ回）。前日ぶんのポートプロファイル'
   '（profiles/date=YYYY-MM-DD/）。止まると転がしの鎖が切れ、翌日は前日の版が'
   '無い薄い表になる。作り直すときは古い日から順に回す')
on conflict (job_name) do update
  set expected_every = excluded.expected_every,
      missing_after  = excluded.missing_after,
      is_active      = excluded.is_active,
      cron_job_name  = excluded.cron_job_name,
      note           = excluded.note;
