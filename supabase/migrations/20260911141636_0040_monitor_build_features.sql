-- 0040 学習サンプルの日次生成を監視に入れる（W4 プラン §6.8 の PR J、§8.5.2）
--
-- `features/date=YYYY-MM-DD/part.parquet` を作る定期ジョブが**そもそも無かった**ので、
-- 09-10 以降が未作成のまま積み上がっていた（§8.5.2、R20）。同じ PR で
-- **GitHub Actions の日次ワークフロー**を足し（`.github/workflows/build-features.yml`、
-- 06:00 JST）、ジョブ側が `job_started` / `job_finished` を書くようにした。
-- ここは見張りの登録だけを行う。
--
-- **止まったことに気づかないと取り返せない。** 学習サンプルは導出データなので
-- 作り直せる——**ただし天気が在るあいだだけ**である。`weather_hourly` の保持は
-- **30 日**なので、止まったまま 1 か月経つと「天気の入っていない日」しか作れなくなる
-- （生アーカイブから戻す手順は PR L で確かめる）。**日付で決まっている宿題**の 1 つ。
--
-- **`cron_job_name` は NULL。** pg_cron でも Vercel Cron でもなく GitHub Actions なので、
-- 0022 の「pg_cron に登録されているか」の検査の対象ではない（`build_reference` と同じ扱い）。
--
-- **閾値は他の日次ジョブと揃える**：`expected_every = 1 日`、`missing_after = 30 時間`。
-- 30 時間なら 1 回落ちた時点で鳴り、**GitHub の `schedule` の遅れ**（混雑時に数分〜）や
-- 時計のずれでは鳴らない。`check_jobs_missing` は `added_at + missing_after` を過ぎるまで
-- この行を見ないので、**登録した直後に鳴ることはない**（0020）。

insert into public.monitored_jobs
  (job_name, expected_every, missing_after, is_active, cron_job_name, note)
values
  ('build_features', interval '1 day', interval '30 hours', true, null,
   'GitHub Actions 06:00 JST。前日ぶんの学習サンプル（features/date=YYYY-MM-DD/）。'
   '止まると学習に使える日が増えなくなり、weather_hourly の 30 日保持を過ぎると'
   '天気付きでは作り直せなくなる')
on conflict (job_name) do update
  set expected_every = excluded.expected_every,
      missing_after  = excluded.missing_after,
      is_active      = excluded.is_active,
      cron_job_name  = excluded.cron_job_name,
      note           = excluded.note;
