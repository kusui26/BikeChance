-- 0003 W1 のコアテーブル（W1 プラン §6.3、契約は §11）
--
-- 設計の要点：
--   * スナップショットは「1 フィード更新 = 1 行」。ポート毎の値は smallint[] に持つ。
--     1 ポート 1 行の長形式は 1 日 1,000 万行になり Pro の 8 GB を数週間で使い切る（開発プラン §5.2）。
--   * 配列は `stations.idx` の順に並ぶ。Postgres の配列は 1 起点なので、idx のポートの値は
--     `arr[idx + 1]` にある。欠損（登録済みだが今回のフィードに現れなかった）は -1（§11.1）。
--   * 収集の最短経路では `stations` に登録以外の書き込みをしない。`last_seen_at` と
--     `is_active` は日次ジョブが直近 25 時間のスナップショットから計算する（W1-13）。
--
-- 列名は `packages/shared` の型（SystemDefinition 等）と揃える。

-- ────────────────────────────────────────────────────────────────
-- 事業者システム
-- ────────────────────────────────────────────────────────────────
create table public.systems (
  system_id          text primary key,
  display_name       text not null,
  operator_name      text not null,
  gbfs_base_url      text not null,
  expected_cadence_s integer not null,
  poll_interval_s    integer not null default 60,
  lock_key           smallint not null unique,
  is_active          boolean not null default true,
  constraint systems_expected_cadence_positive check (expected_cadence_s > 0),
  constraint systems_poll_interval_positive check (poll_interval_s > 0),
  constraint systems_lock_key_positive check (lock_key > 0)
);

comment on table public.systems is 'GBFS を提供する事業者システム。W1 では hellocycling と docomo-cycle の 2 行。';
comment on column public.systems.expected_cadence_s is 'フィードの実測更新周期（秒）。HELLO 300 / ドコモ 80。監視の期待値に使う。';
comment on column public.systems.lock_key is
  'pg_try_advisory_xact_lock の第 2 引数。hashtext() のような文書化されていない内部関数に依存しないため、明示的な列として持つ（§11.4）。';
comment on column public.systems.gbfs_base_url is
  '参照用。収集器は packages/shared の定数を使い、この列は読まない（トークン付き URL を組み立てるのは odpt-fetch.ts だけ。W1-21）。';

-- ────────────────────────────────────────────────────────────────
-- ポート台帳（配列位置の割り当て）
-- ────────────────────────────────────────────────────────────────
create table public.stations (
  system_id     text not null references public.systems (system_id),
  station_id    text not null,
  idx           integer not null,
  first_seen_at timestamptz not null default now(),
  last_seen_at  timestamptz,
  is_active     boolean not null default true,
  pref_code     smallint,
  muni_code     integer,
  primary key (system_id, station_id),
  unique (system_id, idx),
  constraint stations_idx_non_negative check (idx >= 0)
);

comment on table public.stations is 'ポート台帳。行は削除しない。idx は一度割り当てたら不変（§11.1）。';
comment on column public.stations.idx is
  'システム内で一意・不変・0 起点の密なインデックス。スナップショットの配列は idx 順に並び、arr[idx + 1] がそのポートの値。';
comment on column public.stations.last_seen_at is
  '日次ジョブ refresh_station_activity が直近 25 時間のスナップショットから計算して書く。収集の最短経路では触らない（W1-13）。';
comment on column public.stations.is_active is '72 時間観測されなければ false、再出現で true。日次ジョブが更新する。';
comment on column public.stations.pref_code is '座標から導出する都道府県コード（JIS X 0401）。W3 まで NULL。';
comment on column public.stations.muni_code is '座標から導出する市区町村コード（JIS X 0402）。W3 まで NULL。';

-- ────────────────────────────────────────────────────────────────
-- ポート属性の履歴（SCD Type 2）
-- ────────────────────────────────────────────────────────────────
create table public.station_attributes (
  system_id  text not null,
  station_id text not null,
  valid_from timestamptz not null,
  valid_to   timestamptz,
  name       text,
  lat        double precision,
  lon        double precision,
  capacity   smallint,
  raw        jsonb not null,
  primary key (system_id, station_id, valid_from),
  foreign key (system_id, station_id) references public.stations (system_id, station_id),
  constraint station_attributes_valid_range check (valid_to is null or valid_to > valid_from)
);

-- 「現在有効な行」の検索と、1 ポートにつき有効行が 1 本だけであることの保証を兼ねる
create unique index station_attributes_current_idx
  on public.station_attributes (system_id, station_id)
  where valid_to is null;

comment on table public.station_attributes is
  'station_information の履歴。status にしか現れないポート（ドコモで実測 11 件）は行を持たない。属性が無いポートがある前提で扱う（§11.1）。';
