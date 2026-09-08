-- 0022 監視の抜けを塞ぐ（W3 プラン §12 の 86）
--
-- **`daily_quality` が監視されていなかった。** 0020 の `monitored_jobs` への insert から
-- 抜けており（プランの表には書いてあったのに）、成功の有無も pg_cron への登録も
-- 見ていなかった。毎日 07:00 JST に動く前日の品質集計が、静かに止まっても気づけない。
--
-- **問題は 1 行足りないことではなく、足りないことに気づけない仕組みだったこと。**
-- 0021 の `check_cron_jobs` は「知っているジョブがまだ登録されているか」しか見ない。
-- 逆向き——**登録されているのに知らないジョブ**——を見ていなかったので、表への入れ忘れが
-- そのまま盲点になる。両方向を見るようにする。

-- ────────────────────────────────────────────────────────────────
-- 抜けていた 1 行
-- ────────────────────────────────────────────────────────────────
insert into public.monitored_jobs
  (job_name, expected_every, missing_after, cron_job_name, note)
values
  ('daily_quality', interval '1 day', interval '30 hours', 'daily_quality',
   'pg_cron 07:00 JST。前日の収集品質を集計して通知する')
on conflict (job_name) do update set cron_job_name = excluded.cron_job_name;

-- ────────────────────────────────────────────────────────────────
-- 検査 5 を両方向にする
-- ────────────────────────────────────────────────────────────────
-- 追加するのは 2 つ目のループ（**登録されているのに monitored_jobs に無いジョブ**）。
-- これがあれば、次に pg_cron のジョブを足して表に入れ忘れても、5 分後に鳴る。
--
-- Supabase が自前の cron ジョブを足す可能性はあるが、現状は 0 件で、増えたときは
-- 「知らないジョブが増えた」と鳴るのが正しい（無視するなら表に行を足して明示する）。
create or replace function public.check_cron_jobs()
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_row        record;
  v_alerts     integer := 0;
  v_checked    integer := 0;
  v_unmanaged  integer := 0;
begin
  -- ① 知っているジョブが、まだ登録されていて active か
  for v_row in
    select m.job_name, m.cron_job_name,
           (select c.active from cron.job c where c.jobname = m.cron_job_name) as active
      from public.monitored_jobs m
     where m.cron_job_name is not null
  loop
    v_checked := v_checked + 1;
    if v_row.active is distinct from true then
      if public.send_alert('cron_job_missing:' || v_row.cron_job_name, jsonb_build_object(
           'message', 'pg_cron のジョブ ' || v_row.cron_job_name || ' が '
                      || case when v_row.active is null then '登録されていません' else '無効です' end,
           'job_name', v_row.job_name), interval '3 hours') then
        v_alerts := v_alerts + 1;
      end if;
    end if;
  end loop;

  -- ② 登録されているのに、こちらが知らないジョブ（表への入れ忘れを捕まえる）
  for v_row in
    select c.jobname
      from cron.job c
     where not exists (
       select 1 from public.monitored_jobs m where m.cron_job_name = c.jobname
     )
  loop
    v_unmanaged := v_unmanaged + 1;
    if public.send_alert('cron_job_unmonitored:' || v_row.jobname, jsonb_build_object(
         'message', 'pg_cron のジョブ ' || v_row.jobname
                    || ' が monitored_jobs にありません（監視されていません）'),
         interval '24 hours') then
      v_alerts := v_alerts + 1;
    end if;
  end loop;

  return jsonb_build_object('checked', v_checked, 'unmanaged', v_unmanaged, 'alerts', v_alerts);
end;
$$;

comment on function public.check_cron_jobs() is
  'pg_cron のジョブを両方向で見る。①知っているジョブがまだ active か ②登録されているのに知らないジョブが無いか（表への入れ忘れを捕まえる）。';

revoke all on function public.check_cron_jobs() from public, anon, authenticated;

notify pgrst, 'reload schema';
