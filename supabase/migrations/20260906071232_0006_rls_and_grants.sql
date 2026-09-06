-- 0006 RLS と権限（W1 プラン §6.3、§4.3 の 1）
--
-- **落とし穴**：本番プロジェクトは「新規テーブルの自動公開」を OFF にしてある。この設定は
-- `anon` / `authenticated` だけでなく **`service_role` の既定権限も剥奪する**。RLS を有効に
-- しただけでは足りず、明示的な grant が無いと REST 経由で 42501（permission denied）になる。
-- 実測で確認済み。
--
-- W1 の方針：
--   * 全テーブルで RLS を有効にする。ポリシーは作らない
--     → BYPASSRLS を持つ service_role だけが読み書きできる
--   * anon / authenticated には**何も与えない**。読み取り用ビューは W2 で
--     `station_status_latest` の上に作り、そこにだけ select を与える
--   * 行セキュリティは GRANT に**追加で**適用される。GRANT が無ければポリシー以前に届かない

-- ────────────────────────────────────────────────────────────────
-- RLS を全テーブルで有効化
-- ────────────────────────────────────────────────────────────────
alter table public.systems               enable row level security;
alter table public.stations              enable row level security;
alter table public.station_attributes    enable row level security;
alter table public.status_snapshots      enable row level security;
alter table public.station_status_latest enable row level security;
alter table public.feed_state            enable row level security;
alter table public.feed_fetch_log        enable row level security;
alter table public.job_runs              enable row level security;
alter table public.daily_quality         enable row level security;
alter table public.app_config            enable row level security;

-- 親テーブル経由でアクセスするためパーティションへの個別設定は不要だが、
-- 直接参照された場合にも同じ扱いになるよう DEFAULT にも掛けておく
alter table public.status_snapshots_default enable row level security;

-- ────────────────────────────────────────────────────────────────
-- 匿名ロールから取り上げる
-- ────────────────────────────────────────────────────────────────
revoke all on all tables in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;

alter default privileges in schema public revoke all on tables from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;

-- `public` スキーマの USAGE は PUBLIC ロール経由で全員が持っており、anon から個別に
-- revoke しても外れない。PUBLIC から取り上げるのは Supabase 内部を壊しかねないので行わない。
-- 実効的な防御はテーブル権限（上の revoke）と、関数の既定権限（下）である。
--
-- 関数の既定権限を安全側にする。PR B 以降で追加する RPC が、書いた本人が忘れても
-- 匿名から呼べる状態にならないようにする（§4.3 の 2）。2 行必要な理由は次のとおり：
--
--   1. 組み込みの既定は「関数は PUBLIC が EXECUTE できる」。**スキーマ限定の
--      ALTER DEFAULT PRIVILEGES はこれに追加されるだけで打ち消せない**（実測で確認）。
--      そのため、スコープを限定せずに PUBLIC から取り上げる。影響するのは postgres が
--      作る関数だけで、Supabase 自身のオブジェクトは supabase_admin 等が作るため無関係。
--   2. ローカルの Supabase は、postgres が public に作る関数へ anon / authenticated の
--      EXECUTE を既定で与える。こちらはスキーマ限定の既定権限なので個別に取り上げる。
alter default privileges revoke execute on functions from public;
alter default privileges in schema public revoke execute on functions from anon, authenticated;

-- ────────────────────────────────────────────────────────────────
-- サービスロールに明示的に与える
-- ────────────────────────────────────────────────────────────────
grant usage on schema public to service_role;
grant select, insert, update, delete on all tables in schema public to service_role;
grant usage, select on all sequences in schema public to service_role;

-- 将来このマイグレーション以降に作られるテーブルにも効かせる。
-- パーティションは親テーブル経由でアクセスするため個別の grant は不要
alter default privileges in schema public
  grant select, insert, update, delete on tables to service_role;
alter default privileges in schema public
  grant usage, select on sequences to service_role;

-- 保守関数は誰でも実行できる状態にしない（DDL を行うため）。
-- 上の既定権限の変更は「これ以降に作る関数」にしか効かないので、0005 で作った 2 本は個別に外す
revoke all on function public.ensure_snapshot_partitions(integer) from public, anon, authenticated;
revoke all on function public.drop_expired_snapshot_partitions(integer) from public, anon, authenticated;
grant execute on function public.ensure_snapshot_partitions(integer) to service_role;
grant execute on function public.drop_expired_snapshot_partitions(integer) to service_role;

-- PostgREST のスキーマキャッシュを更新する。これを忘れると REST が 404 を返す（§4.3 の 2）
notify pgrst, 'reload schema';
