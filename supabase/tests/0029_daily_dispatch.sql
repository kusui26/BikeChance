-- pgTAP: 日次の枠を起こす仕事（10:15・10:35 JST。開発プランの D-50、0065）
--
-- 起こすかどうかの判断は `apps/web/lib/jobs/daily-dispatch.test.ts`。ここで見るのは **DB 側の契約**：
--
--   1. 見張りに載り、日次のジョブと同じ間隔で見られる（1 日・30 時間）
--   2. 失敗の印（`failure_note`）は付けない（取り返せる失敗。0041）。人がすることは説明（`note`）に書く
--   3. 同時に呼ばれた回の `skipped` は、見張り（`check_jobs_missing`）では成功に数えない

begin;
select plan(6);

select is(
  (select expected_every from public.monitored_jobs where job_name = 'dispatch_daily'),
  interval '1 day',
  'dispatch_daily は 1 日に 1 度は来るものとして見る');

select is(
  (select missing_after from public.monitored_jobs where job_name = 'dispatch_daily'),
  interval '30 hours',
  '30 時間来なければ鳴る（日次のジョブと同じ）');

select ok(
  (select is_active and cron_job_name is null from public.monitored_jobs
    where job_name = 'dispatch_daily'),
  '見張る（Vercel Cron なので pg_cron の名前は持たない）');

select is(
  (select failure_note from public.monitored_jobs where job_name = 'dispatch_daily'),
  null,
  '失敗の印（failure_note）は付けない：起こせなかった枠は手で起こせば取り返せる（0041）');

select ok(
  (select note like '%GITHUB_DISPATCH_TOKEN%' and note like '%gh workflow run build-features.yml%'
     from public.monitored_jobs where job_name = 'dispatch_daily'),
  '説明に、確かめるトークンの変数名（値は書かない）と手で起こす方法を書く');

select ok(
  (select col_description('public.job_runs'::regclass,
     (select attnum from pg_attribute
       where attrelid = 'public.job_runs'::regclass and attname = 'status')) like '%0065%'),
  'job_runs.status の説明に、同時に呼ばれた回の skipped を書く');

select * from finish();
rollback;
