#!/usr/bin/env bash
# 一键初始化演示环境：让 git clone 之后 docker compose 能直接拉起所有容器
# 用法: bash tools/setup.sh [--check]   默认 = 全流程（git 准备 + 构建启动）
set -euo pipefail
cd "$(dirname "$0")/.."
source tools/guard/docker_guard.sh >/dev/null 2>&1 || true

# 参数：--check 只读体检；默认**不**构建/启动 embedding（演示不需要术语向量，初始化更快）
CHECK_ONLY=0
WITH_EMBEDDING=0
for arg in "$@"; do
  case "$arg" in
    --check)          CHECK_ONLY=1 ;;
    --with-embedding) WITH_EMBEDDING=1 ;;
    --no-embedding)   WITH_EMBEDDING=0 ;;
    -h|--help)
      echo "用法: bash tools/setup.sh [--check] [--no-embedding|--with-embedding]"
      echo "  --check           只读体检（不构建、不启动）"
      echo "  --no-embedding    不构建/启动 embedding 容器（**默认**；演示不需要术语向量）"
      echo "  --with-embedding  一并构建/启动 embedding（要做术语向量化试验时才需要）"
      exit 0 ;;
    *) echo "未知参数: $arg（支持 --check / --no-embedding / --with-embedding）" >&2; exit 2 ;;
  esac
done

if [ "$CHECK_ONLY" = "1" ]; then
  echo "== 只读体检 =="
  [ -f termsrv/iris/Dockerfile ] && echo "  ok  submodule" || echo "  !!  submodule 缺失"
  [ -f .env ] && echo "  ok  .env" || echo "  !!  .env 缺失"
  compgen -G "jdbc/intersystems-jdbc-*.jar" >/dev/null 2>&1 && echo "  ok  jdbc" || echo "  !!  jdbc 缺失"
  docker info >/dev/null 2>&1 && echo "  ok  docker" || echo "  !!  docker 未启动"
  exit 0
fi

echo "== 1/5 拉取 termsrv 子模块（术语服务器构建源；缺它 iris-terminology 构建失败）=="
if [ -f termsrv/iris/Dockerfile ]; then echo "  已就绪"; else git submodule update --init --recursive; fi
if [ -f tools/termsrv_apply_patches.sh ]; then bash tools/termsrv_apply_patches.sh; fi

echo "== 2/5 准备 .env（LLM key 可选：不填也能起，AI 功能显式报错）=="
if [ -f .env ]; then echo "  已存在"; else cp .env.example .env && echo "  已生成"; fi

echo "== 3/5 补 data 子目录占位（卷挂载目标）=="
mkdir -p data/iris data/iris-terminology data/terms-inbox data/embedding-model

