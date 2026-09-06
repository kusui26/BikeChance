-- 0007 取り込み RPC（W1 プラン §6.5、契約は §11.3）
--
-- 収集器は 3 つの RPC を順に呼ぶ。**どの列を誰が書くか**を固定することで、
-- 失敗時に取りこぼしが起きない構造にする（W1-14）。
--
--   begin_fetch     … last_fetch_at                      取得を始める前の claim
--   ingest_snapshot … last_etag / last_observed_at        取り込みに成功したときだけ
--   finish_fetch    … last_success_at / consecutive_errors 結果の記録
--
-- `last_etag` を関数側で書かないのが要。Storage への保存後に取り込みが失敗しても、
-- 次回は「最後に取り込みに成功した ETag」で条件付き要求を出すため、ODPT が 200 で
-- 同じ内容を返し、取り込みが自然に再試行される（§5 の 2）。
--
-- 共通の作法：
--   * `security definer` ＋ `set search_path = ''`（すべて schema 修飾する）
--   * `set statement_timeout = '30s'`。PostgREST は関数の設定をトランザクションに
--     持ち上げるので、ロールの 8 秒に縛られない（W1-2）
--   * 同時実行の抑止は `pg_try_advisory_xact_lock(class_id, systems.lock_key)`。
--     Supavisor のトランザクションモードではセッション単位のロックが効かない（§11.4）