comment on column public.station_attributes.valid_to is '現在有効な行は NULL。値が変わったときに旧行を閉じて新行を足す。';
comment on column public.station_attributes.capacity is
  'HELLO は非標準の vehicle_capacity（文字列）を数値化した値、ドコモは動的な capacity。意味の解釈は特徴量側で行う（開発プラン §3.6）。';
comment on column public.station_attributes.raw is 'GBFS オブジェクト全体。未知フィールドを保全し、後から再解釈できるようにする。';

-- ────────────────────────────────────────────────────────────────
-- スナップショット（1 フィード更新 = 1 行）。月次 RANGE パーティション
-- ────────────────────────────────────────────────────────────────
create table public.status_snapshots (
  system_id      text not null references public.systems (system_id),
  observed_at    timestamptz not null,
  fetched_at     timestamptz not null,
  n_stations     integer not null,
  is_anomalous   boolean not null default false,
  bikes          smallint[] not null,
  docks          smallint[] not null,
  flags          smallint[] not null,
  reported_age_s smallint[] not null,
  raw_path       text not null,
  primary key (system_id, observed_at),
  constraint status_snapshots_n_stations_non_negative check (n_stations >= 0),
  constraint status_snapshots_array_lengths check (
    array_length(bikes, 1) is not distinct from array_length(docks, 1)
    and array_length(bikes, 1) is not distinct from array_length(flags, 1)
    and array_length(bikes, 1) is not distinct from array_length(reported_age_s, 1)
  )
) partition by range (observed_at);

comment on table public.status_snapshots is
  '1 フィード更新 = 1 行。配列は stations.idx 順で、arr[idx + 1] がそのポートの値（§11.1）。';
comment on column public.status_snapshots.observed_at is 'フィードの last_updated。パーティションキー。';
comment on column public.status_snapshots.fetched_at is '当方が取得を完了した時刻。observed_at との差が公開遅延。';
comment on column public.status_snapshots.n_stations is
  'このスナップショットに現れたポート数。配列長（= 取り込み時点の登録済みポート数）とは異なる。';
comment on column public.status_snapshots.is_anomalous is
  '出現ポート数が登録済みの 50% 未満だった。保存はするが station_status_latest の不在反転は行っていない（W1-18）。';
comment on column public.status_snapshots.bikes is 'num_bikes_available。欠損は -1。';
comment on column public.status_snapshots.docks is 'num_docks_available。欠損は -1。';
comment on column public.status_snapshots.flags is
  'ビット和：1=is_installed / 2=is_renting / 4=is_returning（すべて真なら 7）。欠損は -1。';
comment on column public.status_snapshots.reported_age_s is
  'observed_at − last_reported の秒数。負値は 0 に丸める（-1 は欠損専用のため衝突させない。§5 の 5）。';
comment on column public.status_snapshots.raw_path is 'Storage 上の生 gzip JSON のパス（§11.5）。一次ソースへの参照。';

-- 月替わりの安全網。保守ジョブが止まっても保存が続く（W1-15）。行が入ったら監視で気づく
create table public.status_snapshots_default partition of public.status_snapshots default;

comment on table public.status_snapshots_default is
  'DEFAULT パーティション。ここに行が入るのは該当月のパーティションが作られていない印で、監視が通知する（PR E1）。';

-- ────────────────────────────────────────────────────────────────
-- 最新状態（API の「現在値」用）
-- ────────────────────────────────────────────────────────────────
create table public.station_status_latest (
  system_id       text not null,
  station_id      text not null,
  bikes           smallint not null,
  docks           smallint not null,
  flags           smallint not null,
  is_present      boolean not null,
  last_changed_at timestamptz not null,
  primary key (system_id, station_id),
  foreign key (system_id, station_id) references public.stations (system_id, station_id)
) with (fillfactor = 70);

comment on table public.station_status_latest is
  '変化した行だけ更新する。フィード全体の鮮度は feed_state.last_observed_at を使う（§11.3）。';
comment on column public.station_status_latest.is_present is '最新スナップショットにそのポートが現れたか。消えても値は保持する。';
comment on column public.station_status_latest.last_changed_at is
  '上の 4 つのいずれかが最後に「変わった」時刻。「最後に観測した時刻」ではない（§5 の 4）。';

-- ────────────────────────────────────────────────────────────────
-- 収集の状態と記録
-- ────────────────────────────────────────────────────────────────
create table public.feed_state (
  system_id          text primary key references public.systems (system_id),
  last_fetch_at      timestamptz,
  last_success_at    timestamptz,
  last_observed_at   timestamptz,
  last_etag          text,
  consecutive_errors integer not null default 0,
  constraint feed_state_errors_non_negative check (consecutive_errors >= 0)
);