echo "== 4/5 构建并启动服务（首次需 InterSystems 容器仓库 IAM 账号拉 irishealth-community 镜像）=="
# 首次启动前置：预建 IRIS 数据目录（**Windows / WSL 绑定挂载必需**）
# 背景（2026-09-28 实测：两次失败、一次成功）：
#   Windows 绑定挂载里，容器**早期启动**创建的目录会被呈现为 root:root，而 IRIS 首次启动的
#   「搬迁数据目录」是"先 mkdir、再以 irisowner 身份 chown" → 对 root 目录 chown 必然 EPERM：
#     Error while moving data directories ERROR #5001:
#       Error executing chown irisowner:irisowner /dur/irissys/: Error:1:
#   → IRIS 容器 Exited(1)、《管理门户 52773 / FHIR 端点全部无响应》。
#   实测结论：只要 /dur/irissys **事先由 irisowner 创建**，同一条 chown 就退化为"chown 自己的目录"
#   → 成功（CHOWN_OK），实例正常启动、数据目录随 ISC_DATA_DIRECTORY 持久化。
# 做法（幂等；**仅当数据目录里还没实例时**执行，两个标记文件都用于防呆）：
#   ① root 一次性：把 data/iris 属主改为 irisowner（宿主新建目录在容器内呈现为 root:root），
#      并在确认无实例后清掉可能残留的半成品 /dur/irissys（root 才能删掉 root 属主的残留）
#   ② irisowner：创建 /dur/irissys → chown（保证属主是 irisowner，IRIS 的那步 chown 才成功）
#   ⚠ 标记文件用 `irissys/iris.cpf`（IRIS 搬迁后写在数据目录根）+ `irissys/mgr/messages.log`：
#     只要任一存在就视为"已有实例"，**绝不删除**（2026-09-28 曾因标记写错为 mgr/iris.cpf 而误删运行中实例）
if [ ! -f data/iris/irissys/iris.cpf ] && [ ! -f data/iris/irissys/mgr/messages.log ]; then
  # 取 iris 镜像名（本步骤要用一次性容器；不用 `docker compose run`：它在 Windows 上把挂载源
  # 传成反斜杠路径（D:\...），实测创建的目录会被呈现为 root:root → chown EPERM；
  # 而 `docker run -v "D:/...":/dur`（正斜杠）→ 目录属主正常、chown 成功）
  # ⚠ 也不用 `docker compose config --images iris`：实测在本机（Windows + WSL + Docker Desktop）
  #   该子命令会**挂住不返回** → 改用 `docker inspect` / 直接从 compose 文件取，零额外依赖。
  IRIS_IMAGE="$(docker inspect dataflow-iris --format '{{.Config.Image}}' 2>/dev/null || true)"
  [ -n "$IRIS_IMAGE" ] || IRIS_IMAGE="$(awk '/irishealth-community/{print $2; exit}' docker-compose.yml 2>/dev/null)"
  # 挂载源：WSL 里要转成 Windows 形式（正斜杠），否则 Windows Docker daemon 解析不了 /mnt/...
  DUR_SRC="$PWD/data/iris"
  if command -v wslpath >/dev/null 2>&1; then
    DUR_SRC="$(wslpath -m "$PWD/data/iris" 2>/dev/null || echo "$PWD/data/iris")"
  fi
  IRIS_UID="$(docker run --rm --entrypoint sh "$IRIS_IMAGE" -c 'id -u irisowner' 2>/dev/null | tr -d '\r\n' || true)"
  [ -n "$IRIS_UID" ] || IRIS_UID=51773
  # ① root 一次性 + ② irisowner 预建：见上方说明（IRIS 首次搬迁里的 chown 要求 /dur/irissys 属主是 irisowner）
  #
  # ⚠ Windows + Docker Desktop 实测（2026-09-28，三种调用方式逐一比对）：
  #   · `docker compose run`（内部传反斜杠 Windows 路径）→ 新建目录在容器内呈现为 root:root → chown EPERM ✗
  #   · WSL 里的 Linux `docker` CLI（路径 /mnt/d/...）→ 呈现为 ubuntu(1000) → chown EPERM ✗
  #   · **Windows 侧 `docker.exe` + Windows 工作目录 + 正斜杠路径 `D:/...`** → 呈现为 irisowner → chown OK ✓
  #   因此：WSL 里通过 `powershell.exe` 委托 Windows 侧执行（保持 Windows cwd）；其它平台（macOS/Linux）直接 docker run。
  #   （本步只操作项目自己的数据目录，用一次性容器名 `dataflow-iris-preflight`；docker 守卫只影响 bash 里的 docker 函数）
  if command -v powershell.exe >/dev/null 2>&1 && command -v wslpath >/dev/null 2>&1; then
    DUR_WIN="$(wslpath -m "$PWD/data/iris" 2>/dev/null)"
    REPO_WIN="$(wslpath -w "$PWD" 2>/dev/null)"
    _win_docker() { powershell.exe -NoProfile -Command "Set-Location '$REPO_WIN'; $1"; }
    _win_docker "docker run --rm --name dataflow-iris-preflight -u 0:0 -v ${DUR_WIN}:/dur --entrypoint sh ${IRIS_IMAGE} -c 'chown irisowner:irisowner /dur; rm -rf /dur/irissys'" >/dev/null 2>&1 || true
    if _win_docker "docker run --rm --name dataflow-iris-preflight -u ${IRIS_UID}:${IRIS_UID} -v ${DUR_WIN}:/dur --entrypoint sh ${IRIS_IMAGE} -c 'mkdir -p /dur/irissys; chown irisowner:irisowner /dur/irissys'" >>/tmp/iris-preflight.log 2>&1; then
      echo "  已预建 data/iris/irissys（属主 irisowner，经 Windows 侧 docker.exe）—— 首次启动的数据目录搬迁需要"
    else
      echo "  [!!] 预建 data/iris/irissys 失败（非阻塞）：若 IRIS 起不来请把下面输出发出来"
      tail -5 /tmp/iris-preflight.log 2>/dev/null | sed 's/^/      /'
    fi
  else
    docker run --rm --name dataflow-iris-preflight -u 0:0 -v "${DUR_SRC}:/dur" --entrypoint sh "$IRIS_IMAGE" \
        -c 'chown irisowner:irisowner /dur; rm -rf /dur/irissys' >/tmp/iris-preflight.log 2>&1 || true
    docker rm -f dataflow-iris-preflight >/dev/null 2>&1 || true
    if docker run --rm --name dataflow-iris-preflight -u "${IRIS_UID}:${IRIS_UID}" -v "${DUR_SRC}:/dur" \
          --entrypoint sh "$IRIS_IMAGE" \
          -c 'mkdir -p /dur/irissys && chown irisowner:irisowner /dur/irissys' >>/tmp/iris-preflight.log 2>&1; then
      echo "  已预建 data/iris/irissys（属主 irisowner）—— 首次启动的数据目录搬迁需要"
    else
      echo "  [!!] 预建 data/iris/irissys 失败（非阻塞）：若 IRIS 起不来请把下面输出发出来"
      tail -5 /tmp/iris-preflight.log 2>/dev/null | sed 's/^/      /'
    fi
  fi
