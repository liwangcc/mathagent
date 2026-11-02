#!/usr/bin/env bash
# 把 GitHub 上的 main 与全部标签同步到国内镜像仓库（Gitee / GitLink / GitCode / AtomGit 都行）。
#
# 用法：
#   MIRROR_URL=git@gitee.com:liwangcc/mathagent.git bash scripts/mirror-to-gitee.sh
#   MIRROR_URL=https://gitlink.org.cn/liwangcc/mathagent.git bash scripts/mirror-to-gitee.sh
#   MIRROR_URL=... MIRROR_NAME=gitcode bash scripts/mirror-to-gitee.sh   # 想同时留几个镜像时
#
# 先在目标平台建一个**空仓库**（不要初始化 README，否则第一次推送会冲突）。
set -euo pipefail

MIRROR_URL="${MIRROR_URL:-${GITEE_URL:-}}"
MIRROR_NAME="${MIRROR_NAME:-mirror}"

if [ -z "$MIRROR_URL" ]; then
  echo "缺少 MIRROR_URL。例：MIRROR_URL=git@gitee.com:liwangcc/mathagent.git bash $0" >&2
  exit 2
fi

cd "$(dirname "$0")/.."

if git remote get-url "$MIRROR_NAME" >/dev/null 2>&1; then
  git remote set-url "$MIRROR_NAME" "$MIRROR_URL"
else
  git remote add "$MIRROR_NAME" "$MIRROR_URL"
fi

echo "推送到 $MIRROR_NAME -> $MIRROR_URL"
git push "$MIRROR_NAME" main:main
git push "$MIRROR_NAME" --tags

echo
echo "完成。检查：git ls-remote --tags $MIRROR_NAME"