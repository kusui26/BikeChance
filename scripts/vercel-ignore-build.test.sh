#!/usr/bin/env bash
#
# `scripts/vercel-ignore-build.sh` の検査（CI の「Ignored Build Step の判定」で走る）。
#
# 一時ディレクトリに git のリポジトリを作ってコミットを積み、`VERCEL_GIT_PREVIOUS_SHA` を
# 渡して**終了コード**を見る：飛ばす ＝ 0、ビルドする ＝ 1。**2 以上は誤り**——Vercel は 0・1 以外を
# 返したデプロイを失敗にする（2026-10-10 に確かめた）。
# **厚く見るのは「飛ばしてはいけないのに飛ばす」側**——コードの変更が本番に出なくなる。
#
#   bash scripts/vercel-ignore-build.test.sh

set -euo pipefail

SCRIPT="$(cd "$(dirname "$0")" && pwd)/vercel-ignore-build.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
REPO="$WORK/repo"
FAILED=0

# 手元の git の設定に左右されない（Vercel のクローンも素の設定で動く）。上の階のリポジトリも探さない
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_CEILING_DIRECTORIES="$WORK"
export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid
export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid

# ── 道具 ──────────────────────────────────────────────────────
# 置いたもの（追加・変更・削除・改名）をコミットし、その SHA を出す
commit_staged() {
  git -C "$REPO" commit -qm change
  git -C "$REPO" rev-parse HEAD
}

# ファイルの中身を変えて（無ければ作って）コミットする
touch_files() {
  local path
  for path in "$@"; do
    mkdir -p "$(dirname "$REPO/$path")"
    echo "$path $RANDOM" >>"$REPO/$path"
    git -C "$REPO" add -- "$path"
  done
  commit_staged
}

remove_file() {
  git -C "$REPO" rm -q -- "$1"
  commit_staged
}

move_file() {
  mkdir -p "$(dirname "$REPO/$2")"
  git -C "$REPO" mv -- "$1" "$2"
  commit_staged
}

# どの場面も、同じ土台のコミットから枝分かれして始める
from_base() {
  git -C "$REPO" checkout -q --detach "$BASE"
}

# expect <skip|build> <場面> <前回の SHA> [起動する場所。既定はリポジトリの最上位]
# EXPECT_PATH を置くと、その PATH で起動する（偽の git を前に挟む場面）
# EXPECT_ENV を置くと、それを VERCEL_ENV に渡す（既定は空。枝の最初のプレビューの場面で使う）
expect() {
  local want="$1" name="$2" previous="$3" dir="${4:-$REPO}" code=0 got
  (cd "$dir" && PATH="${EXPECT_PATH:-$PATH}" VERCEL_GIT_PREVIOUS_SHA="$previous" \
    VERCEL_ENV="${EXPECT_ENV:-}" bash "$SCRIPT" >"$WORK/out" 2>&1) || code=$?
  case "$code" in 0) got=skip ;; 1) got=build ;; *) got="落ちた（終了コード ${code}）" ;; esac
  if [[ "$got" == "$want" ]]; then
    echo "ok    $name"
  else
    echo "FAIL  ${name}：期待 ${want}、実際 ${got}"
    sed 's/^/        /' "$WORK/out"
    FAILED=$((FAILED + 1))
  fi
}

# ── 場面 ──────────────────────────────────────────────────────
documents_only_are_skipped() {
  echo "── 飛ばす（前回からの変更が文書だけ）"
  from_base && touch_files docs/plan.md >/dev/null
  expect skip "docs/ の下" "$BASE"
  from_base && touch_files apps/web/AGENTS.md fixtures/gbfs/README.md >/dev/null
  expect skip "どこにあっても *.md" "$BASE"
  from_base && touch_files .claude/CLAUDE.md .claude/settings.json >/dev/null
  expect skip ".claude/ の下" "$BASE"
  from_base && remove_file docs/plan.md >/dev/null
  expect skip "文書を消す" "$BASE"
  from_base && touch_files "docs/計画.md" >/dev/null
  expect skip "日本語の名前の文書" "$BASE"
  from_base && touch_files docs/plan.md >/dev/null && touch_files .claude/CLAUDE.md >/dev/null
  expect skip "文書だけのコミットを 2 つ、1 回で push" "$BASE"
}

ios_and_ci_are_skipped() {
  echo "── 飛ばす（iOS と CI の設定も Vercel のビルドは読まない。W8-08）"
  from_base && touch_files apps/ios/BikeChance/Map/MapScreen.swift >/dev/null
  expect skip "apps/ios/ の下" "$BASE"
  from_base && touch_files apps/ios/BikeChance.xcodeproj/project.pbxproj >/dev/null
  expect skip "Xcode のプロジェクト" "$BASE"
  from_base && touch_files .github/workflows/ios.yml >/dev/null
  expect skip ".github/ の下" "$BASE"
  from_base && touch_files apps/ios/BikeChance/App.swift docs/plan.md .github/workflows/ci.yml >/dev/null
  expect skip "iOS・文書・CI の設定が同じコミット" "$BASE"
  from_base && touch_files apps/ios/BikeChance/App.swift apps/web/app/page.tsx >/dev/null
  expect build "iOS と web のコードが同じコミット" "$BASE"
  from_base && touch_files .github/workflows/ci.yml apps/ml/main.py >/dev/null
  expect build "CI の設定と ml のコードが同じコミット" "$BASE"
}

