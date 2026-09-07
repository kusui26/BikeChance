-- 0017 学習用 Parquet のバケット（W2 プラン §5.6、PR D）
--
-- Postgres のスナップショットは 60 日で消える（保持期間の設計は開発プラン §5.2）。
-- 学習に使うのはそれより長い期間なので、**毎時 1 ファイルずつ、消えない置き場に
-- 畳んでいく**。生 gzip JSON が一次ソースであることは変わらず（開発プラン D-03）、
-- Parquet は生 JSON からも Postgres からも作り直せる派生物である。
--
-- パスは `{system}/date=YYYY-MM-DD/hour=HH/part.parquet`（UTC、W2 プラン §9.2）。
-- Hive 形式にしておくと、後で日付・時刻での絞り込みがそのまま効く。
--
-- `supabase db diff` は Storage バケットを検出しないため手書きで管理する
-- （W1 プラン §4.3 の 17）。0001・0015 と同じ形にしてある。

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
    'gbfs-parquet',
    'gbfs-parquet',
    false,               -- 非公開。読み書きはサービスロールのみ
    52428800,            -- 50 MiB。見込みは 1 時間 1 システムで 0.1〜1 MB
    -- IANA 登録済みの型。Content-Type を付けない実装を弾く（0001 の gzip と同じ方針）
    array['application/vnd.apache.parquet']
  )
  on conflict (id) do nothing;
end
$$;
