-- 0001 生データ用の Storage バケット（W1 プラン §6.2 / §11.5）
--
-- 生 gzip JSON はこのプロジェクトの一次ソース（開発プラン D-03）で、Postgres の
-- 配列スナップショットはここから再構築できる派生物である。したがってバケットは
-- スキーマ本体（PR A）より先に作り、PR 0 の収集を最短で始められるようにする。
--
-- `supabase db diff` は Storage バケットを検出しないため、手書きで管理する
-- （W1 プラン §4.3 の 17）。
--
-- `storage` スキーマの存在で分岐するのは、素の Postgres イメージでマイグレーションを
-- 流す環境（将来の軽量 CI）でも失敗しないようにするため。ローカルの `supabase start`
-- と本番では作成される。

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
    'gbfs-raw',
    'gbfs-raw',
    false,               -- 非公開。読み書きはサービスロールと S3 互換の資格情報のみ
    52428800,            -- 50 MiB。実測の最大は station_information の gzip 約 0.9 MB
    array['application/gzip']  -- contentType を明示しない実装を弾く（§4.3 の 9）
  )
  on conflict (id) do nothing;
end
$$;
