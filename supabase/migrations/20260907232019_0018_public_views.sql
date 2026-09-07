-- 0018 公開 API `/v1` が読むビュー（W2 プラン §5.7、PR E）
--
-- **匿名ロールに基底テーブルの権限を与えない**（CLAUDE.md §5）。読めるのはここで作る
-- 2 つのビューだけで、権限は pgTAP（`supabase/tests/0010_public_views.sql`）で固定する。
--
-- ビューは `security_invoker = false`（Postgres の既定）で作る。実行はビューの所有者
-- （postgres）の権限で行われ、所有者は基底テーブルの RLS を迂回する（FORCE していない）。
-- これは意図した設計で、「匿名は絞り込まれたビューだけを読める」を実現する唯一の形である。
--
-- **内部の約束をビューの外に漏らさない**：
--   * `-1`（登録済みだが観測されていない）は **NULL** にする。0 と区別する（W1 プラン §11.1）
--   * `flags` のビット和は真偽値 3 つに開く。ビットの意味は API の利用者の関心ではない
--   * `geo_suspect`（日本の外接矩形の外にある壊れた座標）は出さない（開発プラン §14）
--   * 停止したシステム（`systems.is_active = false`）は出さない。古い値を「現在値」にしない

-- ────────────────────────────────────────────────────────────────
-- v1_feeds — フィードの鮮度と性質
-- ────────────────────────────────────────────────────────────────
-- `/v1/meta` と `/v1/stations` の両方が使う。**鮮度はポート単位ではなくフィード単位**で、
-- ポートごとに持たせると 2 万行に同じ値が並ぶだけなので分けてある（§11.3）。
create view public.v1_feeds as
select s.system_id,
       s.display_name,
       s.expected_cadence_s,
       s.poll_interval_s,
       s.capacity_is_dynamic,
       f.last_observed_at
  from public.systems s
  join public.feed_state f on f.system_id = s.system_id
 where s.is_active;

comment on view public.v1_feeds is
  '公開 API 用。フィードの鮮度（last_observed_at）と、鮮度の判定に要る周期を返す。稼働中のシステムのみ。';

-- ────────────────────────────────────────────────────────────────
-- v1_stations_current — 地図に出せるポートの現在値
-- ────────────────────────────────────────────────────────────────
-- 属性（名称・座標・容量）は `left join` にする。新しいポートは `station_status` に
-- 現れた時点で台帳に載るが、属性を得るのは次の日次同期（最大 24 時間後）なので、
-- その間 `station_attributes` に行が無い（開発プラン §8.3）。**行を落とさず NULL で返す。**
-- 座標が無いポートは bbox 検索の結果から自然に外れる。
create view public.v1_stations_current as
select l.system_id,
       l.station_id,
       a.name,
       a.lat,
       a.lon,
       a.capacity,
       -- -1 は「登録済みだが観測されていない」。0（本当に 0 台）と区別する
       nullif(l.bikes, -1) as bikes,
       nullif(l.docks, -1) as docks,
       case when l.flags < 0 then null else (l.flags & 1) > 0 end as is_installed,
       case when l.flags < 0 then null else (l.flags & 2) > 0 end as is_renting,
       case when l.flags < 0 then null else (l.flags & 4) > 0 end as is_returning,
       l.is_present,
       l.last_changed_at
  from public.station_status_latest l
  join public.systems sy
    on sy.system_id = l.system_id and sy.is_active
  left join public.station_attributes a
    on a.system_id = l.system_id
   and a.station_id = l.station_id
   and a.valid_to is null
 where coalesce(a.geo_suspect, false) = false;

comment on view public.v1_stations_current is
  '公開 API 用。ポートの現在値。-1 は NULL に、flags は真偽値に開いてある。geo_suspect と停止中システムは除く。';

-- ────────────────────────────────────────────────────────────────
-- bbox 検索のための索引
-- ────────────────────────────────────────────────────────────────
-- `/v1/stations` は緯度経度の範囲で絞る。索引が無いと現在有効な属性行（実測 20,742 行）を
-- 毎回全件走査する（実測 6.9 ms のうちの大半）。ポートが増えるほど効いてくる。
--
-- 部分索引にするのは、走査するのが常に「現在有効で、座標の壊れていない行」だからで、
-- 索引を小さく保てる。日次同期の書き込みが増えるが 1 日 1 回・数千行なので無視できる。
create index if not exists station_attributes_current_geo_idx
  on public.station_attributes (lat, lon)
  where valid_to is null and not geo_suspect;

-- ────────────────────────────────────────────────────────────────
-- 権限
-- ────────────────────────────────────────────────────────────────
-- 0006 で `alter default privileges ... revoke all on tables from anon, authenticated` を
-- 掛けてあるので、ビューを作っただけでは匿名から見えない。明示的に与える。
--
-- `authenticated` には与えない。認証の仕組みがまだ無く、到達し得るのは `anon` だけで、
-- 必要になった時点で 1 行足せばよい（最小権限）。
grant select on public.v1_feeds to anon;
grant select on public.v1_stations_current to anon;

-- サービスロールは既定権限で読めるが、ビューは 0006 の `alter default privileges` より
-- 後に作られるため明示しておく（読めないと `/v1` が動かない）
grant select on public.v1_feeds to service_role;
grant select on public.v1_stations_current to service_role;

-- PostgREST のスキーマキャッシュを更新する。忘れると REST が 404 を返す（W1 プラン §4.3 の 2）
notify pgrst, 'reload schema';
