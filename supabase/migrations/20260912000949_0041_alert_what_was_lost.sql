-- 0041 通知に「何が失われたか」を書く（W4 プラン §6.8 の PR K、§8.5.6）
--
-- **検出は効いていた。** 2026-09-10 17:17 の `archive_weather` の失敗は 2 分後に通知され、
-- Webhook も 204 で届いた。足りないのは**中身**である。
--
--   届いた通知：archive_weather が直近 3 時間に 1 回失敗しました  last_error: null
--   届くべき　：… ＋「その時刻の予報は二度と取れない」＋「fetch/TypeError」
--
-- 欠けていたものが 2 つある。
--
--   ①**理由**：`archive_weather` は分割ごとの失敗を `detail.failures`（配列）に入れる。
--     `check_jobs_failed` は `detail->>'error'` しか見ていなかったので NULL になった。
--     **`error` が出るのはジョブ全体が落ちたときだけ**で、**部分的に落ちたとき**——
--     つまり実際に起きたほう——は別の鍵に入る。
--   ②**失われたものの性質**：「落ちた時刻の予報は永久に失われる」は `monitored_jobs.note`
--     に書いてあるのに、通知には出ていなかった。
--
-- **②を「note の文面を照合して」判定しない。** W4 プランは「note に『回復不能』の印が
-- 付いたジョブ」と書いていたが、**`archive_weather` の note にその語は無い**
-- （「永久に失われる」と書いてある）。**文面の一致に頼る設計は、書いた本人の記憶にしか
-- 合わない。** 表示するための欄を別に持つ。

-- ────────────────────────────────────────────────────────────────
-- ② 失敗したときに足す一文
-- ────────────────────────────────────────────────────────────────
alter table public.monitored_jobs
  add column if not exists failure_note text;

comment on column public.monitored_jobs.failure_note is
  'このジョブが失敗したときに通知の本文へ足す一文（PR K）。**再実行では取り返せない失敗にだけ書く。**'
  ' 空なら何も足さない。note と違い、これは「人に見せる文」であって照合の対象ではない。';

-- **いま印を付けるのは 1 本だけ。** ほかは再実行で取り返せる：`compact_parquet` は
-- `status_snapshots`（60 日）から畳み直せるし、`build_features` は 30 日以内なら天気付きで
-- 作り直せる。pg_cron の保守系は次の回が同じ仕事をする。
-- **取り返せないのは「外から取るしかないもの」だけ**である。
update public.monitored_jobs
   set failure_note = 'その時刻の予報は二度と取れません（Open-Meteo は過去の発行を返さない）'
 where job_name = 'archive_weather';

-- ────────────────────────────────────────────────────────────────
-- ① 失敗の理由を取り出す
-- ────────────────────────────────────────────────────────────────
-- **実際に書かれている形を数えて並べた**（推測ではない）。
--
--   error      ジョブ全体が落ちた。pg_cron の SQL 関数（sqlerrm）・build_reference・
--              build_features・load_weather・sync_stations・compact・archive_weather
--   failures   分割のどれかが落ちた。archive_weather。**2026-09-10 に拾えなかった形**
--   systems[]  系統のどれかが落ちた。compact_parquet
--   reason     そもそも起動できなかった。trigger_infer・trigger_backup_collect
--
-- **どれにも当てはまらなければ、生の断片を返す。** 黙って NULL にすると、また
-- 「last_error: null」だけが届く。**知らない形が来たことが、読む人に見えるほうがよい。**
create or replace function public.failure_reason(p_detail jsonb)
returns text
language sql
immutable
set search_path = ''
as $$
  select coalesce(
    nullif(p_detail->>'error', ''),
    nullif(p_detail->'failures'->>0, ''),
    (select nullif(one->>'error', '')
       from jsonb_array_elements(
              case when jsonb_typeof(p_detail->'systems') = 'array'
                   then p_detail->'systems' else '[]'::jsonb end) as one
      where nullif(one->>'error', '') is not null
      limit 1),
    nullif(p_detail->>'reason', ''),
    nullif(nullif(left(p_detail::text, 200), 'null'), '{}')
  );
$$;

comment on function public.failure_reason(jsonb) is
  'job_runs.detail から失敗の理由を 1 行で取り出す（PR K）。知らない形は生の断片を返す。';

-- ────────────────────────────────────────────────────────────────
-- 検査 2：失敗したジョブ（0020 の差し替え）
-- ────────────────────────────────────────────────────────────────
-- **`monitored_jobs` に無いジョブも拾う**ので、結合は左外部結合にする（0020 と同じ方針）。
create or replace function public.check_jobs_failed()
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_row    record;
  v_alerts integer := 0;
  v_jobs   integer := 0;
begin
  for v_row in
    select f.job_name, f.n, f.last_at, f.last_error, m.failure_note
      from (
        select r.job_name, count(*) as n, max(r.started_at) as last_at,
               (array_agg(x.reason order by r.started_at desc)
                  filter (where x.reason is not null))[1] as last_error
          from public.job_runs r
          cross join lateral (select public.failure_reason(r.detail) as reason) x
         where r.status = 'failed' and r.started_at > now() - interval '3 hours'
         group by r.job_name
      ) f
      left join public.monitored_jobs m on m.job_name = f.job_name
  loop
    v_jobs := v_jobs + 1;
    if public.send_alert('job_failed:' || v_row.job_name, jsonb_build_object(
         'message', v_row.job_name || ' が直近 3 時間に ' || v_row.n || ' 回失敗しました'
                    || coalesce('。' || v_row.failure_note, ''),
         'count', v_row.n,
         'last_at', v_row.last_at,
         'last_error', v_row.last_error), interval '1 hour') then
      v_alerts := v_alerts + 1;
    end if;
  end loop;
  return jsonb_build_object('failed_jobs', v_jobs, 'alerts', v_alerts);
end;
$$;

comment on function public.check_jobs_failed() is
  '直近 3 時間に status=failed があるジョブを通知する。monitored_jobs に限らず全ジョブを見る。'
  ' 理由は failure_reason() で取り出し、failure_note のあるジョブは本文にその一文を足す（PR K）。';

-- 権限：0009・0020 と同じ作法で、匿名から呼べないことを明示的に閉じる。
-- pg_cron は postgres として実行するので grant は要らない。
revoke all on function public.failure_reason(jsonb) from public, anon, authenticated;
revoke all on function public.check_jobs_failed() from public, anon, authenticated;

notify pgrst, 'reload schema';
