-- 0014 ポート属性の日次同期（W1 プラン §6.9、PR F）
--
-- `station_information` を SCD2（有効期間つき履歴）で `station_attributes` に入れる。
-- 変わったときだけ行が増えるので、1 年経っても行数は「ポート数 ＋ 変更回数」で済む。

-- ────────────────────────────────────────────────────────────────
-- geo_suspect：日本の外接矩形の外にある座標（開発プラン §14）
-- ────────────────────────────────────────────────────────────────
-- 開発プラン §14 が対処を決めていたのに、0003 の DDL に列が無かった。
-- ドコモに実在する：`4826`「アートフォーラムあざみ野」（横浜市青葉区）の経度が
-- 139.553764 ではなく **39.553764**（先頭の 1 が落ちている）。地図には出さず、
-- 時系列は保持する。判定は `packages/gbfs-core` が行い、ここは受け取って保存するだけ
-- （BBox の定義を SQL と TypeScript に二重化しないため）。
alter table public.station_attributes
  add column if not exists geo_suspect boolean not null default false;

comment on column public.station_attributes.geo_suspect is
  '日本の外接矩形（lat 20-46 / lon 122-154）の外にある座標。地図に出さない。時系列は保持する（開発プラン §14）。';

-- ────────────────────────────────────────────────────────────────
-- capacity_is_dynamic：容量が「属性」でないシステムを区別する
-- ────────────────────────────────────────────────────────────────
-- ドコモの `capacity` は固定ラック数ではなく `bikes + docks` の動的値（開発プラン §3.6）。
-- これを SCD2 の比較に入れると、**属性の履歴が容量の変更ログになる**。
-- 実測：2 分あけて 2 回同期しただけで 154 件の版が増え、その **154 件すべてが容量のみの変更**
-- （名前・座標の変更は 0 件）。日次で回すと 1 日およそ 5,000 版、1 年で 180 万行になる。
--
-- 比較から外す。列には版の開始時点で観測した値をそのまま入れる（プラン §6.9 の
-- 「そのまま保存し、意味の解釈は特徴量側で行う」に沿う）。動的な値の最新は
-- `status_snapshots` から `bikes + docks` で得られるので、失われるものは無い。
alter table public.systems
  add column if not exists capacity_is_dynamic boolean not null default false;

comment on column public.systems.capacity_is_dynamic is
  'capacity が固定ラック数ではなく動的値のシステム（ドコモ）。true のとき SCD2 の比較から capacity を外す。';

update public.systems set capacity_is_dynamic = true where system_id = 'docomo-cycle';

comment on column public.station_attributes.capacity is
  'HELLO は vehicle_capacity（文字列）を数値化した実容量。ドコモは版の開始時点で観測した動的値で、'
  '変化しても新しい版を作らない（systems.capacity_is_dynamic）。最新の動的値は status_snapshots から得る。';