fi
docker compose up -d --build iris
compgen -G "jdbc/intersystems-jdbc-*.jar" >/dev/null 2>&1 && echo "  JDBC 驱动已在，跳过提取" || bash tools/fetch_jdbc_jar.sh
if [ "$WITH_EMBEDDING" = "1" ]; then
  docker compose up -d --build
else
  docker compose up -d --build iris backend frontend iris-terminology
  echo "  已跳过 embedding 容器（演示不需要术语向量化，初始化更快）"
  echo "  需要术语向量化 / 语义检索（可选，读者自行试验）时："
  echo "    ① docker compose up -d embedding          # 本地向量服务（首次自动下载模型，约 1.1GB）"
  echo "    ② bash tools/termsrv_vector_init.sh       # 查看向量能力状态 + 向量化步骤（表已就绪）"
fi

echo "== 等待核心服务就绪（**首次安装**时 IRIS / FHIR 仓库 / 目标表初始化需数分钟）=="
# ⚠ 顺序要求（2026-09-28 实测修正）：必须先等 backend 就绪，**再**做下面「演示数据前置」。
#   原因：全新实例的首次 FHIR 初始化（建库/建 namespace/编译/建 CLINIC 命名空间）在本机耗时 > 180s，
#   而 clinic_init / term_data_load 连 IRIS 时若凭据尚未初始化完会报
#     `<COMMUNICATION LINK ERROR> ... Access Denied`（旧版顺序 → 第 5 步整体失败，只能手动重跑）。
#
# ⚠ 探测口径（2026-09-28 实测修正）：本脚本常跑在 **WSL2(NAT)** 里，那种模式下 `localhost` 指向 WSL 自己，
#   到不了 Windows 上发布的容器端口（实测 `curl http://localhost:5001` → 000）。
#   故统一用 host_ok <端口> <路径> [账号:密码]：先试 localhost，不通再回退**默认网关**（= Windows 宿主）。
GW_IP=""
host_ok() {
  _port="$1"; _path="$2"; _auth="${3:-}"
  if [ -n "$_auth" ]; then
    curl -fsS -m 5 -o /dev/null -u "$_auth" "http://localhost:${_port}${_path}" 2>/dev/null && return 0
  else
    curl -fsS -m 5 -o /dev/null "http://localhost:${_port}${_path}" 2>/dev/null && return 0
  fi
  if [ -z "$GW_IP" ]; then
    GW_IP="$(ip route show default 2>/dev/null | awk '/^default/{print $3; exit}')"
  fi
  [ -z "$GW_IP" ] && return 1
  if [ -n "$_auth" ]; then
    curl -fsS -m 5 -o /dev/null -u "$_auth" "http://${GW_IP}:${_port}${_path}" 2>/dev/null
  else
    curl -fsS -m 5 -o /dev/null "http://${GW_IP}:${_port}${_path}" 2>/dev/null
  fi
}
for i in $(seq 1 60); do
  if host_ok 5001 /api/pipelines/status; then
    echo "  backend 就绪（约 $((i * 10))s）"
    break
  fi
  [ "$i" = "60" ] && echo "  [!!] 等待 600s 仍未就绪，仍继续做演示数据前置与健康检查（可稍后看日志）"
  sleep 10
done

echo "== 5/5 演示数据前置（CLINIC 源库表结构 + 术语服务器初始化）=="
if [ -f tools/clinic_init.sh ]; then
  bash tools/clinic_init.sh || echo "  [!!] CLINIC 建表失败，稍后手动: bash tools/clinic_init.sh"
else
  echo "  [!!] 缺 tools/clinic_init.sh：跳过 CLINIC 建表"
fi
if [ -f data/terms-inbox/icd10_main.csv ] && [ -f tools/term_data_load.sh ]; then
  bash tools/term_data_load.sh --wait 300 || echo "  [!!] 术语概念导入失败，稍后手动: bash tools/term_data_load.sh"
else
  echo "  [!!] 术语素材缺失：跳过概念导入（CLINIC 演示数据生成会受影响）"
fi
if [ -f data/seeds/term_map_seed.json ] && [ -f tools/term_map_import.sh ]; then
  bash tools/term_map_import.sh --wait 180 || echo "  [!!] 映射导入失败，稍后手动: bash tools/term_map_import.sh"
else
  echo "  映射种子未就绪：术语检索/校验可用，转换映射需另行灌库"
fi

echo "== 健康检查（尽力探测，不强制失败）=="
host_ok 5001 /api/pipelines/status && echo "  ok  backend :5001" || echo "  !!  backend :5001 未就绪"
host_ok 80 / && echo "  ok  frontend :80" || echo "  !!  frontend :80 未就绪"
host_ok 52774 /terminology/systems superuser:SYS && echo "  ok  terminology :52774" || echo "  !!  terminology :52774 未就绪"
[ -n "$GW_IP" ] && echo "  （WSL2 NAT：localhost 到不了宿主端口，已改用默认网关 $GW_IP 探测）"

echo
echo "== 完成：http://localhost（中文）/ http://localhost/en（英文）=="
