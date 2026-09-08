-- 0028 `job_finished` の `finished_at` を `clock_timestamp()` にする（W3 プラン §12 の 94）
--
-- **pg_cron から呼ぶジョブの所要が、必ず 0 秒になっていた。**
--
--   本番の job_runs（2026-09-08 22:00 JST 時点、直近 36 時間）
--     pg_cron の中     7 系統  2,697 行   すべて 0 ms        ← 壊れている
--     PostgREST 越し   6 系統    133 行   269〜5,079 ms      ← 元から正しい
--
-- 原因は `now()` で、これは**トランザクション開始時刻**を返す。同じトランザクションの
-- 中では何秒はたらいても動かない（本番サーバーで確認した。時刻はサーバーの UTC）。
--
--   now   1 回目 13:02:32.861     clock 1 回目 13:02:32.877
--      ↓ pg_sleep(0.5)
--   now   2 回目 13:02:32.861     clock 2 回目 13:02:33.434
--
-- pg_cron は**関数の呼び出し全体が 1 トランザクション**なので、`job_started` が入れる
-- `started_at`（`default now()`）と `job_finished` が入れる `finished_at`（`now()`）が
-- 同じ値になる。PostgREST 越しのジョブは RPC 1 回が 1 トランザクションで、`job_started`
-- と `job_finished` が別々のトランザクションで走るため、たまたま正しく測れていた。
--
-- **`started_at` は `now()` のままでよい。** pg_cron ではトランザクション開始＝ジョブ開始、
-- PostgREST では `job_started` 自身が 1 トランザクション。どちらの経路でも「開始時刻」と
-- して正しい。直すのは `finished_at` だけである。この非対称は段 8 の `finish_inference`
-- （0026）で既に採っており、13 系統が使う共有関数のほうが取り残されていた。
--
-- **天気の特徴量は動かない。** `v_weather_files.available_at`（0023）は
-- `job_runs.finished_at` そのもので、学習の入力である（W3-06）。ただし書き手の
-- `archive_weather` は Vercel Cron ＝ PostgREST 越しなので、`finished_at` は元から
-- 「`job_finished` を呼んだ瞬間」だった。`clock_timestamp()` にしても動くのは 1 ミリ秒
-- 未満で、**train/serve skew も特徴量の作り直しも生じない**。
--
-- **これでも測れないもの**
--   * `trigger_backup_collect`（0021）は `net.http_post` でキューに積むだけなので、
--     測れるのは投函までである。バックアップの実所要は Edge Function 自身が書く
--     `backup_collect:<system>` の行が持つ（実測 269 / 592 ms）。
--   * **過去の行は 0 秒のまま。** 埋め戻す材料が無いので補間しない（CLAUDE.md §6）。
--     適用の前後を並べて比べてはいけない。

create or replace function public.job_finished(p_id bigint, p_status text, p_detail jsonb)
returns void
language sql
security definer
set search_path = ''
as $$
  update public.job_runs
     set finished_at = clock_timestamp(), status = p_status, detail = p_detail
   where id = p_id;
$$;

comment on function public.job_finished(bigint, text, jsonb) is
  'ジョブの終了を job_runs に記録する。finished_at は clock_timestamp()（now() はトランザクション開始時刻で動かず、pg_cron のジョブの所要が 0 秒になる）。';

-- `create or replace` は既存の ACL を保つが、権限は**毎回明示的に書く**のが本リポジトリの
-- 約束である（W3 プラン §12 の 90。本番は関数の既定権限に service_role が入らない）。
-- 見張りは `supabase/tests/0003_grants.sql` の分類テスト。
revoke all on function public.job_finished(bigint, text, jsonb) from public, anon, authenticated;
grant execute on function public.job_finished(bigint, text, jsonb) to service_role;

notify pgrst, 'reload schema';
