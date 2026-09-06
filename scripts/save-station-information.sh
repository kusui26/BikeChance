#!/usr/bin/env bash
#
# station_information のベースラインを 1 回だけ保存する（W1 プラン §7 の段 0、§5.1 の 31）。
#
# なぜ必要か：`station_information` を取得するものは PR F（日次同期）まで存在しない。
# `station_status` と違って生アーカイブも残らないため、PR F が動き出すまでに起きた
# ポート属性の変化（新設・廃止・容量変更）は永久に失われる。段 0 で 1 回保存しておけば、
# PR F の初回実行でこれを取り込み、属性履歴の起点を W1 の初日にできる。
#
# なぜ公開エンドポイントを使うか：認証付きと応答がバイト単位で一致することを実測で
# 確認しており（開発プラン §15 の D-04）、トークンを使わなければ URL に載る心配も無い。
# 常時の収集と違い ODPT から見た識別性が要らない 1 回限りの取得なので、
# 漏洩面がゼロの経路を選ぶ（W1-21 の趣旨）。
#
# 使い方：
#   本番へ  : ./scripts/save-station-information.sh .env
#   ローカルへ: ./scripts/save-station-information.sh apps/web/.env.local
#
# 冪等性：パスに取得時刻を含めるため（§11.5）、素直に再実行すると毎回別のオブジェクトが
# できてしまう。そこで「同じ UTC 日に同じシステムの station_information が既にあれば
# 取得せずスキップする」ようにしてある。安全に何度でも実行できる。

set -euo pipefail

ENV_FILE="${1:-.env}"
BUCKET="gbfs-raw"
FEED="station_information"
PUBLIC_BASE="https://api-public.odpt.org/api/v4/gbfs"
SYSTEMS=("hellocycling" "docomo-cycle")

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "環境ファイルが見つかりません: ${ENV_FILE}" >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a; . "${ENV_FILE}"; set +a

: "${SUPABASE_URL:?SUPABASE_URL が未設定です}"
: "${SUPABASE_SECRET_KEY:?SUPABASE_SECRET_KEY が未設定です}"
: "${CONTACT_EMAIL:=unknown@example.com}"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

TODAY_PATH="$(date -u +%Y/%m/%d)"

# 同じ UTC 日に、そのシステムの station_information が既に保存されているか
already_saved() {
  local system="$1"
  curl -sS -X POST --max-time 30 \
    -H "apikey: ${SUPABASE_SECRET_KEY}" \
    -H "Authorization: Bearer ${SUPABASE_SECRET_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"prefix\":\"${system}/${TODAY_PATH}/\",\"limit\":100,\"search\":\"${FEED}_\"}" \
    "${SUPABASE_URL}/storage/v1/object/list/${BUCKET}" \
  | python3 -c 'import json,sys; print(len([o for o in json.load(sys.stdin) if o.get("name","").startswith("station_information_")]))'
}

for SYSTEM in "${SYSTEMS[@]}"; do
  RAW="${WORK_DIR}/${SYSTEM}.json"

  # 0. 今日すでに保存済みなら ODPT を叩かずにスキップする
  EXISTING="$(already_saved "${SYSTEM}")"
  if [[ "${EXISTING}" != "0" ]]; then
    printf '%-14s %-9s 今日 (UTC %s) は保存済み（%s 件）\n' \
      "${SYSTEM}" "skipped" "${TODAY_PATH}" "${EXISTING}"
    continue
  fi

  # 1. 取得（公開エンドポイント。トークンを使わない）
  HTTP_STATUS="$(curl -sS -A "BikeChance/0.1 (+${CONTACT_EMAIL})" --max-time 30 \
    -o "${RAW}" -w '%{http_code}' "${PUBLIC_BASE}/${SYSTEM}/${FEED}.json")"
  if [[ "${HTTP_STATUS}" != "200" ]]; then
    echo "${SYSTEM}: 取得に失敗しました (HTTP ${HTTP_STATUS})" >&2
    exit 1
  fi

  # 2. パス組み立て。station_information は取得時刻を使う（§11.5）。日付は UTC
  FETCHED_AT_EPOCH="$(date -u +%s)"
  DATE_PATH="$(date -u -r "${FETCHED_AT_EPOCH}" +%Y/%m/%d 2>/dev/null \
    || date -u -d "@${FETCHED_AT_EPOCH}" +%Y/%m/%d)"
  OBJECT_PATH="${SYSTEM}/${DATE_PATH}/${FEED}_${FETCHED_AT_EPOCH}.json.gz"

  # 3. 受信したバイト列をそのまま gzip する（再直列化しない。§5 の 14）
  gzip -c "${RAW}" > "${RAW}.gz"

  # 4. Storage へ。upsert しない（同一パスは 409 で正常系）
  UPLOAD_STATUS="$(curl -sS -X POST --max-time 120 \
    -H "apikey: ${SUPABASE_SECRET_KEY}" \
    -H "Authorization: Bearer ${SUPABASE_SECRET_KEY}" \
    -H "Content-Type: application/gzip" \
    -H "x-upsert: false" \
    --data-binary "@${RAW}.gz" \
    -o /dev/null -w '%{http_code}' \
    "${SUPABASE_URL}/storage/v1/object/${BUCKET}/${OBJECT_PATH}")"

  RAW_BYTES="$(wc -c < "${RAW}" | tr -d ' ')"
  GZ_BYTES="$(wc -c < "${RAW}.gz" | tr -d ' ')"
  STATION_COUNT="$(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))["data"]["stations"]))' "${RAW}")"

  case "${UPLOAD_STATUS}" in
    200|201) RESULT="saved" ;;
    409)     RESULT="duplicate" ;;
    *)       echo "${SYSTEM}: 保存に失敗しました (HTTP ${UPLOAD_STATUS})" >&2; exit 1 ;;
  esac

  printf '%-14s %-9s stations=%-6s raw=%-9s gzip=%-8s %s\n' \
    "${SYSTEM}" "${RESULT}" "${STATION_COUNT}" "${RAW_BYTES}" "${GZ_BYTES}" "${OBJECT_PATH}"
done

echo "完了。PR F の初回同期でこれらを取り込むと、属性履歴の起点が今日になる。"
