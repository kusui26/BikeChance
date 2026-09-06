-- 0005 パーティションの保守（W1 プラン §6.3、W1-8、W1-15）
--
-- pg_partman を使わない理由（W1-8）：必要なのは「先の月を作る」「古い月を落とす」の 2 つだけ。
-- pg_partman は part_config という設定データを別途マイグレーションで再現する必要があり、
-- `retention_keep_table` の既定が true（実際には削除されない）という罠もある。
-- この規模なら手書きの関数を pgTAP で完全にテストできる。
--
-- 月の境界は **UTC** で切る。Storage のパスと Parquet のパーティションに揃えるため
-- （人が読む daily_quality.quality_date だけが JST。§11.5）。

-- ────────────────────────────────────────────────────────────────
-- 先の月のパーティションを作る
-- ────────────────────────────────────────────────────────────────
create or replace function public.ensure_snapshot_partitions(p_months_ahead integer default 2)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_created    integer := 0;
  v_offset     integer;
  v_month      timestamp;      -- UTC の壁時計としての月初
  v_from       timestamptz;
  v_to         timestamptz;
  v_from_lit   text;
  v_to_lit     text;
  v_name       text;
  v_in_default bigint;
begin
  if p_months_ahead < 0 then
    raise exception 'p_months_ahead は 0 以上である必要があります (%)', p_months_ahead;
  end if;

  for v_offset in 0 .. p_months_ahead loop
    v_month := date_trunc('month', timezone('UTC', now())) + make_interval(months => v_offset);
    v_from  := v_month at time zone 'UTC';
    v_to    := (v_month + interval '1 month') at time zone 'UTC';
    v_name  := 'status_snapshots_y' || to_char(v_month, 'YYYY') || 'm' || to_char(v_month, 'MM');

    continue when to_regclass('public.' || quote_ident(v_name)) is not null;

    -- セッションのタイムゾーンに左右されないよう、オフセット付きの literal を自分で組み立てる
    v_from_lit := to_char(v_month, 'YYYY-MM-DD HH24:MI:SS') || '+00';
    v_to_lit   := to_char(v_month + interval '1 month', 'YYYY-MM-DD HH24:MI:SS') || '+00';

    select count(*) into v_in_default
    from public.status_snapshots_default
    where observed_at >= v_from and observed_at < v_to;

    if v_in_default = 0 then
      execute format(
        'create table public.%I partition of public.status_snapshots for values from (%L) to (%L)',
        v_name, v_from_lit, v_to_lit
      );
    else
      -- DEFAULT に該当範囲の行があると `create ... partition of` は**エラーになる**。
      -- detach → 作成 → 行の移動 → attach を 1 トランザクションで行う（§5 の 24）
      execute 'alter table public.status_snapshots detach partition public.status_snapshots_default';
      execute format(
        'create table public.%I partition of public.status_snapshots for values from (%L) to (%L)',
        v_name, v_from_lit, v_to_lit
      );
      execute format(
        'insert into public.status_snapshots
           select * from public.status_snapshots_default
           where observed_at >= %L::timestamptz and observed_at < %L::timestamptz',
        v_from_lit, v_to_lit
      );
      execute format(
        'delete from public.status_snapshots_default
          where observed_at >= %L::timestamptz and observed_at < %L::timestamptz',
        v_from_lit, v_to_lit
      );
      execute 'alter table public.status_snapshots attach partition public.status_snapshots_default default';
    end if;

    -- パーティションは親から RLS を継承しない。直接参照された場合にも守られるよう個別に有効化する
    -- （権限は親テーブル経由でのアクセスに従うため、個別の grant は不要）
    execute format('alter table public.%I enable row level security', v_name);

    v_created := v_created + 1;
  end loop;

  return v_created;
end;
$$;

comment on function public.ensure_snapshot_partitions(integer) is
  '今月から p_months_ahead か月先までのパーティションを作る。作成数を返す。冪等。DEFAULT に該当行があれば detach → 作成 → 移動 → attach を 1 トランザクションで行う。';

-- ────────────────────────────────────────────────────────────────
-- 保持期間を過ぎた月のパーティションを落とす
-- ────────────────────────────────────────────────────────────────
create or replace function public.drop_expired_snapshot_partitions(p_keep_days integer default 60)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_dropped integer := 0;
  v_rec     record;
  v_parts   text[];
  v_month   timestamp;
  v_to      timestamptz;
  v_current timestamp;
begin
  if p_keep_days < 1 then
    raise exception 'p_keep_days は 1 以上である必要があります (%)', p_keep_days;
  end if;

  v_current := date_trunc('month', timezone('UTC', now()));

  for v_rec in
    select c.relname
      from pg_catalog.pg_class c
      join pg_catalog.pg_inherits i on i.inhrelid = c.oid
     where i.inhparent = 'public.status_snapshots'::regclass
       -- 命名規約に一致するものだけ。DEFAULT パーティションはここで自動的に外れる
       and c.relname ~ '^status_snapshots_y[0-9]{4}m[0-9]{2}$'
     order by c.relname
  loop
    v_parts := regexp_match(v_rec.relname, '^status_snapshots_y([0-9]{4})m([0-9]{2})$');
    v_month := to_date(v_parts[1] || v_parts[2], 'YYYYMM')::timestamp;
    v_to    := (v_month + interval '1 month') at time zone 'UTC';

    -- 現行月は保持日数を短くしても落とさない（収集中のパーティションを消さない）
    continue when v_month >= v_current;
    -- 上限が保持期間の内側にあるものは残す
    continue when v_to > now() - make_interval(days => p_keep_days);

    execute format('drop table public.%I', v_rec.relname);
    v_dropped := v_dropped + 1;
  end loop;

  return v_dropped;
end;
$$;

comment on function public.drop_expired_snapshot_partitions(integer) is
  '上限が now() - p_keep_days より前の月パーティションを drop する。削除数を返す。DEFAULT と現行月は対象外。';

-- 初期パーティション：今月・翌月・翌々月
select public.ensure_snapshot_partitions(2);