code_is_built() {
  echo "── ビルドする（コードがある）"
  from_base && touch_files apps/web/app/page.tsx >/dev/null
  expect build "web のコード" "$BASE"
  from_base && touch_files apps/ml/main.py >/dev/null
  expect build "ml のコード" "$BASE"
  from_base && touch_files vercel.json >/dev/null
  expect build "vercel.json" "$BASE"
  from_base && touch_files docs/plan.md apps/ml/main.py >/dev/null
  expect build "文書とコードが同じコミット" "$BASE"
  from_base && touch_files docs/plan.md vercel.json >/dev/null
  expect build "文書が先に並び、コードが後（1 つ目だけで決めない）" "$BASE"
  from_base && touch_files apps/ml/main.py >/dev/null && touch_files docs/plan.md >/dev/null
  expect build "コード → 文書の 2 コミットを 1 回で push（HEAD^ と比べると見落とす）" "$BASE"
  from_base && move_file apps/web/app/page.tsx docs/page.tsx >/dev/null
  expect build "コードを docs/ へ動かす（改名を追わない）" "$BASE"
  from_base && remove_file apps/ml/main.py >/dev/null
  expect build "コードを消す" "$BASE"
}

lookalikes_are_built() {
  echo "── ビルドする（文書に似ているが文書でない）"
  local path
  for path in docsx/a.txt apps/web/docs/a.ts apps/web/.claude/a.ts apps/web/notes.md.ts \
    README.MD "apps/web/app/日本.ts" 'docs/a"b.md' apps/iosx/a.swift apps/web/apps/ios/a.ts \
    apps/ios .githubx/a.yml apps/web/.github/a.yml; do
    from_base && touch_files "$path" >/dev/null
    expect build "$path" "$BASE"
  done
}

unusable_previous_is_built() {
  echo "── ビルドする（前回が使えない）"
  from_base && touch_files docs/plan.md >/dev/null
  expect build "前回が空（枝の最初のデプロイ）" ""
  expect build "前回が SHA の形でない（オプション）" "--output=$WORK/x"
  expect build "前回が SHA の形でない（枝の名前）" "main"
  expect build "前回がリポジトリに無い" "0123456789abcdef0123456789abcdef01234567"
  expect build "前回が HEAD と同じ（Redeploy）" "$(git -C "$REPO" rev-parse HEAD)"
  mkdir -p "$WORK/plain"
  expect build "git のリポジトリの外" "$BASE" "$WORK/plain"
}

services_decide_alike() {
  echo "── サービスの root から起動しても同じ判定"
  from_base && touch_files docs/plan.md >/dev/null
  expect skip "文書だけ（apps/web から）" "$BASE" "$REPO/apps/web"
  expect skip "文書だけ（apps/ml から）" "$BASE" "$REPO/apps/ml"
  from_base && touch_files apps/ml/main.py >/dev/null
  expect build "ml のコード（apps/web から）" "$BASE" "$REPO/apps/web"
  expect build "ml のコード（apps/ml から）" "$BASE" "$REPO/apps/ml"
  from_base && touch_files apps/ml/main.py apps/web/README.md >/dev/null
  git -C "$REPO" config diff.relative true
  expect build "diff.relative が入っていても、ほかのサービスのコードを見る" "$BASE" "$REPO/apps/web"
  git -C "$REPO" config --unset diff.relative
}

merges_into_main() {
  echo "── main へのマージ（merge commit。前回は main で最後にデプロイしたコミット）"
  local deployed
  from_base && deployed="$(touch_files apps/ml/main.py)"
  git -C "$REPO" checkout -q -b docs-pr "$BASE" && touch_files docs/plan.md >/dev/null
  git -C "$REPO" checkout -q -b code-pr "$BASE" && touch_files apps/web/app/page.tsx >/dev/null
  git -C "$REPO" checkout -q --detach "$deployed" && git -C "$REPO" merge -q --no-ff -m m docs-pr
  expect skip "文書だけの PR" "$deployed"
  git -C "$REPO" checkout -q --detach "$deployed" && git -C "$REPO" merge -q --no-ff -m m code-pr
  expect build "コードの PR" "$deployed"
}

