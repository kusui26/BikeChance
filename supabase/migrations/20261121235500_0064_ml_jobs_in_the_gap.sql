-- ────────────────────────────────────────────────────────────────
-- 0064 — 天気の取り込みと参照の時刻の説明を、W12 の PR M に合わせる
--        （W12 プランの W12-05、W11 プランの所見 246）
-- ────────────────────────────────────────────────────────────────
-- 推論のほかの ML の仕事を、**推論の合間の毎 5 分の 4 分台**にそろえた（`vercel.json`）：
-- 天気の取り込み（`/ml/weather`）を毎時 28 分から 29 分へ、参照（`/ml/reference`）を 05:03 から
-- 05:04 JST へ。Parquet 化（9 分）は前から 4 分台にある。
--
-- 28 分 23 秒ごろの取り込みと 05:03 の 16 秒ごろの参照は、ドコモの推論（2 分 44〜48 秒に起動し、
-- 最長 36 秒）が分の後半に呼ばれて長引くと、その終わり（3 分 24 秒ごろ）にかかりうる（所見 246）。
--
-- `monitored_jobs.note` は説明の欄で、警報の文には使わない（0061 と同じ）。見張りの間隔
-- （`expected_every`・`missing_after`）は変えない——1 分ずらすだけなので。

update public.monitored_jobs
   set note = 'Vercel Cron 毎時 :29（LOAD_WEATHER_CRON。2026-11-26 まで :28）。weather-raw の gzip JSON を weather_hourly に取り込む。止まると天気の特徴量が古い発行のままになる（v_weather_pending に溜まる）'
 where job_name = 'load_weather';

update public.monitored_jobs
   set note = 'Vercel Cron 05:04 JST（REFERENCE_CRON。2026-11-26 まで 05:03）。前日ぶんの参照スナップショット（reference/date=YYYY-MM-DD/）。止まると翌日の学習・推論が前日の版を読めなくなる'
 where job_name = 'build_reference';
