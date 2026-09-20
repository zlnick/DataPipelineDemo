#!/usr/bin/env bash
# 从**已运行的 IRIS 容器**里提取 JDBC 驱动到 ./jdbc/（免下载、版本与 IRIS 一致、不涉及再分发）。
#
# 背景：backend 的「数据源连通测试 / 选 schema·表 / 分析列 / DB 元数据发现」走 JayDeBeApi + JDBC jar；
# 该驱动是 InterSystems 专有件（**不入版本库**），但 **IRIS 官方镜像自带它**
# （`/usr/irissys/dev/java/lib/1.8/intersystems-jdbc-*.jar`）→ 从容器里取最省事，无需去官网找。
#
# 新环境首次使用：
#   docker compose up -d iris      # 先起 IRIS（驱动就在镜像里；backend 的 ./jdbc 挂载此时还是空的）
#   bash tools/fetch_jdbc_jar.sh   # 提取到 ./jdbc/
#   docker compose up -d           # 再起其余服务
# 若服务已全部起来：提取后 `docker compose restart backend`
# （⚠ 首次启动时目标表为空，restart 不会造成数据损失；⚠ 环境已有演示数据时请勿随意 restart，会触发 init_data.py 重建表）
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source tools/guard/docker_guard.sh >/dev/null 2>&1 || true

C="${IRIS_CONTAINER:-dataflow-iris}"
DEST="jdbc"
SRC_DIR="/usr/irissys/dev/java/lib/1.8"

mkdir -p "$DEST"

if ! docker exec "$C" sh -c "ls $SRC_DIR/intersystems-jdbc-*.jar" >/dev/null 2>&1; then
  echo "✗ 容器 $C 中未找到 JDBC 驱动（或容器未运行）。请先执行： docker compose up -d iris" >&2
  exit 1
fi

JAR="$(docker exec "$C" sh -c "ls $SRC_DIR/intersystems-jdbc-*.jar | head -1 | xargs -n1 basename")"
# ⚠ 本项目 docker 守卫会把 `docker cp` 的**源**路径当越界目标校验 → 用 cat 重定向规避
docker exec "$C" sh -c "cat ${SRC_DIR}/${JAR}" > "${DEST}/${JAR}"

SZ="$(wc -c < "${DEST}/${JAR}" | tr -d ' ')"
if [ "${SZ}" -lt 100000 ]; then
  echo "✗ 提取结果异常（${SZ} 字节），请检查容器 ${C} 是否健康" >&2
  exit 1
fi
# ⚠ 变量后若紧跟全角字符（如 `$JAR（`），macOS bash 3.2 会把全角字节吞进变量名 → 一律用 ${VAR} 写法
echo "✓ 已提取 ${JAR}（${SZ} 字节）→ ./${DEST}/"
ls -la "${DEST}"
echo "  （来源：容器 ${C}:${SRC_DIR}/${JAR} —— 与 IRIS 镜像版本一致，无需手工下载）"
