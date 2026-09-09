-- 0030 推論の記録に `detail` を足す（W3 プラン §14.2 の 3）
--
-- **成果物に無いポートが静かに増えるのを、見えるようにする。**
--
-- §12 の 110 で「成果物に無いポートは B1 に落とす」と直した。落ちること自体は正しいが、
-- **落ちていることが見えない。** 新規ポートは 1 日約 100 件現れ、W6 以降は週次再学習に
-- なるので、**再学習の間隔ぶんだけ溜まる**。§7.8 に手で突き合わせる手順は書いたものの、
-- 自動で気づく口が無かった。
--
-- **`job_runs` と同じ形にする。** あちらは `detail jsonb` に要約を入れ、監視は
-- `detail->>'...'` で読む。`inference_log` にも同じ口を作れば、これから増える数
-- （`cpu_ms`、B2 に落ちた割合など）を**列を足さずに**載せられる。
--
-- **列に在るものは入れない。** `status` / `n_rows` / `duration_ms` / `error` /
-- `model_version` / `base_observed_at` は列で持っている。同じ値を jsonb にも並べると
-- 1 日 576 行 × 5 項目ぶん無駄に膨らむ。`detail` に入れるのは**列になっていない 3 つ**
-- （`stations` / `skipped` / `unknown_ports`）だけにする。
--
-- **`finish_inference` は落として作り直す。** `create or replace` が置き換えになるのは
-- **引数の並びが同じとき**だけで、末尾に既定値付きの引数を足すと**多重定義**になり、
-- PostgREST がどちらを呼ぶか決められなくなる。マイグレーションは 1 トランザクションで
-- 走るので、関数が消えている隙間はできない。
--
-- `p_detail` に既定値を付けてあるので、**古い呼び出し（5 引数）もそのまま通る**。
-- マイグレーションとコードのどちらを先に出しても壊れない。

alter table public.inference_log add column detail jsonb;

comment on column public.inference_log.detail is
  '列になっていない要約だけを入れる（stations / skipped / unknown_ports）。unknown_ports は成果物に無かったポートの数で、増え続けたら再学習の間隔が長すぎる（W3 プラン §12 の 110）。';

drop function public.finish_inference(bigint, text, integer, integer, text);

create or replace function public.finish_inference(
  p_id          bigint,
  p_status      text,
  p_n_rows      integer,
  p_duration_ms integer,
  p_error       text default null,
  p_detail      jsonb default null
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  update public.inference_log
     set status = p_status,
         finished_at = clock_timestamp(),
         n_rows = p_n_rows,
         duration_ms = p_duration_ms,
         error = left(p_error, 500),
         detail = p_detail
   where id = p_id;
end;
$$;

comment on function public.finish_inference(bigint, text, integer, integer, text, jsonb) is
  '推論の結果を記録する。finished_at は clock_timestamp()（now() はトランザクション開始時刻で動かない）。detail は列になっていない要約だけ。';

revoke all on function public.finish_inference(bigint, text, integer, integer, text, jsonb)
  from public, anon, authenticated;

-- **本番は「新規オブジェクトの自動公開」が OFF** で、関数の EXECUTE が service_role に
-- 既定で付かない（W3 プラン §12 の 90）。PostgREST から呼ぶので明示的に付ける。
grant execute on function public.finish_inference(bigint, text, integer, integer, text, jsonb)
  to service_role;

notify pgrst, 'reload schema';
