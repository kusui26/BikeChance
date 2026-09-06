-- pgTAP: パーティションの保守関数（W1 プラン §6.3、W1-8、W1-15、§5 の 24）
--
-- ここで確かめたい山場は 1 つ：**DEFAULT に該当行がある状態で新パーティションを作れるか**。
-- Postgres は、新しい範囲に該当する行が DEFAULT にあると `create table ... partition of` を
-- エラーにする。detach → 作成 → 移動 → attach の分岐が正しく動かないと、月替わりに
-- 保守ジョブが失敗し、やがて収集が止まる。

begin;
select plan(24);

-- このファイルはトランザクション内で完結し rollback するので、ここでの削除は外に影響しない。
-- ベンチマークや手動確認でデータが残っていても同じ結果になるよう、作業テーブルを空にしてから始める
-- （テストが実行環境の状態に依存しないようにする）。
delete from public.station_status_latest;
delete from public.status_snapshots;
delete from public.station_attributes;
delete from public.stations;
delete from public.feed_fetch_log;
delete from public.job_runs;
update public.feed_state
   set last_fetch_at = null, last_success_at = null, last_observed_at = null,
       last_etag = null, consecutive_errors = 0;

-- テスト用のスナップショットを 1 行入れる小さなヘルパ
create function pg_temp.put_snapshot(p_observed_at timestamptz) returns void
language sql as $$
  insert into public.status_snapshots
    (system_id, observed_at, fetched_at, n_stations, bikes, docks, flags, reported_age_s, raw_path)
  values ('hellocycling', p_observed_at, p_observed_at, 1, '{1}', '{1}', '{7}', '{0}', 'test');
$$;

-- 当月を UTC で求める（関数と同じ基準）
create function pg_temp.month_start(p_offset integer) returns timestamptz
language sql as $$
  select (date_trunc('month', timezone('UTC', now())) + make_interval(months => p_offset)) at time zone 'UTC';
$$;

create function pg_temp.partition_name(p_offset integer) returns name
language sql as $$
  select ('status_snapshots_y'
    || to_char(date_trunc('month', timezone('UTC', now())) + make_interval(months => p_offset), 'YYYY')
    || 'm'
    || to_char(date_trunc('month', timezone('UTC', now())) + make_interval(months => p_offset), 'MM'))::name;
$$;

-- ────────────────────────────────────────────────────────────────
-- 初期状態：マイグレーションが今月・翌月・翌々月を作っている
-- ────────────────────────────────────────────────────────────────
select has_table('public'::name, pg_temp.partition_name(0), '今月のパーティションがある');
select has_table('public'::name, pg_temp.partition_name(1), '翌月のパーティションがある');
select has_table('public'::name, pg_temp.partition_name(2), '翌々月のパーティションがある');
select hasnt_table('public'::name, pg_temp.partition_name(3), '3 か月先はまだ無い');

-- ────────────────────────────────────────────────────────────────
-- 冪等性
-- ────────────────────────────────────────────────────────────────
select is(public.ensure_snapshot_partitions(2), 0, '2 回目の呼び出しは 0 を返す（冪等）');
select is(public.ensure_snapshot_partitions(3), 1, '1 か月延ばすと 1 つだけ作る');
select has_table('public'::name, pg_temp.partition_name(3), '3 か月先が作られた');
select throws_ok(
  $$select public.ensure_snapshot_partitions(-1)$$,
  'p_months_ahead は 0 以上である必要があります (-1)',
  '負の月数は弾く'
);

-- ────────────────────────────────────────────────────────────────
-- 行が正しいパーティションに入る
-- ────────────────────────────────────────────────────────────────
select lives_ok(
  $$select pg_temp.put_snapshot(pg_temp.month_start(2) + interval '5 days')$$,
  '翌々月の範囲の行を入れられる'
);
select is(
  (select count(*)::int from public.status_snapshots_default), 0,
  '範囲内の行は DEFAULT に入らない'
);

-- 範囲外（1 年先）は DEFAULT に落ちる
select lives_ok(
  $$select pg_temp.put_snapshot(pg_temp.month_start(12) + interval '3 days')$$,
  '範囲外の行も保存できる（DEFAULT が受け止める）'
);
select is(
  (select count(*)::int from public.status_snapshots_default), 1,
  '範囲外の行は DEFAULT に入る'
);
select is(
  (select count(*)::int from public.status_snapshots where observed_at > now() + interval '300 days'), 1,
  '親テーブル経由でも見える'
);

-- ────────────────────────────────────────────────────────────────
-- 山場：DEFAULT に行がある状態で、その範囲のパーティションを作る
-- ────────────────────────────────────────────────────────────────
select is(public.ensure_snapshot_partitions(12), 9, 'DEFAULT に行がある月を含めて 9 か月分を作る');
select has_table('public'::name, pg_temp.partition_name(12), '12 か月先のパーティションが作られた');
select is(
  (select count(*)::int from public.status_snapshots_default), 0,
  'DEFAULT の行が新パーティションへ移り、DEFAULT が空になった'
);
select is(
  (select count(*)::int from public.status_snapshots where observed_at > now() + interval '300 days'), 1,
  '移動しても行は失われていない'
);
select is(
  (select count(*)::int from pg_class c join pg_inherits i on i.inhrelid = c.oid
    where i.inhparent = 'public.status_snapshots'::regclass
      and c.relname = 'status_snapshots_default'),
  1, 'DEFAULT が再び attach されている'
);

-- ────────────────────────────────────────────────────────────────
-- 新パーティションにも RLS が掛かる
-- ────────────────────────────────────────────────────────────────
select is(
  (select bool_and(c.relrowsecurity) from pg_class c join pg_inherits i on i.inhrelid = c.oid
    where i.inhparent = 'public.status_snapshots'::regclass),
  true, '全パーティションで RLS が有効（親から継承されないので個別に掛けている）'
);

-- ────────────────────────────────────────────────────────────────
-- 古いパーティションの削除
-- ────────────────────────────────────────────────────────────────
create table public.status_snapshots_y2020m01 partition of public.status_snapshots
  for values from ('2020-01-01 00:00:00+00') to ('2020-02-01 00:00:00+00');

select is(public.drop_expired_snapshot_partitions(60), 1, '保持期間を過ぎた月を 1 つ落とす');
select hasnt_table('public'::name, 'status_snapshots_y2020m01'::name, '2020 年 1 月は落ちた');
select has_table(
  'public'::name, pg_temp.partition_name(0),
  '保持日数を 1 日にしても現行月は落とさない'
);
select is(
  (select count(*)::int from pg_class c join pg_inherits i on i.inhrelid = c.oid
    where i.inhparent = 'public.status_snapshots'::regclass
      and c.relname = 'status_snapshots_default'),
  1, 'DEFAULT は削除の対象にならない'
);
select throws_ok(
  $$select public.drop_expired_snapshot_partitions(0)$$,
  'p_keep_days は 1 以上である必要があります (0)',
  '保持日数 0 は弾く（誤って全部消さない）'
);

select * from finish();
rollback;
