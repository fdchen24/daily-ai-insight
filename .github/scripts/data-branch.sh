#!/usr/bin/env bash
# data 分支的检出与写回。采集与解读拆成了两个 job，两边都要做同样的事，
# 抄两份 YAML 迟早会改漏一份，所以抽成脚本。
#
#   prepare                     在 .data-branch/ 建好 data 分支的工作区
#   push <标签> <日志> [备注]    把 data/ 的变更与日志提交回 data 分支
set -euo pipefail

prepare() {
  mkdir -p .data-branch
  cd .data-branch
  git init -q -b data
  git config user.name "github-actions[bot]"
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
  git remote add origin \
    "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
  if git fetch -q --depth 1 origin data; then
    git checkout -q -B data FETCH_HEAD
  else
    echo "data 分支尚不存在，本次运行将创建"
  fi
  # data 分支自带忽略规则：raw/ 与 candidates/ 是可由 collect_all 重算的
  # 中间产物，单批次约 8MB，不入库
  mkdir -p data
  printf 'raw/\ncandidates/\n' > data/.gitignore
}

push() {
  local label="$1" log_file="$2" note="${3:-}"
  cd .data-branch
  mkdir -p data/logs
  # 日志按 job 分文件：两个 job 写同一个文件名会互相覆盖
  tail -c 200000 "../${log_file}" \
    > "data/logs/$(date -u +%Y-%m-%d)-${label}.log" || true
  git add -A data
  if git diff --cached --quiet; then
    echo "无变更，跳过提交"
    return 0
  fi
  git commit -q -m "chore(data): ${label} $(date -u +%Y-%m-%d)${note}"
  # 两个 job 串行推同一分支，正常不会撞；真撞上了（手动触发与定时任务错峰
  # 失败）就 rebase 重放一次，不为一次 push 冲突浪费整轮运行的结果
  git pull -q --rebase origin data || true
  git push -u origin data
}

case "${1:-}" in
  prepare) prepare ;;
  push) shift; push "$@" ;;
  *) echo "用法：data-branch.sh prepare | push <标签> <日志文件> [备注]" >&2
     exit 2 ;;
esac
