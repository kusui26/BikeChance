#!/usr/bin/env bash
#
# Vercel の Ignored Build Step（W6 プランの PR M、W6-22）。**両方のサービス**（`web`・`ml`）の
# `ignoreCommand` から呼ぶ（`vercel.json`）。
#
# Vercel の決まり：**終了コード 0 ならこのデプロイを飛ばし**（状態は CANCELED）、
# **1 以上ならビルドする**。本番（main）にも効く。飛ばした回は、収集器も Cron も
# 前のデプロイのまま動き続ける。
#
# **飛ばすのは、前回うまくいったデプロイからの変更が全部「文書」だと言い切れるときだけ。
# 迷ったらビルドする。**
#
#   文書 ＝ `docs/` の下・`.claude/` の下・`*.md`（どの場所でも）
#
#   * **比べる相手は `VERCEL_GIT_PREVIOUS_SHA`**（同じ枝で最後に成功したデプロイのコミット）。
#     `HEAD^` と比べると、2 つ以上のコミットを 1 回で push したとき（main へのマージが
#     続いたときも）、前のコミットのコードを見落とす
#   * **改名を追わない**（`--no-renames`）。コードを `docs/` へ動かした変更は、
#     コードが消えた変更として数える
#   * **ビルドする**：前回が無い（枝の最初のデプロイ）・前回がクローン（深さ 10）に無い・
#     差分が取れない・**差分が空**（同じコミットの Redeploy。環境変数を変えた後の
#     再デプロイを飛ばさない）・文書でないファイルが 1 つでもある
#
# **2 つのサービスは必ず同じ判定を出す**（片方だけを飛ばした形を作らない）。入力を
# 環境変数と git だけにし、動く場所（サービスの root）に依存させない——比べるパスは
# リポジトリの最上位からの相対にそろえる。
#
# **ビルドが読むファイルを、文書の場所に置かない。** 置くと、その変更が配られない。
#
# 検査：`scripts/vercel-ignore-build.test.sh`（CI の「Ignored Build Step の判定」）。

set -euo pipefail

build() {
  echo "ビルドする：$1"
  exit 1
}

skip() {
  echo "飛ばす：$1"
  exit 0
}

# パスはリポジトリの最上位からの相対（`git diff --name-only` の出力）
is_document() {
  case "$1" in
    docs/* | .claude/* | *.md) return 0 ;;
    *) return 1 ;;
  esac
}

# 前回のコミットから HEAD までに変わったファイル（1 行に 1 つ）。日本語の名前もそのまま出す
changed_files() {
  git -c core.quotePath=false diff --name-only --no-renames "$1" HEAD --
}

decide() {
  local base="$1" changed path
  [[ -n "$base" ]] || build "前回成功したデプロイが無い（この枝の最初のデプロイ）"
  [[ "$base" =~ ^[0-9a-f]{7,40}$ ]] || build "前回のコミットが SHA の形でない"
  git cat-file -e "${base}^{commit}" 2>/dev/null || build "前回のコミット ${base:0:7} がクローンに無い"
  changed="$(changed_files "$base")" || build "前回 ${base:0:7} との差分を取れない"
  [[ -n "$changed" ]] || build "前回 ${base:0:7} から変更が無い（同じコミットの Redeploy）"
  while IFS= read -r path; do
    is_document "$path" || build "文書でない変更がある：${path}"
  done <<<"$changed"
  skip "前回 ${base:0:7} からの変更 $(grep -c '' <<<"$changed") 件がすべて文書"
}

top="$(git rev-parse --show-toplevel 2>/dev/null)" || build "git のリポジトリの中で動いていない"
where="${PWD#"$top"}"
echo "Ignored Build Step（${where:-/} で起動、HEAD $(git rev-parse --short HEAD)）"
cd "$top"
decide "${VERCEL_GIT_PREVIOUS_SHA:-}"
