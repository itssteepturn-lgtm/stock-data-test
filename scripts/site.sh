#!/usr/bin/env bash
# 网页站点发布脚本。数据不再提交到 main 分支，而是放在单独的 gh-pages 分支，
# 每次发布都把 gh-pages 重置成「只有一个提交」(force push)，所以仓库历史永远不会越长越大。
#   prepare : 把线上最新的 gh-pages 拉到 site/（里面是上一次的数据），首次运行则建空目录
#   publish : 把 index.html 和 site/ 里的数据重新发布到 gh-pages（没有变化就不发布）
#   page    : 只更新 gh-pages 上的 index.html（改了网页代码时用）
set -euo pipefail

BRANCH="${PAGES_BRANCH:-gh-pages}"
CODE_DIR="${CODE_DIR:-code}"
SITE_DIR="${SITE_DIR:-site}"
REPO_URL="${REPO_URL:-https://x-access-token:${GITHUB_TOKEN:-}@github.com/${GITHUB_REPOSITORY:-}.git}"

branch_state() {  # 0=存在 2=不存在 其他=网络等错误
  set +e
  git ls-remote --exit-code --heads "$REPO_URL" "$BRANCH" >/dev/null 2>&1
  local rc=$?
  set -e
  echo "$rc"
}

cmd="${1:-}"
case "$cmd" in
  prepare)
    rm -rf "$SITE_DIR"
    rc=$(branch_state)
    if [ "$rc" = "0" ]; then
      git clone --quiet --depth 1 --branch "$BRANCH" "$REPO_URL" "$SITE_DIR"
      echo "已载入 $BRANCH 上的现有数据"
    elif [ "$rc" = "2" ]; then
      mkdir -p "$SITE_DIR"
      echo "首次运行：$BRANCH 分支还不存在，从空数据开始"
    else
      echo "无法访问仓库（ls-remote 返回 $rc），为避免覆盖数据，本次终止" >&2
      exit 1
    fi
    ;;

  publish)
    [ -d "$SITE_DIR" ] || { echo "没有 $SITE_DIR 目录，不发布" >&2; exit 1; }
    cd "$SITE_DIR"
    cp "../$CODE_DIR/index.html" index.html
    touch .nojekyll
    if [ -d .git ]; then
      # 注意：这里不能用 "| head -n 1"。改动文件有几千个时，head 提前退出会让 git 收到 SIGPIPE，
      # 在 pipefail 下整个脚本以 141 退出（线上就是这样失败的）。用 wc -l 把输出读完。
      changed=$(git status --porcelain -- data/stocks data/indices data/screen index.html .nojekyll | wc -l)
      if [ "$changed" -eq 0 ] && [ "${FORCE_PUBLISH:-0}" != "1" ]; then
        echo "数据和网页都没有变化，不发布（休市日/已是最新）"
        exit 0
      fi
    fi
    rm -rf .git
    git init -q -b "$BRANCH"
    git config user.name "data-bot"
    git config user.email "bot@users.noreply.github.com"
    git config http.postBuffer 524288000
    git add -A
    git commit -q -m "data $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    git remote add origin "$REPO_URL"
    ok=0
    for i in 1 2 3 4; do
      if git push -q --force origin "$BRANCH"; then ok=1; break; fi
      echo "推送失败，第 $i 次，稍后重试"; sleep $((i*15))
    done
    [ "$ok" = "1" ] || { echo "推送最终失败" >&2; exit 1; }
    echo "已发布到 $BRANCH（单提交，历史不增长）"
    ;;

  page)
    rc=$(branch_state)
    if [ "$rc" != "0" ]; then echo "$BRANCH 还不存在（先运行一次 update-data），跳过"; exit 0; fi
    rm -rf "$SITE_DIR"
    git clone --quiet --depth 1 --branch "$BRANCH" "$REPO_URL" "$SITE_DIR"
    cd "$SITE_DIR"
    cp "../$CODE_DIR/index.html" index.html
    if [ -z "$(git status --porcelain index.html)" ]; then echo "网页没有变化"; exit 0; fi
    git config user.name "data-bot"
    git config user.email "bot@users.noreply.github.com"
    git add index.html
    git commit -q -m "page $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    git push -q origin "$BRANCH"
    echo "网页已更新"
    ;;

  *)
    echo "用法: site.sh prepare|publish|page" >&2
    exit 2
    ;;
esac
