-- ────────────────────────────────────────────────────────────────
-- 0055 — 合成器の種類 `composite`（W6 の PR H、W6-08、開発プランの D-33）
-- ────────────────────────────────────────────────────────────────
-- v1 を配る形は**合成器**である：セル（system × ターゲット × 水平 × 台数バケツ）ごとに、
-- 門を越えたセルは LightGBM、ほかは B3 を配る。成果物は自己完結（B3・森・門の表を
-- 1 つのファイルに持つ）なので、登録簿には **3 つ目の読み方**として載る。
--
-- **制約を広げるだけ**で、いまの行は変わらない（`baseline` と `lightgbm` はそのまま通る）。
-- 鮮度の見張り（0048）は `max(train_days)` を見るので、合成器の `train_days` を B3 と
-- LightGBM の学習日の和集合にすれば、B3 の最終学習日で見張れる（W6-08）。
--
-- **昇格の決まりは変えない**（契約 39）。合成器も `register_model_version` で candidate として
-- 登録し、所有者が `promote_model_version` で上げる。
-- ────────────────────────────────────────────────────────────────

alter table public.model_versions drop constraint model_versions_kind;
alter table public.model_versions
  add constraint model_versions_kind check (kind in ('baseline', 'lightgbm', 'composite'));

comment on column public.model_versions.kind is
  '成果物の読み方。baseline は B1〜B3 の表、lightgbm は木の構造（単体では配らない。W6 の契約 33）、composite は B3・森・門の表を 1 つに持つ合成器（W6-08）。';
