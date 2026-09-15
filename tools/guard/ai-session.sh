#!/usr/bin/env bash
# tools/guard/ai-session.sh —— 启动「受限 AI 会话」（文件沙箱 + CLI 守卫）
#
# 用法（在普通终端里执行一次，然后在弹出的受限 shell 里让 AI 干活）：
#     cd /Users/lzhu/Documents/GeneratorDemo
#     ./tools/guard/ai-session.sh
#
# 两层同时生效：
#   1) macOS sandbox-exec —— **写入**只允许 仓库 / 知识库 / 临时目录 / 系统缓存，其余内核级拒绝。
#   2) source tools/guard/docker_guard.sh —— CLI 层防呆：本项目之外的破坏性 docker 命令拒绝（rc=77）。
#
# 退出受限会话：exit。
# 说明：本会话**不**改 DOCKER_HOST（用本机真实 docker socket）——代理层已于 2026-09-14 回滚，
#       不再用中间代理拦 docker；docker 侧的边界靠"规则 + CLI 守卫 + 停下等用户确认"。

set -u
GUARD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$GUARD_DIR/../.." && pwd)"
VAULT="${AI_VAULT:-$HOME/Documents/IRIS-Dev-Vault}"
PROFILE="$GUARD_DIR/ai-sandbox.sb"
RC_FILE="$GUARD_DIR/ai-session-rc.sh"

printf '\n=== GeneratorDemo 受限 AI 会话（文件沙箱 + CLI 守卫）===\n'
printf '  仓库（可写）  : %s\n' "$REPO"
printf '  知识库（可写）: %s\n' "$VAULT"
printf '  临时目录（可写）: /tmp、~/Library/Caches\n'
printf '  其它路径      : 只读（写入会被内核拒绝）\n'
printf '  Docker        : 本机真实 socket（边界靠 CLI 守卫 + 规则）\n'

if ! command -v sandbox-exec >/dev/null 2>&1; then
  printf '✗ 未找到 sandbox-exec，无法启用文件沙箱；请改用 IDE 侧人工确认（关闭终端自动批准）。\n' >&2
  exit 1
fi

# 用 bash 起受限交互 shell：--rcfile 加载受限会话 rc（内含 CLI 守卫 + 提示符标记）
exec sandbox-exec -f "$PROFILE" -D REPO="$REPO" -D VAULT="$VAULT" -D CACHES="$HOME/Library/Caches" \
  "${AI_SHELL:-/bin/bash}" --rcfile "$RC_FILE" -i