-- ────────────────────────────────────────────────────────────────
-- upsert_station_attributes
-- ────────────────────────────────────────────────────────────────
-- 入力は `[{station_id, name, lat, lon, capacity, geo_suspect, raw}, ...]`。
--
-- **入力に含まれないポートの有効行は閉じない。** フィードの一時的な欠落で属性を
-- 失わないため。閉じるかどうかは W2 以降の判断（§6.9）。
create or replace function public.upsert_station_attributes(
  p_system_id  text,
  p_fetched_at timestamptz,
  p_rows       jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
set statement_timeout = '120s'
as $$
declare
  v_lock_key     smallint;
  v_dynamic_cap  boolean;
  v_input        integer;
  v_max_idx      integer;
  v_new_stations integer := 0;
  v_changed      integer := 0;
  v_added        integer := 0;
  v_skipped_old  integer := 0;
  v_suspect      integer := 0;
begin
  -- ── 1. システムの確認 ──
  select lock_key, capacity_is_dynamic into v_lock_key, v_dynamic_cap
    from public.systems where system_id = p_system_id;
  if v_lock_key is null then
    raise exception '未知のシステムです: %', p_system_id using errcode = '22023';
  end if;

  -- 自分自身の多重起動を弾く（日次ジョブ用のクラス）
  if not pg_try_advisory_xact_lock(8422, v_lock_key) then
    return jsonb_build_object('status', 'locked');
  end if;

  -- **idx の採番は ingest_snapshot と同じ資源を触る。** 別のロックにすると、
  -- 毎分の収集と同時に走ったとき両方が同じ max(idx) を読み、unique(system_id, idx)
  -- 違反になる。try ではなく**待つ**。日次ジョブなので数百ミリ秒待って構わない
  perform pg_advisory_xact_lock(8421, v_lock_key);

  -- ── 2. 入力の展開と検査 ──
  -- 一時テーブルにするのは、入力を「登録」「比較」「追加」で 3 回使うため。
  -- **`create temp table ... as` は同じトランザクションで 2 回目に落ちる**ので、
  -- 定義を分けて truncate する（pgTAP は 1 トランザクションで何度も呼ぶ）
  if to_regclass('pg_temp.incoming_station_attributes') is null then
    create temp table incoming_station_attributes (
      station_id  text primary key,
      name        text,
      lat         double precision,
      lon         double precision,
      capacity    smallint,
      geo_suspect boolean not null default false,
      raw         jsonb
    ) on commit drop;
  else
    truncate pg_temp.incoming_station_attributes;
  end if;

  -- 重複があると、どの値を採用したのか説明できない。gbfs-core が排除しているので
  -- 通常は起きないが、契約違反は黙って通さない（ingest_snapshot と同じ方針）。
  -- 主キーがあるので、重複はここで一意制約違反として現れる
  begin
    insert into pg_temp.incoming_station_attributes
      (station_id, name, lat, lon, capacity, geo_suspect, raw)
    select r->>'station_id',
           r->>'name',
           (r->>'lat')::double precision,
           (r->>'lon')::double precision,
           nullif(r->>'capacity', '')::smallint,
           coalesce((r->>'geo_suspect')::boolean, false),
           r->'raw'
      from jsonb_array_elements(coalesce(p_rows, '[]'::jsonb)) as r;
  exception when unique_violation then
    raise exception '入力に重複した station_id があります' using errcode = '22023';
  end;

  select count(*), count(*) filter (where geo_suspect)
    into v_input, v_suspect
    from pg_temp.incoming_station_attributes;

  if exists (select 1 from pg_temp.incoming_station_attributes
              where station_id is null or station_id = '') then
    raise exception 'station_id が空の行があります' using errcode = '22023';
  end if;
  if exists (select 1 from pg_temp.incoming_station_attributes where raw is null) then
    raise exception 'raw が無い行があります（未知フィールドの保全は必須）' using errcode = '22023';
  end if;

  -- ── 3. 未登録ポートの登録。idx は 0 起点で密に採番する（§11.1） ──
  select coalesce(max(idx), -1) into v_max_idx from public.stations where system_id = p_system_id;

  with candidate as (
    select i.station_id, row_number() over (order by i.station_id) as seq
      from pg_temp.incoming_station_attributes i
     where not exists (
       select 1 from public.stations s
        where s.system_id = p_system_id and s.station_id = i.station_id
     )
  )
  insert into public.stations (system_id, station_id, idx)
  select p_system_id, station_id, v_max_idx + seq from candidate;
  get diagnostics v_new_stations = row_count;

  -- ── 4. 値が変わった有効行を閉じる ──
  -- `is distinct from` を使う（NULL 同士を「同じ」として扱う）。capacity は
  -- 読み取れないことが正常にあるため、NULL → NULL を変更とみなしてはいけない
  update public.station_attributes a
     set valid_to = p_fetched_at
    from pg_temp.incoming_station_attributes i
   where a.system_id = p_system_id
     and a.station_id = i.station_id
     and a.valid_to is null
     and a.valid_from < p_fetched_at
     and (a.name is distinct from i.name
       or a.lat  is distinct from i.lat
       or a.lon  is distinct from i.lon
       or (not v_dynamic_cap and a.capacity is distinct from i.capacity));
  get diagnostics v_changed = row_count;

  -- 過去の時刻で取り込もうとしたのに値が違う場合。ベースラインを後から入れると起きる。
  -- 黙って捨てず、件数を返して気づけるようにする
  select count(*) into v_skipped_old
    from public.station_attributes a
    join pg_temp.incoming_station_attributes i
      on i.station_id = a.station_id
   where a.system_id = p_system_id
     and a.valid_to is null
     and a.valid_from >= p_fetched_at
     and (a.name is distinct from i.name
       or a.lat  is distinct from i.lat
       or a.lon  is distinct from i.lon
       or (not v_dynamic_cap and a.capacity is distinct from i.capacity));

  -- ── 5. 有効行が無いポートに新しい行を足す（新規ポートと、いま閉じたポート） ──
  insert into public.station_attributes
    (system_id, station_id, valid_from, valid_to, name, lat, lon, capacity, geo_suspect, raw)
  select p_system_id, i.station_id, p_fetched_at, null,
         i.name, i.lat, i.lon, i.capacity, i.geo_suspect, i.raw
    from pg_temp.incoming_station_attributes i
   where not exists (
     select 1 from public.station_attributes a
      where a.system_id = p_system_id and a.station_id = i.station_id and a.valid_to is null
   );
  get diagnostics v_added = row_count;

  return jsonb_build_object(
    'status', 'ok',
    'n_input', v_input,
    'n_new_stations', v_new_stations,
    'n_changed', v_changed,
    'n_unchanged', v_input - v_added,
    'n_versions_added', v_added,
    'n_geo_suspect', v_suspect,
    'capacity_is_dynamic', v_dynamic_cap,
    'n_skipped_older', v_skipped_old
  );
end;
$$;

comment on function public.upsert_station_attributes(text, timestamptz, jsonb) is
  'station_information を SCD2 で station_attributes に取り込む。入力に無いポートの有効行は閉じない（§6.9）。';

-- ────────────────────────────────────────────────────────────────
-- 権限
-- ────────────────────────────────────────────────────────────────
revoke all on function public.upsert_station_attributes(text, timestamptz, jsonb)
  from public, anon, authenticated;
grant execute on function public.upsert_station_attributes(text, timestamptz, jsonb) to service_role;

-- 日次同期は feed_fetch_log ではなく job_runs に記録する。あちらは status 専用で
-- feed 列を持たず、混ぜると取得率と誤検知の指標が汚れる
grant execute on function public.job_started(text) to service_role;
grant execute on function public.job_finished(bigint, text, jsonb) to service_role;

notify pgrst, 'reload schema';
