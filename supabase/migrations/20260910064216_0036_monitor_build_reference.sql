-- 0036 参照スナップショットの日次ジョブを監視に入れる（W4 プラン §12 の 116）
--
-- `/ml/reference`（Vercel Cron、20:00 UTC ＝ 05:00 JST）は **`job_runs` に何も書いて
-- いなかった**ので、`monitored_jobs` にも載らず、**止まっても誰も気づかない**状態だった。
--
-- 学習も推論も「基準時刻の**前日**の版」を読む設計（W3 プラン §14.3）なので、止まると
-- **翌日に静かに壊れる**。W3 の 109・111 と同じ形（作ったが検知の経路が無い）。
--
-- ジョブ側は同じ PR で `job_started` / `job_finished` を書くようにした
-- （`apps/ml/bikechance_ml/jobs/build_reference.py`）。ここは見張りの登録だけを行う。
--
-- **`cron_job_name` は NULL。** pg_cron ではなく Vercel Cron なので、0022 の
-- 「pg_cron に登録されているか」の検査の対象ではない（`compact_parquet` や
-- `archive_weather` と同じ扱い）。
--
-- **閾値は他の日次ジョブと揃える**：`expected_every = 1 日`、`missing_after = 30 時間`。
-- 30 時間なら 1 回落ちた時点で鳴り、時計のずれや実行時刻の揺らぎでは鳴らない。

insert into public.monitored_jobs
  (job_name, expected_every, missing_after, is_active, cron_job_name, note)
values
  ('build_reference', interval '1 day', interval '30 hours', true, null,
   'Vercel Cron 05:00 JST。前日ぶんの参照スナップショット（reference/date=YYYY-MM-DD/）。'
   '止まると翌日の学習・推論が前日の版を読めなくなる')
on conflict (job_name) do update
  set expected_every = excluded.expected_every,
      missing_after  = excluded.missing_after,
      is_active      = excluded.is_active,
      cron_job_name  = excluded.cron_job_name,
      note           = excluded.note;