shallow_clone_is_handled() {
  echo "── 浅いクローン（Vercel は深さ 10）"
  local i middle=""
  git -C "$REPO" checkout -q -b long "$BASE"
  for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if [[ "$i" == 8 ]]; then middle="$(touch_files docs/plan.md)"; else touch_files docs/plan.md >/dev/null; fi
  done
  git clone -q --depth=10 --branch long "file://$REPO" "$WORK/shallow"
  expect build "前回が深さ 10 の外" "$BASE" "$WORK/shallow"
  expect skip "前回が深さ 10 の内（文書だけ）" "$middle" "$WORK/shallow"
}

# 枝の最初のプレビュー（前回が無い）は、`main` を浅く取ってきて分岐点と比べる（W8-08）。
# `origin` は手元のリポジトリ（`file://`）。`main` は土台のコミットを指している
first_preview_compares_with_main() {
  echo "── 枝の最初のプレビュー（前回が無い）：main との分岐点と比べる"
  local clone="$WORK/branch"
  rm -rf "$clone"
  git clone -q --depth=10 --branch main "file://$REPO" "$clone"
  git -C "$clone" checkout -q -b docs-branch origin/main
  touch_files_in "$clone" docs/plan.md
  EXPECT_ENV=preview expect skip "文書だけの枝の最初のプレビュー" "" "$clone"
  EXPECT_ENV=production expect build "本番で前回が無い（プロジェクトの最初のデプロイ）" "" "$clone"
  expect build "VERCEL_ENV が無い" "" "$clone"
  git -C "$clone" checkout -q -b code-branch origin/main
  touch_files_in "$clone" apps/web/app/page.tsx
  EXPECT_ENV=preview expect build "コードの枝の最初のプレビュー" "" "$clone"
  git -C "$clone" checkout -q -b ios-branch origin/main
  touch_files_in "$clone" apps/ios/BikeChance/App.swift
  EXPECT_ENV=preview expect skip "iOS だけの枝の最初のプレビュー" "" "$clone"
  # 枝を切った後に main がコードの変更で進んでも、比べるのは分岐点（main の先頭ではない）
  git -C "$REPO" checkout -q main && touch_files apps/web/app/later.tsx >/dev/null
  EXPECT_ENV=preview expect skip "main が先に進んでも、枝の変更（iOS だけ）だけを見る" "" "$clone"
  git -C "$REPO" checkout -q --detach "$BASE"
  git -C "$clone" remote remove origin
  EXPECT_ENV=preview expect build "origin が無い（main を取ってこられない）" "" "$clone"
  git -C "$clone" remote add origin "file://$REPO"
  git -C "$clone" checkout -q --orphan unrelated && git -C "$clone" rm -rqf . &&
    touch_files_in "$clone" docs/other.md
  EXPECT_ENV=preview expect build "main と分岐点の無い枝" "" "$clone"
}

# 別のクローンでファイルを変えてコミットする（`touch_files` と同じ。リポジトリを選べる）
touch_files_in() {
  local repo="$1" path
  shift
  for path in "$@"; do
    mkdir -p "$(dirname "$repo/$path")"
    echo "$path $RANDOM" >>"$repo/$path"
    git -C "$repo" add -- "$path"
  done
  git -C "$repo" commit -qm change
}

# git が途中で想定外の終了コードを返す偽物を、PATH の前に挟む
fake_git() {
  local dir="$WORK/fakebin" real
  real="$(command -v git)"
  mkdir -p "$dir"
  # 偽物の中身には `$@` と `$a` をそのまま書く（ここで展開しない）
  # shellcheck disable=SC2016
  printf '#!/usr/bin/env bash\nfor a in "$@"; do [[ "$a" == %s ]] && exit %s; done\nexec "%s" "$@"\n' \
    "$1" "$2" "$real" >"$dir/git"
  chmod +x "$dir/git"
  echo "$dir"
}

unexpected_failures_build() {
  echo "── 想定外の失敗も 1（ビルド）で終わる（0・1 以外を返さない）"
  from_base && touch_files docs/plan.md >/dev/null
  EXPECT_PATH="$(fake_git --show-prefix 128):$PATH"
  expect build "git が途中で 128 を返す（文書だけの変更でも）" "$BASE"
  rm -rf "$WORK/fakebin"
  EXPECT_PATH="$(fake_git --name-only 3):$PATH"
  expect build "git diff が 3 を返す" "$BASE"
  rm -rf "$WORK/fakebin"
  unset EXPECT_PATH
}

# ── 実行 ──────────────────────────────────────────────────────
git init -q -b main "$REPO"
BASE="$(touch_files apps/web/app/page.tsx apps/web/AGENTS.md apps/ml/main.py docs/plan.md \
  .claude/CLAUDE.md vercel.json)"
documents_only_are_skipped
ios_and_ci_are_skipped
code_is_built
lookalikes_are_built
unusable_previous_is_built
services_decide_alike
merges_into_main
shallow_clone_is_handled
first_preview_compares_with_main
unexpected_failures_build

if ((FAILED > 0)); then
  echo "${FAILED} 件が期待と違った"
  exit 1
fi
echo "すべて期待どおり"
