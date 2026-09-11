-- 0039 `/v1/trip-check` が読む近傍のビュー（W4 プラン §6.7、W4-26）
--
-- **公開経路が触れてよいのは `v1_` で始まるビューだけ**（CLAUDE.md §5、`read-port.ts` の
-- 冒頭）。`station_neighbors` は基底テーブルで匿名に権限が無いので、**この規律のためだけに
-- 1 枚足す**。
--
-- **代わりに bbox で近傍を引く案は採らなかった。** 座標から距離を出し直すことになり、
-- **距離の実装が SQL（`rebuild_geo`）と TS の 2 つ**になる。同じ 2 点に別の距離を出す
-- 状態を作らない（W4-26）。
--
-- **絞り込みはビューに焼き付けない。** `distance_m <= 400` も `same_system` も
-- `/v1/trip-check` の今日の都合であって、近傍という事実の性質ではない。ビューは
-- 素直に写し、絞るのは問い合わせ側（`view-query.ts`）で行う——`v1_stations_current` が
-- bbox を焼き付けていないのと同じ形である。

-- ────────────────────────────────────────────────────────────────
-- v1_station_neighbors — 500 m 以内のポートの組
-- ────────────────────────────────────────────────────────────────
-- 元の表は**片方向の行を両向きぶん持つ**ので、`station_id` で絞れば「そのポートの近傍」が
-- そのまま出る。日次の `rebuild_geo` が全置換する（W3 プラン §10.2）。
--
-- **停止したシステムのポートは出さない。** `v1_stations_current` と同じ約束にしておかないと、
-- 代替候補として出したポートを `/v1/stations` が知らない、という食い違いが起きる。
create view public.v1_station_neighbors as
select n.system_id,
       n.station_id,
       n.nb_system_id,
       n.nb_station_id,
       n.distance_m,
       n.same_system
  from public.station_neighbors n
  join public.systems s  on s.system_id  = n.system_id     and s.is_active
  join public.systems sn on sn.system_id = n.nb_system_id  and sn.is_active;

comment on view public.v1_station_neighbors is
  '公開 API 用。半径 500 m 以内のポートの組（片方向。両向きの行が入る）。稼働中のシステムのみ。/v1/trip-check の代替候補に使う。';

-- ────────────────────────────────────────────────────────────────
-- 権限
-- ────────────────────────────────────────────────────────────────
-- 0006 の `alter default privileges ... revoke all on tables from anon, authenticated` が
-- 効いているので、作っただけでは匿名から見えない。0018 と同じく明示的に与える。
-- `authenticated` には与えない（最小権限）。
grant select on public.v1_station_neighbors to anon;
grant select on public.v1_station_neighbors to service_role;

-- PostgREST のスキーマキャッシュを更新する。忘れると REST が 404 を返す（W1 プラン §4.3 の 2）
notify pgrst, 'reload schema';
