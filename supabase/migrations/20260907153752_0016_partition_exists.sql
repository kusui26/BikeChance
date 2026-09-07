-- 0016 パーティションの有無を確かめる（W2 プラン §5.4、PR B）
--
-- 再構築スクリプトは過去のスナップショットを流し込む。対象月のパーティションが
-- 無いと行は **DEFAULT パーティションに落ちる**。そこは「保守ジョブが止まっている印」
-- として `monitor_feeds` が通知を上げる場所で、データの置き場所ではない（W1-15）。
--
-- `ensure_snapshot_partitions` は**今月から先しか作らない**（実装で確認）。したがって
-- 過去月の再構築は、パーティションの有無を**取り込む前に**確かめる必要がある。
-- 作成そのものは W2 の範囲外で、必要になった時点で判断する（W2 プラン §8）。
--
-- スクリプトは PostgREST 越しに動くため `pg_catalog` を直接引けない。RPC にする。
create or replace function public.snapshot_partition_exists(p_at timestamptz)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
      from pg_catalog.pg_class c
      join pg_catalog.pg_inherits i on i.inhrelid = c.oid
     where i.inhparent = 'public.status_snapshots'::regclass
       and c.relname = 'status_snapshots_y'
                       || to_char(timezone('UTC', p_at), 'YYYY')
                       || 'm' || to_char(timezone('UTC', p_at), 'MM')
  );
$$;

comment on function public.snapshot_partition_exists(timestamptz) is
  'その時刻を含む月次パーティションが存在するか。再構築が DEFAULT に行を落とさないための事前確認（§5.4）。';

revoke all on function public.snapshot_partition_exists(timestamptz) from public, anon, authenticated;
grant execute on function public.snapshot_partition_exists(timestamptz) to service_role;

notify pgrst, 'reload schema';
