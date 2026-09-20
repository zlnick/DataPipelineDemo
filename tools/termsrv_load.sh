#!/usr/bin/env bash
# 把术语服务器的类编译进**运行中的** iris-terminology 容器（不重建容器 → 不丢术语数据）。
#
# 背景：术语数据在 ./data/iris-terminology（容器重建后需重导），因此平台侧扩展
# （Terminology.Mapping.CodeMap + Production.API 的 /mapping/* 路由）走"热加载"而不是 rebuild。
# 类文件经 docker exec 的 stdin 送入容器（本项目 docker 守卫会把 `docker cp` 的**源**路径
# 误判成越界目标 → 用 cat 重定向规避，见 tools/guard/README.md）。
#
# 用法：bash tools/termsrv_load.sh
set -euo pipefail

cd "$(dirname "$0")/.."
source tools/guard/docker_guard.sh >/dev/null 2>&1 || true

C=iris-terminology
REPO="$PWD"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

send() {                       # $1=宿主文件 $2=容器内路径
  docker exec -i "$C" sh -c "cat > $2" < "$1"
}

# ⚠ 容器内 /tmp 存在历史遗留文件（root/宿主属主，不可覆盖）→ 用独立前缀名
send "$REPO/termsrv/iris/src/Terminology/Mapping/CodeMap.cls" /tmp/gd_codemap.cls
send "$REPO/termsrv/iris/src/Terminology/Production/API.cls" /tmp/gd_api.cls

printf '%s\n' \
  'do $system.OBJ.Load("/tmp/gd_codemap.cls","ck")' \
  'do $system.OBJ.Load("/tmp/gd_api.cls","ck")' \
  'write "COMPILE-DONE",!' \
  'halt' > "$TMP/compile.os"

docker exec -i "$C" /usr/irissys/bin/iris session IRIS -U TERMINOLOGY < "$TMP/compile.os" \
  | tr -d '\r' | tail -20
