-- ────────────────────────────────────────────────────────────────
-- 0063 — 天気の取り直しの回（毎時 47 分）を、監視の文と状態の説明に足す
--        （W12 プランの W12-04・§4.3、W11 プランの所見 247。W12 の PR T）
-- ────────────────────────────────────────────────────────────────
-- 天気のアーカイブ（毎時 17 分）が**その時間に 1 つも保存できなかったとき**、47 分の
-- `/api/jobs/weather-catch-up` が 7 分割すべてを取り直す。どちらの回も `archive_weather` として
-- 記録し、`detail.run_kind`（`scheduled`・`catch_up`）で分ける——取り直しで保存した発行も
-- `v_weather_files` に載り、次の時間の取り込みが拾う（**ビューは変えない**）。7 分割すべてを
-- 取り直しの回で取るので、1 つの発行に 2 つの「入手した時刻」は混ざらない（W7-01）。
--
--   * **保存済みの時間の取り直しの回は `skipped`**（0053 の「やることが無かった回」）。
--     `hour_epoch_s` の鍵を書かない（`checked_hour_epoch_s` に書く）ので `v_weather_files` に
--     載らない。見張り（`check_jobs_missing`）は `ok` だけを数えるので、`skipped` は定時の回の
--     代わりにならない（取り直しの回が実際に取った時間は `ok` で数える——そのときも予報は
--     失われていない）
--   * **通知の文を直す**：「その時刻の予報は二度と取れません」は、定時の回の失敗の通知
--     （17 分の回の数分後に鳴る）の時点では、まだ正しくない。取り直しの結果が出るのは 47 分の後
--
-- 0041 の検査（`0011_monitor_jobs.sql`）が見る 2 つの言い回し（「二度と取れません」と
-- 「Open-Meteo は過去の発行を返さない」）は残す——取り直しも落ちた時間には、そのまま正しい。

update public.monitored_jobs
   set note = 'Vercel Cron 毎時 :17（ARCHIVE_WEATHER_CRON）。その時間に 1 つも保存できなければ :47 に取り直す（WEATHER_CATCH_UP_CRON。どちらも archive_weather で、detail.run_kind で分ける。0063）。取り直しも落ちた時刻の予報は永久に失われる',
       failure_note = 'その時間に 1 つも保存できていなければ、47 分の回が 1 度だけ取り直します（結果は job_runs の detail.run_kind = catch_up の行）。取り直しも落ちた時刻の予報は二度と取れません（Open-Meteo は過去の発行を返さない）'
 where job_name = 'archive_weather';

comment on column public.job_runs.status is
  'running（実行中）/ ok / failed / skipped（やることが無かった回：evaluate_daily の予測ログが 2 システムとも無い日（0053）と、天気の取り直しの回でその時間の発行が保存済みだったとき（0063））。監視（check_jobs_missing）は ok だけを成功と数える。';