comment on table public.feed_state is
  '列ごとに書く者を固定することで、Storage 成功 → RPC 失敗 → 304 でスキップ、という静かな取りこぼしを構造的に防ぐ（W1-14、§11.3）。';
comment on column public.feed_state.last_fetch_at is 'begin_fetch が書く。二重起動の抑止とウォッチドッグの判定に使う。';
comment on column public.feed_state.last_success_at is 'finish_fetch が書く。ODPT から正常な応答を得た最後の時刻。';
comment on column public.feed_state.last_etag is
  'ingest_snapshot だけが書く。最後に「取り込みに成功した」スナップショットの ETag。取りこぼし防止の要（W1-14）。';
comment on column public.feed_state.last_observed_at is
  'ingest_snapshot だけが書く。前進のみで、後退したスナップショットを渡されても巻き戻さない（§5 の 26）。';

create table public.feed_fetch_log (
  id                      bigint generated always as identity primary key,
  system_id               text not null,
  fetched_at              timestamptz not null default now(),
  source                  text not null default 'cron',
  endpoint                text,
  http_status             smallint,
  result                  text not null,
  ok                      boolean not null,
  bytes                   integer,
  duration_ms             integer,
  n_stations              integer,
  ratelimit_remaining_day integer,
  error                   text,
  warnings                jsonb,
  constraint feed_fetch_log_source_valid check (source in ('cron', 'watchdog', 'manual')),
  constraint feed_fetch_log_endpoint_valid check (endpoint is null or endpoint in ('token', 'public')),
  constraint feed_fetch_log_result_valid check (
    result in ('inserted', 'duplicate', 'unchanged', 'skipped_recent', 'locked', 'error')
  )
);

-- 直近 N 時間の集計と、30 日超の削除ジョブの両方がこの列で絞る
create index feed_fetch_log_fetched_at_idx on public.feed_fetch_log (fetched_at);

comment on table public.feed_fetch_log is '毎回の呼び出しを記録する。30 日で削除する（PR E1）。system_id に外部キーを張らないのは毎分の書き込み経路を軽くするため。';
comment on column public.feed_fetch_log.source is 'cron（Vercel Cron）/ watchdog（pg_cron の再起動）/ manual（手動）。ウォッチドッグの到達を見分ける（§5 の 25）。';
comment on column public.feed_fetch_log.endpoint is 'token（認証付き）/ public（フォールバック）。**URL は保存しない**（W1-21）。';
comment on column public.feed_fetch_log.ratelimit_remaining_day is
  'ODPT の X-RateLimit-Remaining-day。上限 24,000 に対し想定使用量は 2,880。認証付きのときだけ値が入る（W1-21）。';
comment on column public.feed_fetch_log.error is 'phase・HTTP ステータス・例外名だけの伏字化済みメッセージ。URL とトークンを含まない。';

-- ────────────────────────────────────────────────────────────────
-- 運用
-- ────────────────────────────────────────────────────────────────
create table public.job_runs (
  id          bigint generated always as identity primary key,
  job_name    text not null,
  started_at  timestamptz not null default now(),
  finished_at timestamptz,
  status      text not null,
  detail      jsonb,
  constraint job_runs_status_valid check (status in ('running', 'ok', 'failed'))
);

create index job_runs_name_started_idx on public.job_runs (job_name, started_at desc);

comment on table public.job_runs is 'pg_cron と Vercel Cron から呼ばれるジョブの実行記録。監視が failed を検知する（PR E1）。';

create table public.daily_quality (
  system_id      text not null references public.systems (system_id),
  quality_date   date not null,
  n_snapshots    integer not null,
  n_expected     integer not null,
  max_gap_s      integer,
  n_errors       integer not null default 0,
  n_anomalous    integer not null default 0,
  db_bytes_delta bigint,
  computed_at    timestamptz not null default now(),
  primary key (system_id, quality_date)
);

comment on table public.daily_quality is '日次 QA レポート。人が読むためのものなので日付は JST（§5 の 17）。';
comment on column public.daily_quality.quality_date is
  '**JST の日付**。Storage のパスと Parquet のパーティションは UTC で、用途が違うことを明示する（§11.5）。';
comment on column public.daily_quality.computed_at is '集計した時刻。ジョブを再実行したときにどちらの結果かを見分ける。';

create table public.app_config (
  key        text primary key,
  value      text not null,
  updated_at timestamptz not null default now()
);

comment on table public.app_config is
  '秘密でない設定値。秘密は Vault に置く。W1 では project_base_url のみ（ウォッチドッグが叩く先。§11.7）。';
