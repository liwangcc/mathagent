#!/usr/bin/env bash
# 把 GitHub 上的 main 与全部标签同步到 Gitee 镜像仓库。
#
# 用法：
#   GITEE_URL=git@gitee.com:liwangcc/mathagent.git bash scripts/mirror-to-gitee.sh
#   GITEE_URL=https://gitee.com/liwangcc/mathagent.git bash scripts/mirror-to-gitee.sh
#
# 首次使用前先在 Gitee 建一个**空仓库**（不要勾选“初始化 README”，否则会有一次
# 冲突的初始提交，第一次推送会被拒）。
set -euo pipefail

GITEE_URL="${GITEE_URL:-}"
if [ -z "$GITEE_URL" ]; then
  echo "缺少 GITEE_URL。例：GITEE_URL=git@gitee.com:liwangcc/mathagent.git bash $0" >&2
  exit 2
fi

cd "$(dirname "$0")/.."

if git remote get-url gitee >/dev/null 2>&1; then
  git remote set-url gitee "$GITEE_URL"
else
  git remote add gitee "$GITEE_URL"
fi

echo "推送到 $GITEE_URL"
git push gitee main:main
git push gitee --tags

echo
echo "完成。检查：git ls-remote --tags gitee"