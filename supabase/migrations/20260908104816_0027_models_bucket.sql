-- 0027 モデル成果物のバケット（W3 プラン §5.10、開発プラン §8.2）
--
-- CLAUDE.md §1 は Supabase Storage に「生 gzip JSON＝一次ソース、Parquet、**モデル**、
-- 予測ログ」を置くと書いているが、**モデルの置き場所を作っていなかった**。
-- 段 8 で成果物を上げようとして初めて分かった（W3 プラン §12 の 106）。
--
-- **`gbfs-parquet` に相乗りさせない。** あちらは `application/vnd.apache.parquet`
-- しか許していない（0017）。許可を広げれば通るが、**学習用アーカイブとモデルは
-- 寿命も作り直し方も違う**：Parquet は生 JSON から作り直せる派生物で、モデルは
-- 「その時点の学習結果」という別の資産である。混ぜると保持期間を別々に決められない。
--
-- パスは `baseline/{model_version}.json.gz`（W3 の段 8）。LightGBM を配り始めたら
-- `short/{model_version}/...` を並べる（開発プラン §8.2）。
--
-- `supabase db diff` は Storage バケットを検出しないため手書きで管理する
-- （W1 プラン §4.3 の 17）。0001・0015・0017 と同じ形にしてある。

do $$
begin
  if not exists (
    select 1 from information_schema.schemata where schema_name = 'storage'
  ) then
    raise notice 'storage スキーマが無いためバケット作成をスキップした';
    return;
  end if;

  insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
  values (
    'models',
    'models',
    false,               -- 非公開。読み書きはサービスロールのみ
    52428800,            -- 50 MiB。ベースラインの成果物は実測 3.2 MB
    -- gzip した JSON。LightGBM の成果物も gzip で置く
    array['application/gzip']
  )
  on conflict (id) do nothing;
end
$$;