-- ────────────────────────────────────────────────────────────────
-- 補助：jsonb から数値・真偽値を安全に取り出す
-- ────────────────────────────────────────────────────────────────
-- finish_fetch はエラー経路からも呼ばれる。そこで型変換に失敗すると、記録を残せないまま
-- 二次的な例外になる。「取れなければ null」に倒して、記録だけは必ず残す。
create or replace function public.jsonb_number(p_value jsonb)
returns numeric
language sql
immutable
parallel safe
set search_path = ''
as $$
  select case when jsonb_typeof(p_value) = 'number' then (p_value #>> '{}')::numeric end;
$$;

comment on function public.jsonb_number(jsonb) is
  'jsonb が数値なら numeric、それ以外（null・文字列・欠落）は null。記録用の緩い取り出し。';

create or replace function public.jsonb_boolean(p_value jsonb)
returns boolean
language sql
immutable
parallel safe
set search_path = ''
as $$
  select case when jsonb_typeof(p_value) = 'boolean' then (p_value #>> '{}')::boolean end;
$$;

comment on function public.jsonb_boolean(jsonb) is
  'jsonb が真偽値なら boolean、それ以外は null。';

-- ────────────────────────────────────────────────────────────────
-- 1. begin_fetch — 取得を始める前に原子的に claim する
-- ────────────────────────────────────────────────────────────────
-- 「読んでから判断」ではなく **1 文の UPDATE** で claim する。読んでから書く方式だと、
-- 同時に走る 2 つの実行が両方とも「まだ誰も取っていない」と判断して通過する（§5 の 6）。
-- 行そのものの更新が排他になるため、ここではアドバイザリロックを使わない（§11.4）。
--
-- 戻り値で ETag と last_observed_at も返し、条件付き要求のための往復を 1 回にまとめる。
create or replace function public.begin_fetch(
  p_system_id      text,
  p_min_interval_s integer default 30
)
returns jsonb
language plpgsql
security definer
set search_path = ''
set statement_timeout = '30s'
as $$
declare
  v_state public.feed_state%rowtype;
begin
  update public.feed_state
     set last_fetch_at = now()
   where system_id = p_system_id
     and (last_fetch_at is null or last_fetch_at < now() - make_interval(secs => p_min_interval_s))
  returning * into v_state;

  if found then
    return jsonb_build_object(
      'claimed', true,
      'last_etag', v_state.last_etag,
      'last_observed_at', v_state.last_observed_at
    );
  end if;

  -- 更新できなかった理由は 2 つ。区別できないと運用調査で困る
  if not exists (select 1 from public.feed_state where system_id = p_system_id) then
    raise exception '未知のシステムです: %', p_system_id using errcode = '22023';
  end if;
  return jsonb_build_object('claimed', false);
end;
$$;

comment on function public.begin_fetch(text, integer) is
  '取得を始める前の claim。1 文の UPDATE で原子的に行い、ETag と last_observed_at を返す（§11.3）。';

-- ────────────────────────────────────────────────────────────────
-- 2. ingest_snapshot — スナップショットを 1 トランザクションで取り込む
-- ────────────────────────────────────────────────────────────────
create or replace function public.ingest_snapshot(
  p_system_id          text,
  p_observed_at        timestamptz,
  p_fetched_at         timestamptz,
  p_etag               text,
  p_station_ids        text[],
  p_bikes              smallint[],
  p_docks              smallint[],
  p_flags              smallint[],
  p_reported_age_s     smallint[],
  p_raw_path           text,
  p_min_presence_ratio numeric default 0.5
)
returns jsonb
language plpgsql
security definer
set search_path = ''
set statement_timeout = '30s'
as $$
declare
  v_lock_key         smallint;
  v_input_len        integer;
  v_distinct_len     integer;
  v_registered_before integer;
  v_max_idx          integer;
  v_new              integer := 0;
  v_present          integer;
  v_array_length     integer;
  v_anomalous        boolean;
  v_changed          integer := 0;
  v_inserted         integer;
  v_is_newest        boolean;
  v_bikes            smallint[];
  v_docks            smallint[];
  v_flags            smallint[];
  v_ages             smallint[];
begin
  -- ── 1. システムの確認と同時実行の抑止 ──
  select lock_key into v_lock_key from public.systems where system_id = p_system_id;
  if v_lock_key is null then
    raise exception '未知のシステムです: %', p_system_id using errcode = '22023';
  end if;
  if not pg_try_advisory_xact_lock(8421, v_lock_key) then
    return jsonb_build_object('status', 'locked');
  end if;

  -- ── 2. 引数の検査 ──
  -- 配列長がずれると、あるポートの台数が別のポートの行に入る。ここで必ず止める
  v_input_len := coalesce(array_length(p_station_ids, 1), 0);
  if v_input_len <> coalesce(array_length(p_bikes, 1), 0)
     or v_input_len <> coalesce(array_length(p_docks, 1), 0)
     or v_input_len <> coalesce(array_length(p_flags, 1), 0)
     or v_input_len <> coalesce(array_length(p_reported_age_s, 1), 0) then
    raise exception '配列の長さが揃っていません: ids=% bikes=% docks=% flags=% ages=%',
      v_input_len, coalesce(array_length(p_bikes, 1), 0), coalesce(array_length(p_docks, 1), 0),
      coalesce(array_length(p_flags, 1), 0), coalesce(array_length(p_reported_age_s, 1), 0)
      using errcode = '22023';
  end if;

  -- 入力に重複があると左結合で行が増え、配列長と idx の対応が崩れる。
  -- gbfs-core が重複排除しているので通常は起きないが、契約違反は黙って通さない
  select count(distinct x) into v_distinct_len from unnest(p_station_ids) x;
  if v_distinct_len <> v_input_len then
    raise exception '入力に重複した station_id があります（% 件中 % 件が一意）', v_input_len, v_distinct_len
      using errcode = '22023';
  end if;

  -- ── 3. 二重処理の早期打ち切り ──
  -- Cron の二重配信で最も多く通る経路。配列を組み立てる前に返す
  if exists (
    select 1 from public.status_snapshots
     where system_id = p_system_id and observed_at = p_observed_at
  ) then
    return jsonb_build_object('status', 'duplicate');
  end if;

  -- ── 4. 異常ガードの判定（登録の前に測る） ──
  -- 初回取り込みでは登録済みが 0 なので必ず偽になり、正しく通る
  select count(*) into v_registered_before from public.stations where system_id = p_system_id;
  select count(*) into v_present
    from unnest(p_station_ids) x
   where exists (
     select 1 from public.stations s where s.system_id = p_system_id and s.station_id = x
   );
  v_anomalous := v_present < v_registered_before * p_min_presence_ratio;
  -- 出現数は登録後の値（新規ポートも「現れた」ので数える）
  v_present := v_input_len;

  -- ── 5. 未登録ポートの登録。stations への書き込みはここだけ（W1-13） ──
  -- idx は 0 起点で密に採番する。配列の位置 idx+1 との対応がこれで保たれる
  select coalesce(max(idx), -1) into v_max_idx from public.stations where system_id = p_system_id;

  with candidate as (
    select t.station_id, t.ordinality
      from unnest(p_station_ids) with ordinality as t(station_id, ordinality)
     where not exists (
       select 1 from public.stations s
        where s.system_id = p_system_id and s.station_id = t.station_id
     )
  )
  insert into public.stations (system_id, station_id, idx)
  select p_system_id, station_id, v_max_idx + row_number() over (order by ordinality)
    from candidate;
  get diagnostics v_new = row_count;

  -- ── 6. 密な配列の組み立て。現れなかったポートは -1（§11.1） ──
  with input as (
    select *
      from unnest(p_station_ids, p_bikes, p_docks, p_flags, p_reported_age_s)
        as t(station_id, bikes, docks, flags, reported_age_s)
  )
  select
    array_agg(coalesce(i.bikes, -1) order by s.idx),
    array_agg(coalesce(i.docks, -1) order by s.idx),
    array_agg(coalesce(i.flags, -1) order by s.idx),
    array_agg(coalesce(i.reported_age_s, -1) order by s.idx),
    count(*)
  into v_bikes, v_docks, v_flags, v_ages, v_array_length
    from public.stations s
    left join input i on i.station_id = s.station_id
   where s.system_id = p_system_id;

  -- ── 7. スナップショットの保存 ──
  insert into public.status_snapshots
    (system_id, observed_at, fetched_at, n_stations, is_anomalous,
     bikes, docks, flags, reported_age_s, raw_path)
  values
    (p_system_id, p_observed_at, p_fetched_at, v_present, v_anomalous,
     coalesce(v_bikes, '{}'), coalesce(v_docks, '{}'), coalesce(v_flags, '{}'),
     coalesce(v_ages, '{}'), p_raw_path)
  on conflict (system_id, observed_at) do nothing;
  get diagnostics v_inserted = row_count;
  if v_inserted = 0 then
    return jsonb_build_object('status', 'duplicate');
  end if;

  -- ── 8. 最新状態の更新 ──
  --
  -- **後退したスナップショットでは何も書かない**（§5 の 26）。
  -- 行ごとの `last_changed_at` と比べるだけでは足りない。`last_changed_at` は
  -- 「最後に**変化**した時刻」なので、しばらく値が動いていないポートは、
  -- 古いスナップショットでも「前回の変化より新しい」と判定されて上書きされてしまう。
  -- 後退かどうかはフィード単位の性質なので、`feed_state.last_observed_at` と比べる。
  select coalesce(last_observed_at, '-infinity'::timestamptz) < p_observed_at
    into v_is_newest
    from public.feed_state where system_id = p_system_id;

  if coalesce(v_is_newest, true) then
  -- 変化した行だけ書く。異常なフィードでは「不在」への反転をしない（W1-18）
  with input as (
    select *
      from unnest(p_station_ids, p_bikes, p_docks, p_flags, p_reported_age_s)
        as t(station_id, bikes, docks, flags, reported_age_s)
  )
  insert into public.station_status_latest as l
    (system_id, station_id, bikes, docks, flags, is_present, last_changed_at)
  select p_system_id, s.station_id,
         coalesce(i.bikes, l0.bikes, -1),
         coalesce(i.docks, l0.docks, -1),
         coalesce(i.flags, l0.flags, -1),
         i.station_id is not null,
         p_observed_at
    from public.stations s
    left join input i on i.station_id = s.station_id
    left join public.station_status_latest l0
      on l0.system_id = s.system_id and l0.station_id = s.station_id
   where s.system_id = p_system_id
     and (i.station_id is not null or not v_anomalous)
      on conflict (system_id, station_id) do update
     set bikes = excluded.bikes,
         docks = excluded.docks,
         flags = excluded.flags,
         is_present = excluded.is_present,
         last_changed_at = excluded.last_changed_at
   where l.last_changed_at < excluded.last_changed_at
     and (l.bikes, l.docks, l.flags, l.is_present)
         is distinct from (excluded.bikes, excluded.docks, excluded.flags, excluded.is_present);
    get diagnostics v_changed = row_count;
  end if;

  -- ── 9. feed_state を前進させる（後退したスナップショットで巻き戻さない。§5 の 26） ──
  update public.feed_state
     set last_etag = p_etag,
         last_observed_at = p_observed_at
   where system_id = p_system_id
     and (last_observed_at is null or last_observed_at < p_observed_at);

  return jsonb_build_object(
    'status', 'inserted',
    'n_stations', v_present,
    'n_new_stations', v_new,
    'n_changed', v_changed,
    'array_length', coalesce(v_array_length, 0),
    'is_anomalous', v_anomalous
  );
end;
$$;

comment on function public.ingest_snapshot(
  text, timestamptz, timestamptz, text, text[], smallint[], smallint[], smallint[], smallint[], text, numeric
) is
  'スナップショットを 1 トランザクションで取り込む。status は inserted / duplicate / locked（§11.3）。';

-- ────────────────────────────────────────────────────────────────
-- 3. finish_fetch — 結果を記録する
-- ────────────────────────────────────────────────────────────────
-- feed_state の更新規則（§11.3）：
--   result が inserted / duplicate / unchanged → 成功。last_success_at を進め、連続失敗を 0 に
--   ok が偽                                    → 連続失敗を加算
--   skipped_recent / locked                    → ログだけ。feed_state は触らない
create or replace function public.finish_fetch(p_system_id text, p_log jsonb)
returns void
language plpgsql
security definer
set search_path = ''
set statement_timeout = '30s'
as $$
declare
  v_result text := p_log->>'result';
  v_ok     boolean := coalesce(public.jsonb_boolean(p_log->'ok'), false);
begin
  insert into public.feed_fetch_log
    (system_id, fetched_at, source, endpoint, http_status, result, ok,
     bytes, duration_ms, n_stations, ratelimit_remaining_day, error, warnings)
  values (
    p_system_id,
    coalesce((p_log->>'fetched_at')::timestamptz, now()),
    coalesce(p_log->>'source', 'cron'),
    p_log->>'endpoint',
    public.jsonb_number(p_log->'http_status')::smallint,
    v_result,
    v_ok,
    public.jsonb_number(p_log->'bytes')::integer,
    public.jsonb_number(p_log->'duration_ms')::integer,
    public.jsonb_number(p_log->'n_stations')::integer,
    public.jsonb_number(p_log->'ratelimit_remaining_day')::integer,
    p_log->>'error',
    case when jsonb_typeof(p_log->'warnings') = 'object' then p_log->'warnings' end
  );

  if v_result in ('inserted', 'duplicate', 'unchanged') then
    update public.feed_state
       set last_success_at = now(), consecutive_errors = 0
     where system_id = p_system_id;
  elsif not v_ok then
    update public.feed_state
       set consecutive_errors = consecutive_errors + 1
     where system_id = p_system_id;
  end if;
end;
$$;

comment on function public.finish_fetch(text, jsonb) is
  '取得結果を feed_fetch_log に記録し、feed_state の成功時刻と連続失敗回数を更新する（§11.3）。';

-- ────────────────────────────────────────────────────────────────
-- 権限
-- ────────────────────────────────────────────────────────────────
-- 0006 の既定権限により、これらの関数は明示的に grant するまで誰も実行できない。
-- 念のため revoke も書いておく（既定が変わっても匿名から呼べないようにする）。
revoke all on function public.jsonb_number(jsonb) from public, anon, authenticated;
revoke all on function public.jsonb_boolean(jsonb) from public, anon, authenticated;
revoke all on function public.begin_fetch(text, integer) from public, anon, authenticated;
revoke all on function public.finish_fetch(text, jsonb) from public, anon, authenticated;
revoke all on function public.ingest_snapshot(
  text, timestamptz, timestamptz, text, text[], smallint[], smallint[], smallint[], smallint[], text, numeric
) from public, anon, authenticated;

grant execute on function public.begin_fetch(text, integer) to service_role;
grant execute on function public.finish_fetch(text, jsonb) to service_role;
grant execute on function public.ingest_snapshot(
  text, timestamptz, timestamptz, text, text[], smallint[], smallint[], smallint[], smallint[], text, numeric
) to service_role;

-- DDL の後にスキーマキャッシュを更新しないと REST が 404 を返す（§4.3 の 2）
notify pgrst, 'reload schema';
