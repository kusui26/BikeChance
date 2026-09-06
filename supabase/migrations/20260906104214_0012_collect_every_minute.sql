-- 0012 収集を毎分にする（W1 プラン §6.8 の PR E2 ＝ M0）
--
-- 変えるのはこの 1 行と `vercel.json` の `crons` だけ。監視の閾値は
-- `collect_interval_s` から導出しているので、ここを 60 にすれば一緒に締まる（W1-29）。
--
--   ウォッチドッグ    collect_interval_s × 2 + 30            630 秒 → **150 秒**
--   フィード停滞      max(expected_cadence_s × 3, ×3)  両系 15 分 → **HELLO 15 分・ドコモ 4 分**
--
-- **適用の順番に注意。** 先にこのマイグレーションを流すと、収集がまだ 5 分間隔なのに
-- ウォッチドッグの閾値だけ 150 秒になり、毎分ウォッチドッグ経由で収集が走る。
-- ドコモの停滞検知も 4 分になるため誤報が出る。**`vercel.json` の本番デプロイが
-- 毎分で回り始めたことを確認してから流す。** 逆順（デプロイ先・設定後）なら、
-- 一時的に監視が緩いだけで害はない。
--
-- 巻き戻し：`vercel.json` を `*/5` に戻して再デプロイし、この値を 300 に戻す。
update public.app_config
   set value = '60', updated_at = now()
 where key = 'collect_interval_s';

-- 設定行が無い環境（0008 より前から続く DB は無いはずだが）でも辻褄を合わせる
insert into public.app_config (key, value)
select 'collect_interval_s', '60'
 where not exists (select 1 from public.app_config where key = 'collect_interval_s');
