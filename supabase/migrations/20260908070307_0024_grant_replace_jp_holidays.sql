-- 0024 `replace_jp_holidays` の実行権限（W3 プラン §12 の 90）
--
-- **本番だけで 42501 になった。** ローカルでは通っていた。
--
-- 0006 の冒頭が書いているとおり、本番プロジェクトは「新規オブジェクトの自動公開」を
-- OFF にしてあり、`postgres` が作ったものへの既定権限が `service_role` に付かない。
-- ただし**表とシーケンスには 0006 が `alter default privileges` を入れてある**ので付く。
-- 抜けているのは**関数**である。
--
--   pg_default_acl（本番）
--     postgres / r（表）      → {postgres=arwdDxtm/postgres, service_role=arwdDxtm/postgres}
--     postgres / f（関数）    → {postgres=X/postgres}          ← service_role が無い
--
-- したがって **PostgREST から呼ぶ関数には毎回 `grant execute` を書く**必要がある。
-- 既存の 10 個（`ingest_snapshot`・`job_started` ほか）はそうしてあり、0023 で足した
-- `replace_jp_holidays` だけ忘れていた。
--
-- **同じ抜けが二度と起きないよう、0003 の pgTAP に不変条件を足した**（関数を
-- 「PostgREST から呼ぶ」と「pg_cron からだけ呼ぶ」に分類し、前者に明示的な grant が
-- あることと、どちらにも入っていない関数が無いことを見張る）。
grant execute on function public.replace_jp_holidays(jsonb) to service_role;

notify pgrst, 'reload schema';
