#!/usr/bin/env bash
#
# Vault に秘密を登録する（W1 プラン §6.7、§11.7）。
#
# pg_cron のウォッチドッグと通知が使う 2 つだけを入れる。
#   cron_secret        … Vercel の収集エンドポイントを叩くときの Authorization
#   alert_webhook_url  … 監視通知の送信先（未設定でも動く。alert_state には残る）
#
# **値をコマンドライン引数に載せない。** `ps` から見えてしまう。SQL 文は標準入力から渡し、
# 値はドル引用符（`$tag$...$tag$`）で囲む。タグは実行のたびに乱数で作るので、
# 値の中身に関係なくエスケープが要らない。
#
# （psql の `\getenv` を使えばもっと素直に書けるが、あれは psql 16 以降で、
#   この開発機の psql は 14 系のため使えない）
#
# 使い方:
#   ./scripts/setup-vault.sh [環境ファイル]        # 既定は .env（本番）
#   ./scripts/setup-vault.sh apps/web/.env.local  # ローカル
#
# 冪等：既にある名前は値を更新する（vault.update_secret）。

set -euo pipefail

ENV_FILE="${1:-.env}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "環境ファイルが見つかりません: ${ENV_FILE}" >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a; . "${ENV_FILE}"; set +a

: "${SUPABASE_DB_URL:?SUPABASE_DB_URL が未設定です}"
: "${CRON_SECRET:?CRON_SECRET が未設定です}"
WEBHOOK_URL="${ALERT_WEBHOOK_URL:-}"

# 値に含まれ得ないタグを作る。万一含まれていたら作り直す
new_tag() {
  local tag
  while :; do
    tag="q$(LC_ALL=C tr -dc 'a-z0-9' < /dev/urandom | head -c 12)"
    if [[ "$1" != *"\$$tag\$"* ]]; then printf '%s' "$tag"; return; fi
  done
}
TAG_SECRET="$(new_tag "${CRON_SECRET}")"
TAG_WEBHOOK="$(new_tag "${WEBHOOK_URL}")"

psql "${SUPABASE_DB_URL}" --quiet --no-psqlrc --set ON_ERROR_STOP=1 <<SQL
do \$outer\$
declare
  v_id uuid;
  v_secret text := \$${TAG_SECRET}\$${CRON_SECRET}\$${TAG_SECRET}\$;
begin
  select id into v_id from vault.secrets where name = 'cron_secret';
  if v_id is null then
    perform vault.create_secret(v_secret, 'cron_secret', 'ウォッチドッグが Vercel を叩くときの Authorization');
  else
    perform vault.update_secret(v_id, v_secret);
  end if;
end
\$outer\$;

do \$outer\$
declare
  v_id uuid;
  v_url text := \$${TAG_WEBHOOK}\$${WEBHOOK_URL}\$${TAG_WEBHOOK}\$;
begin
  if v_url = '' then
    raise notice 'ALERT_WEBHOOK_URL が空のため通知先は登録しません（alert_state には記録されます）';
    return;
  end if;
  select id into v_id from vault.secrets where name = 'alert_webhook_url';
  if v_id is null then
    perform vault.create_secret(v_url, 'alert_webhook_url', '監視通知の送信先');
  else
    perform vault.update_secret(v_id, v_url);
  end if;
end
\$outer\$;

-- 登録できたことだけを確認する（値は表示しない）
select name, length(decrypted_secret) || ' 文字' as 長さ, created_at
  from vault.decrypted_secrets
 where name in ('cron_secret', 'alert_webhook_url')
 order by name;
SQL

echo "完了。値は表示していません。"
