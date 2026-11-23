-- 当てはめ直し 2026-11-22 の昇格（docs/refits/2026-11-22.md。W8-04）。**利用者の確認を取ってから**、推論の合間
-- （毎 5 分の 4 分台の 5〜40 秒。HELLO は 0 分・ドコモは 2 分に始まる。W11 の PR P）に流す。1 つでも失敗したら止まる。
--   psql "$SUPABASE_DB_URL" -X -P pager=off -f docs/refits/2026-11-22-promote.sql
-- B3 だけの週なので、合成器は shadow を飛ばす（D-44・W6-39）。比べ役の B3 は新しい版を shadow に
-- 置き直す——shadow に上げると、前の shadow（baseline-b3-v0-20261115）は自動で retired になる（0038）。
-- このひな形は weekly_refit が版の名前を差し込んで docs/refits/2026-11-22-promote.sql にする（W8 の PR G）。
\set ON_ERROR_STOP on
\echo '1. 合成器 composite-v1-20261122 を active に（前の active の composite-v1-20261115 は retired になる）'
select now() at time zone 'Asia/Tokyo' as 時刻, public.promote_model_version('composite-v1-20261122', 'active');
\echo '2. B3 baseline-b3-v0-20261122（合成器の中と同じバイト列）を shadow に（W6-39：合成器と比べ続ける）'
select now() at time zone 'Asia/Tokyo' as 時刻, public.promote_model_version('baseline-b3-v0-20261122', 'shadow');
\echo '3. 登録簿'
select model_version, kind, status, to_char(promoted_at at time zone 'Asia/Tokyo', 'MM-DD HH24:MI:SS') as 昇格
  from public.model_versions
 where status in ('active', 'shadow')
    or model_version in ('composite-v1-20261115', 'composite-v1-20261122', 'baseline-b3-v0-20261122')
 order by status, model_version;
