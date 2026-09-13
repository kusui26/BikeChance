-- ────────────────────────────────────────────────────────────────
-- 0047 — `capacity_est = 0` を通す（0045 の制約を緩める）
-- ────────────────────────────────────────────────────────────────
-- 0045 は「0 は『大きさ 0』ではなく『分からない』なので、行ごと作らない側で弾く」と
-- 書いて `capacity_est > 0` を掛けた。**実データを入れたら弾かれた。**
--
-- 実測（2026-09-12 の参照スナップショット、20,804 ポート）：
--
--   | `capacity_est` | 件数 |
--   |---|---:|
--   | NULL（1 度も観測されなかった） | **0** |
--   | **0**（7 日のあいだ `bikes + docks` が 0 のまま） | **37**（ドコモ 35・HELLO 2） |
--   | 正 | 20,767 |
--
-- **0 は「分からない」ではなく「観測した結果が 0」である。** 1 度も観測されなかった
-- ポートは `capacity_est` が NULL になり、そもそも行が作られない（`capacity_rows` が
-- 落とす）。**このリポジトリは 0 と NULL を一貫して区別している**——`-1`（未観測）を
-- ビューが NULL に開き、`bikes = 0`（本当に 0 台）と分けているのと同じ約束である
-- （データ辞書 §5.1）。**0 を NULL に丸めると、そこだけ約束が破れる。**
--
-- **消す・0 に丸める・NULL にする、のどれも採らない。** そのまま渡し、読む側が
-- 「7 日のあいだ 1 台も並ばなかったポート」と読めるようにする（37 件・0.18%）。
--
-- 列も既定値も変えない（`check` の付け替えだけ）。
-- ────────────────────────────────────────────────────────────────

alter table public.station_capacity_est
  drop constraint if exists station_capacity_est_positive;

alter table public.station_capacity_est
  add constraint station_capacity_est_non_negative check (capacity_est >= 0);

comment on column public.station_capacity_est.capacity_est is
  '前日までの 7 日の max(bikes + docks)。固定ラック数（station_attributes.capacity）とは別物。0 は「7 日とも 0 台 0 枠だった」という観測で、「分からない」ではない（分からないポートは行そのものが無い。0047）。';

notify pgrst, 'reload schema';
