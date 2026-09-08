-- 0019 bbox 索引の述語を直す（W2 プラン §5.7 の続き）
--
-- **0018 で作った索引は一度も使われていなかった。** 本番の実行計画で全件走査が選ばれ続けた。
--
-- 原因は部分索引の述語の書き方である。ビューは属性を `left join` しているため、
-- 座標の無いポート（属性をまだ取れていない）を落とさないよう `where` を
-- **`coalesce(a.geo_suspect, false) = false`** と書いている。一方 0018 の索引の述語は
-- **`not geo_suspect`** だった。人間には同じに見えるが、**Postgres の述語の含意判定は
-- `NOT COALESCE(x, false)` から `NOT x` を導けない**ため、索引の候補にすら入らない。
--
-- 実測（本番、2026-09-08）：
--   ビューと同じ述語 → Bitmap Heap Scan にフォールバックして 11.5 ms
--   `not geo_suspect` と書き換えた同じ問い合わせ → Index Only Scan で **0.27 ms**
-- ローカルで本番と同じ 20,000 行を作って比べた結果も同じ向きだった（13.9 ms → 0.95 ms）。
--
-- **直し方は「索引の述語をビューに合わせる」ではなく「ビューに必ず含まれる条件だけにする」。**
-- `valid_to is null` はビューの結合条件にそのまま現れるので、含意が自明に成り立つ。
-- `geo_suspect` の除外は索引を引いたあとの絞り込みに任せる。該当は実測 1 件だけで、
-- 索引に含めても実質的な損はない。
--
-- **この索引の述語に `geo_suspect` を足さないこと。** 足すと静かに使われなくなる。

drop index if exists public.station_attributes_current_geo_idx;

create index if not exists station_attributes_current_geo_idx
  on public.station_attributes (lat, lon)
  where valid_to is null;

comment on index public.station_attributes_current_geo_idx is
  '/v1/stations の bbox 検索用。述語は valid_to is null のみ。geo_suspect を足すと、ビューの coalesce(...) から含意を導けず索引が使われなくなる（0019）。';
