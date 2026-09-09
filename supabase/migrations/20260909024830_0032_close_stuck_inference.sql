-- 0032 閉じられなかった推論の記録を、保守が閉じる（0031 の続き）
--
-- **検知は入れたが、解消する経路が無かった。**
--
-- 0031 で「`running` のまま `infer_alert_s` を過ぎた行」を `check_inference` が
-- 通知するようにした。**初回で実物を捕まえた**（2026-09-09 11:19 の 1 行。§12 の 111）。
-- ところがその行は**自分では閉じない**。作ったプロセスはもう居ないからである。
-- 抑制は 6 時間なので、**1 日 4 回、永久に鳴り続ける。**
--
-- **通知は「知りたいあいだ」だけ鳴るべきである。** 手で `update` して回るのは仕組みに
-- ならない。日次の保守が閉じる。
--
--   * `status = 'failed'` にする。**`failed` は保持の対象外**（0031）なので消えない
--   * `error = 'never_finished'` を残す。**「失敗した」と「閉じられなかった」は違う。**
--     前者は推論が例外で落ちたことを、後者は `finish_inference` 自体に届かなかったことを
--     指す。区別が付かなくなると、原因を追うときに迷う
--   * `finished_at` は `clock_timestamp()`。**`duration_ms` は入れない**：どれだけ
--     走ったかは分からないので、それらしい数を作らない（推測で埋めない）
--
-- **閾値は 1 時間。** 推論は 5 分周期で `maxDuration` が 120 秒なので、1 時間走り続けて
-- いる実行はあり得ない。`check_inference` の 15 分（通知）より緩くしてあるのは、**気づく
-- のは早く、閉じるのは慎重に**という順にするため。保守は日次なので実際にはもっと古い行
-- しか当たらないが、保守の頻度を上げても規則は変わらない。

create or replace function public.run_maintenance(p_keep_days integer default 60)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_run       bigint;
  v_created   integer;
  v_dropped   integer;
  v_logs      integer;
  v_closed    integer;
  v_inference integer;
  v_details   integer := 0;
  v_result    jsonb;
begin
  if not pg_try_advisory_xact_lock(8423, 3) then
    return jsonb_build_object('status', 'locked');
  end if;
  v_run := public.job_started('maintain_partitions');

  begin
    v_created := public.ensure_snapshot_partitions(2);
    v_dropped := public.drop_expired_snapshot_partitions(p_keep_days);

    delete from public.feed_fetch_log where fetched_at < now() - interval '30 days';
    get diagnostics v_logs = row_count;

    -- **閉じられなかった行を閉じる**（0032）。消すのではなく `failed` にする
    update public.inference_log
       set status = 'failed',
           finished_at = clock_timestamp(),
           error = 'never_finished'
     where status = 'running' and generated_at < now() - interval '1 hour';
    get diagnostics v_closed = row_count;

    -- **`ok` だけを消す。** 失敗・二重抑止・閉じられなかった行は残す（0031 の冒頭）
    delete from public.inference_log
     where status = 'ok' and generated_at < now() - interval '90 days';
    get diagnostics v_inference = row_count;

    -- cron.job_run_details は自動削除されない。毎分ジョブで月 4.3 万行たまる（§4.3 の 3）
    begin
      delete from cron.job_run_details where end_time < now() - interval '7 days';
      get diagnostics v_details = row_count;
    exception when insufficient_privilege then
      v_details := -1;  -- 権限が無い環境では諦める（ローカルなど）
    end;

    v_result := jsonb_build_object(
      'partitions_created', v_created, 'partitions_dropped', v_dropped,
      'fetch_logs_deleted', v_logs, 'inference_runs_closed', v_closed,
      'inference_logs_deleted', v_inference, 'cron_details_deleted', v_details);
    perform public.job_finished(v_run, 'ok', v_result);
    return v_result;
  exception when others then
    perform public.job_finished(v_run, 'failed',
      jsonb_build_object('error', sqlerrm, 'code', sqlstate));
    return jsonb_build_object('status', 'failed', 'error', sqlerrm);
  end;
end;
$$;

comment on function public.run_maintenance(integer) is
  'パーティションの作成・削除、取得ログ 30 日超・推論ログ（ok のみ）90 日超・cron.job_run_details 7 日超の削除、閉じられなかった推論（1 時間超の running）を failed にする。';
