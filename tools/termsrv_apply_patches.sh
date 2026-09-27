#!/usr/bin/env bash
# 把 termsrv-patches/ 覆盖进 termsrv/ 子模块（幂等）—— 保证术语服务器具备 /mapping/* 映射能力。
# 用法: bash tools/termsrv_apply_patches.sh [目标目录=termsrv]
set -euo pipefail
cd "$(dirname "$0")/.."
SRC="termsrv-patches"
DST="${1:-termsrv}"
if [ ! -f "$DST/iris/Dockerfile" ]; then echo "[patches] 目标不是 termsrv 子模块: $DST"; exit 1; fi
n=0
for f in $(find "$SRC" -type f | sort); do
  case "$f" in */README.md) continue ;; esac
  rel=$(echo "$f" | sed "s|^$SRC/||")
  mkdir -p "$DST/$(dirname "$rel")"
  if cmp -s "$f" "$DST/$rel"; then echo "  = $rel"
  else cp -f "$f" "$DST/$rel"; n=$((n+1)); echo "  + $rel"; fi
done
echo "[patches] applied=$n -> $DST"
