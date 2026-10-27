#!/usr/bin/env bash
#
# Vercel の Ignored Build Step（W6 プランの PR M、W6-22）。**プロジェクトの設定**
# （Settings → Build and Deployment → Ignored Build Step の Command）に、次の 1 行で置く：
#
#   bash "$(git rev-parse --show-toplevel)/scripts/vercel-ignore-build.sh" || exit 1
#
# （`bash` で始まるので、画面の Behavior の表示は自動で「Run my Bash script」になる。
# 走るのは Command の文字列そのもの）
#
# **Vercel が受ける終了コードは 0 と 1 だけ**：0 ならこのデプロイを取り消し（状態は CANCELED）、
# 1 ならビルドする。**それ以外はデプロイを失敗にする**（2026-10-10、このスクリプトの無い枝で
# `bash` が 127 を返し、プレビューが失敗した）。末尾の `|| exit 1` は、このスクリプトが
# まだ無い枝（マージ前の main も）と想定外の失敗を 1 にそろえる。スクリプトの中でも、
# 想定外の失敗は ERR の trap で 1 にする（呼び出し側が `|| exit 1` を落としても失敗にしない）。
#
# 本番（main）にも効く。取り消した回は、収集器も Cron も前のデプロイのまま動き続ける。
#
# **`vercel.json` のサービスごとの `ignoreCommand` には置かない。** この Services の
# プロジェクトでは走らなかった（2026-10-10 にプレビューで確かめた：文書だけのコミットも、
# `exit 0` を置いたサービスも、ビルドログに何も出ずに最後までビルドされた）。
#
# **飛ばすのは、前回うまくいったデプロイからの変更が全部「Vercel のビルドが読まない場所」だと
# 言い切れるときだけ。迷ったらビルドする。**
#
#   読まない場所 ＝ 文書（`docs/` の下・`.claude/` の下・`*.md`。どの場所でも）
#                  ・iOS（`apps/ios/` の下。`package.json` が無いので pnpm のワークスペースに入らない）
#                  ・CI の設定（`.github/` の下）
#   （D-42。iOS と CI の設定は 2026-10-27 に足した。W8 プランの W8-08）
#
#   * **比べる相手は `VERCEL_GIT_PREVIOUS_SHA`**（同じ枝で最後に成功したデプロイのコミット）。
#     `HEAD^` と比べると、2 つ以上のコミットを 1 回で push したとき（main へのマージが
#     続いたときも）、前のコミットのコードを見落とす
#   * **改名を追わない**（`--no-renames`）。コードを `docs/` へ動かした変更は、
#     コードが消えた変更として数える
#   * **枝の最初のプレビュー**（前回が無い）は、**`main` を浅く取ってきて分岐点と比べる**（W8-08）。
#     取れなければ（取ってくる口が無い・分岐点が深さの外）今までどおりビルドする。**本番で前回が
#     無いのはプロジェクトの最初のデプロイだけ**なので、この道はプレビューに限る
#   * **ビルドする**：前回が無く分岐点も取れない・前回がクローン（深さ 10）に無い・
#     差分が取れない・**差分が空**（同じコミットの Redeploy。環境変数を変えた後の
#     再デプロイを飛ばさない）・読まない場所の外のファイルが 1 つでもある
#
# 入力は環境変数と git だけにし、**動く場所に依存させない**——比べるパスはリポジトリの
# 最上位からの相対にそろえる（どのディレクトリから呼ばれても同じ判定になる）。
#
# **ビルドが読むファイルを、読まない場所（文書・`apps/ios/`・`.github/`）に置かない。** 置くと、
# その変更が配られない。
#
# 検査：`scripts/vercel-ignore-build.test.sh`（CI の「Ignored Build Step の判定」）。

set -Eeuo pipefail
# 想定外の失敗（git の 128 など）も 1（ビルド）で終える。-E で関数とコマンド置換にも効かせる
trap 'exit 1' ERR

build() {
  echo "ビルドする：$1"
  exit 1
}

skip() {
  echo "飛ばす：$1"
  exit 0
}

# パスはリポジトリの最上位からの相対（`git diff --name-only` の出力）
is_outside_build() {
  case "$1" in
    docs/* | .claude/* | *.md | apps/ios/* | .github/*) return 0 ;;
    *) return 1 ;;
  esac
}

# 枝の最初のプレビューで `main` から取ってくる深さ。PR の枝は数コミットなので、分岐点が入る
MAIN_FETCH_DEPTH=50

# **枝の最初のプレビュー**の比べる相手：`main` との分岐点。取れなければ何も出さずに失敗する
# （呼ぶ側がビルドにする）。失敗しうる命令はすべて `||` の中に置く（ERR の trap に掛けない）
first_preview_base() {
  local base
  [[ "${VERCEL_ENV:-}" == "preview" ]] || return 1
  git fetch --quiet --no-tags --depth="$MAIN_FETCH_DEPTH" origin \
    "+refs/heads/main:refs/remotes/origin/main" 2>/dev/null || return 1
  base="$(git merge-base HEAD refs/remotes/origin/main 2>/dev/null)" || return 1
  echo "$base"
}

# 前回のコミットから HEAD までに変わったファイル（1 行に 1 つ）。日本語の名前もそのまま出す
changed_files() {
  git -c core.quotePath=false diff --name-only --no-renames "$1" HEAD --
}

decide() {
  local base="$1" changed path
  [[ -n "$base" ]] || build "比べる相手が無い"
  [[ "$base" =~ ^[0-9a-f]{7,40}$ ]] || build "前回のコミットが SHA の形でない"
  git cat-file -e "${base}^{commit}" 2>/dev/null || build "前回のコミット ${base:0:7} がクローンに無い"
  changed="$(changed_files "$base")" || build "前回 ${base:0:7} との差分を取れない"
  [[ -n "$changed" ]] || build "前回 ${base:0:7} から変更が無い（同じコミットの Redeploy）"
  while IFS= read -r path; do
    is_outside_build "$path" || build "ビルドが読む場所の変更がある：${path}"
  done <<<"$changed"
  skip "${base:0:7} からの変更 $(grep -c '' <<<"$changed") 件がすべて、ビルドが読まない場所"
}

top="$(git rev-parse --show-toplevel 2>/dev/null)" || build "git のリポジトリの中で動いていない"
# 起動した場所は最上位からの相対で出す（`$PWD` から最上位を引くと、シンボリックリンクを挟んだときに外れる）
where="$(git rev-parse --show-prefix)"
echo "Ignored Build Step（起動した場所 ./${where}、HEAD $(git rev-parse --short HEAD)）"
cd "$top"
previous="${VERCEL_GIT_PREVIOUS_SHA:-}"
if [[ -z "$previous" ]]; then
  previous="$(first_preview_base)" ||
    build "前回成功したデプロイが無く（この枝の最初のデプロイ）、main との分岐点も取れない"
  echo "枝の最初のプレビュー：main との分岐点 ${previous:0:7} と比べる"
fi
decide "$previous"
