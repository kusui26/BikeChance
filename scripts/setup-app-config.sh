#!/usr/bin/env bash
#
# 秘密でない設定値を app_config に入れる（W3 プラン §5.5）。
#
# 秘密は Vault（`setup-vault.sh`）。ここに入れるのは**環境ごとに違うが秘密ではない**値で、
# いまは 2 つある。
#
#   project_base_url    … ウォッチドッグが叩く Vercel の URL（利用者が叩く先と同じ）
#   functions_base_url  … バックアップ収集器を起こす Edge Function の URL
#
# **マイグレーションに値を書かない。** `project_base_url` は 0004 に直書きしてあるが、
# あれは公開されている Vercel のドメインである。Supabase のプロジェクト URL は
# どこにも公開しておらず、秘密ではないにせよリポジトリに置く理由が無い。
#
# 使い方:
#   ./scripts/setup-app-config.sh [環境ファイル]        # 既定は .env（本番）
#   ./scripts/setup-app-config.sh apps/web/.env.local  # ローカル
#
# 冪等：同じ値なら何度流しても変わらない。

set -euo pipefail

ENV_FILE="${1:-.env}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "環境ファイルが見つかりません: ${ENV_FILE}" >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a; . "${ENV_FILE}"; set +a

: "${SUPABASE_DB_URL:?SUPABASE_DB_URL が未設定です}"
: "${SUPABASE_URL:?SUPABASE_URL が未設定です}"

# Edge Function は <SUPABASE_URL>/functions/v1/<name> にある。末尾のスラッシュは落とす
FUNCTIONS_BASE_URL="${SUPABASE_URL%/}/functions/v1"

psql "${SUPABASE_DB_URL}" --quiet --no-psqlrc --set ON_ERROR_STOP=1 \
  --set functions_base_url="${FUNCTIONS_BASE_URL}" <<'SQL'
insert into public.app_config (key, value)
values ('functions_base_url', :'functions_base_url')
on conflict (key) do update set value = excluded.value, updated_at = now();

-- 値は表示しない（秘密ではないが、ログに URL を積む理由も無い）
select key, length(value) || ' 文字' as 長さ,
       to_char(updated_at at time zone 'Asia/Tokyo', 'YYYY-MM-DD HH24:MI') as 更新
  from public.app_config
 where key in ('project_base_url', 'functions_base_url')
 order by key;
SQL

echo "完了。値は表示していません。"
