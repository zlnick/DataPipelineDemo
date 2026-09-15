# tools/guard/ai-session-rc.sh —— 受限 AI 会话的 bash rc（由 ai-session.sh 通过 --rcfile 加载）
# 只做两件事：① 加载 CLI 守卫（越界 docker 命令更早报错）；② 显示"当前处于受限会话"。

GUARD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PS1='[受限AI] \w \$ '

# 加载 CLI 守卫（防呆层：本项目之外的破坏性 docker 命令拒绝，退出码 77）
if [ -f "$GUARD_DIR/docker_guard.sh" ]; then
  # shellcheck disable=SC1091
  . "$GUARD_DIR/docker_guard.sh"
fi

printf '%s\n' "[受限AI] 文件写入仅限：本仓库、知识库、/tmp、~/Library/Caches（其余路径内核拒绝）。"
printf '%s\n' "[受限AI] docker 用本机真实 socket；CLI 守卫会拦本项目之外的破坏性操作（rc=77）。"

