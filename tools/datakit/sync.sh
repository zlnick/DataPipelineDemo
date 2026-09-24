#!/usr/bin/env bash
# =====================================================================
# 一键同步：把 tools/ 下的**权威脚本**同步到 tools/datakit/{host,container}/ 副本
#
# 为什么需要：run.sh **优先执行副本**（副本不存在时才回退 tools/ 原版）→ 副本过期会造成
#   「测试假绿/假红」（已记录的坑）。本脚本让副本始终跟上权威版本。
#
# 用法：
#   bash tools/datakit/sync.sh                  # 同步所有**已有副本**（只从 tools/ 拷出）
#   bash tools/datakit/sync.sh --check          # 只报告差异（有差异时退出码 1）
#   bash tools/datakit/sync.sh --add <脚本名> --to host|container   # 给新脚本登记副本
#
# 约定：**副本 ≠ 权威** —— 只做 tools/ → datakit 的单向同步，绝不反向覆盖。
# =====================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
TOOLS="$REPO/tools"
MODE="sync"
ADD_NAME=""
ADD_TO=""

while [ $# -gt 0 ]; do
  case "$1" in
    --check) MODE="check"; shift ;;
    --add) ADD_NAME="${2:-}"; shift 2 ;;
    --to) ADD_TO="${2:-}"; shift 2 ;;
    *) echo "未知参数: $1"; exit 2 ;;
  esac
done

if [ -n "$ADD_NAME" ]; then
  if [ ! -f "$TOOLS/$ADD_NAME" ] && [ ! -f "$REPO/$ADD_NAME" ]; then
    echo "❌ tools/$ADD_NAME 与仓库根 $ADD_NAME 都不存在"; exit 2
  fi
  case "$ADD_TO" in host|container) ;; *) echo "❌ --to 必须是 host 或 container"; exit 2 ;; esac
  _src="$TOOLS/$ADD_NAME"
  [ -f "$_src" ] || _src="$REPO/$ADD_NAME"
  cp -f "$_src" "$HERE/$ADD_TO/$ADD_NAME"
  echo "✅ 已登记并复制：$ADD_NAME → tools/datakit/$ADD_TO/"
  exit 0
fi

updated=0; same=0; missing=0; orphan=0
# 权威脚本来源 = tools/ 下的全部 .py + **仓库根**的 .py（AGENTS.md 口径："权威版本在仓库 tools/（少数在仓库根）"）
for src in "$TOOLS"/*.py "$REPO"/*.py; do
  [ -e "$src" ] || continue
  name="$(basename "$src")"
  found=""
  for d in host container; do
    dst="$HERE/$d/$name"
    [ -f "$dst" ] || continue
    found="yes"
    if cmp -s "$src" "$dst"; then
      same=$((same + 1))
    elif [ "$MODE" = "sync" ]; then
      cp -f "$src" "$dst"; updated=$((updated + 1))
      echo "  ↑ 更新 $d/$name"
    else
      updated=$((updated + 1))
      echo "  ≠ 差异 $d/$name"
    fi
  done
  [ -n "$found" ] || missing=$((missing + 1))
done

for d in host container; do
  for dst in "$HERE/$d"/*.py; do
    [ -e "$dst" ] || continue
    name="$(basename "$dst")"
    if [ ! -f "$TOOLS/$name" ] && [ ! -f "$REPO/$name" ]; then
      orphan=$((orphan + 1))
      echo "  ⚠ 孤儿副本（tools/ 已无对应脚本）：$d/$name"
    fi
  done
done

if [ "$MODE" = "check" ]; then
  echo "[sync --check] 需更新 $updated 个；已一致 $same 个；未纳管（无副本→run.sh 回退 tools/）$missing 个；孤儿 $orphan 个"
  [ "$updated" -eq 0 ] || exit 1
else
  echo "[sync] 已更新 $updated 个；已一致 $same 个；未纳管 $missing 个；孤儿 $orphan 个"
fi
