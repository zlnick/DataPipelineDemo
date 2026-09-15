#!/usr/bin/env bash
# tools/guard/docker_guard.sh —— Docker 操作边界守卫（防呆层）
#
# 目的：把「AI 只能操作本项目容器」这条边界，从"靠自觉"变成"被脚本拦"。
# 用法：在本项目仓库目录里 `source tools/guard/docker_guard.sh`，之后的 `docker` 调用都会先过白名单。
#
# 允许：
#   - 只读命令（ps / logs / inspect / images / diff / events / compose ls|ps|logs|config …）→ 直接放行
#   - mutating 命令，且目标**全部**匹配 ^(dataflow-[...]|iris-terminology)(:.*)?$ → 放行
#   - `docker compose <mutating>`（up/start/stop/restart/down/rm/build/exec …）且当前目录 = 本项目根 → 放行
# 拒绝（退出码 77，并记审计日志 .trash/docker-guard.log）：
#   - 任何作用于本项目之外的容器/镜像/卷/网络的 mutating 命令
#   - `system|builder|image|container|volume|network|context|plugin|swarm|node` 的破坏性形式
#   - 在本项目目录之外执行 mutating 形式的 `docker compose`
#
# ⚠ 诚实说明：这是**防呆层不是安全边界**。`/usr/local/bin/docker` 之类的绝对路径调用可绕过；
#    真正的硬隔离见 tools/guard/README.md（L3：只读 docker socket 代理 / macOS sandbox-exec）。
#    本守卫**故意不提供**可被 AI 自行打开的放行开关：确需越界由用户在未 source 的终端手动执行。

DG_PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DG_REAL_DOCKER="${DG_REAL_DOCKER:-$(type -P docker)}"
DG_AUDIT="${DG_PROJECT_DIR}/.trash/docker-guard.log"
DG_ALLOW_NAME_RE='^(dataflow-[a-zA-Z0-9._-]+|iris-terminology)(:.*)?$'
DG_ALLOW_IMAGE_RE='^generatordemo-[a-zA-Z0-9._-]+(:.*)?$'

if [[ -z "$DG_REAL_DOCKER" ]]; then
  echo "[guard] 未找到 docker 可执行文件（type -P docker 为空），守卫未生效" >&2
  return 1 2>/dev/null || exit 1
fi

_dg_audit() {  # $1=DENY|ALLOW  $2=说明
  mkdir -p "$(dirname "$DG_AUDIT")" 2>/dev/null
  printf '%s %s pwd=%s cmd=docker %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$(pwd -P)" "$2" >>"$DG_AUDIT" 2>/dev/null
}

_dg_is_readonly() {  # $1=sub  $2=sub2
  case "$1" in
    ps|images|version|info|events|logs|inspect|port|history|stats|diff|top|search|wait|attach)
      return 0 ;;
    image|container|volume|network|system|builder|context|node|service|plugin)
      case "$2" in ls|list|inspect|df|events|ps|history|top|logs|show) return 0 ;; esac ;;
    compose)
      case "$2" in ls|ps|logs|config|version|top|events|images|port) return 0 ;; esac ;;
  esac
  return 1
}

_dg_deny() {  # $1=被拒的命令描述
  _dg_audit DENY "$1"
  {
    printf '\n\033[31m[guard] 已拒绝：docker %s\033[0m\n' "$1"
    printf '[guard] 允许范围：容器名 ^(dataflow-.*|iris-terminology)$，或在本项目目录内执行 docker compose。\n'
    printf '[guard] 本项目之外的 Docker 对象一律只读——需要时请把命令交给用户，在未 source 本守卫的终端执行。\n'
    printf '[guard] 审计日志：%s\n' "$DG_AUDIT"
  } >&2
  return 77
}

docker() {
  local sub="${1:-}" sub2="${2:-}"
  local -a args=("$@")

  # 1) 只读 → 直通
  if _dg_is_readonly "$sub" "$sub2"; then
    "$DG_REAL_DOCKER" "$@"; return $?
  fi

  # 2) compose：mutating 形式要求在项目根目录
  if [[ "$sub" == "compose" ]]; then
    if [[ "$(pwd -P)" != "$DG_PROJECT_DIR" ]]; then
      # ⚠ 注意：不要写成 "$sub2（中文…）"——bash 3.2(macOS) 在变量紧跟全角字符时会吞字节，见 knowledge/04-Pitfalls
      _dg_deny "compose ${sub2} - 当前目录不是本项目根: ${DG_PROJECT_DIR}"; return $?
    fi
    "$DG_REAL_DOCKER" "$@"; return $?
  fi

  # 3) 其余 mutating：校验目标名
  local start=1
  case "$sub" in image|container|volume|network|system|builder|context|node|service|plugin) start=2 ;; esac

  # 只校验首个目标的命令（其后是命令/参数，不是对象名）
  local first_only=0
  case "$sub" in exec|cp|run|create|attach|update|stats|top) first_only=1 ;; esac

  local -a targets=()
  local i arg
  for (( i=start; i<${#args[@]}; i++ )); do
    arg="${args[$i]}"
    [[ "$arg" == -* ]] && continue
    targets+=("$arg")
    [[ $first_only -eq 1 ]] && break
  done

  if (( ${#targets[@]} == 0 )); then
    _dg_deny "${*} - 未指定目标，破坏性/全局操作不在边界内"; return $?
  fi

  local t
  for t in "${targets[@]}"; do
    if [[ ! "$t" =~ $DG_ALLOW_NAME_RE ]]; then
      if [[ "$sub" == "rmi" && "$t" =~ $DG_ALLOW_IMAGE_RE ]]; then
        continue
      fi
      _dg_deny "${*} - 目标 '$t' 不属于本项目"; return $?
    fi
  done

  _dg_audit ALLOW "$*"
  "$DG_REAL_DOCKER" "$@"
}

echo "[guard] Docker 边界守卫已启用：仅允许 $(basename "$DG_PROJECT_DIR") 自己的容器（dataflow-* / iris-terminology）；审计日志 .trash/docker-guard.log" >&2
