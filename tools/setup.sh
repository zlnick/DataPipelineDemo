#!/usr/bin/env bash
# 一键初始化演示环境：让 git clone 之后 docker compose 能直接拉起所有容器
# 用法: bash tools/setup.sh [--check]   默认 = 全流程（git 准备 + 构建启动）
set -euo pipefail
cd "$(dirname "$0")/.."
source tools/guard/docker_guard.sh >/dev/null 2>&1 || true

if [ "${1:-}" = "--check" ]; then
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

echo "== 4/5 构建并启动全部服务（首次需 InterSystems 容器仓库 IAM 账号拉 irishealth-community 镜像）=="
docker compose up -d --build iris
compgen -G "jdbc/intersystems-jdbc-*.jar" >/dev/null 2>&1 && echo "  JDBC 驱动已在，跳过提取" || bash tools/fetch_jdbc_jar.sh
docker compose up -d --build

echo "== 5/5 术语映射种子（可选：让术语转换立即可用）=="
if [ -f data/seeds/term_map_seed.json ] && [ -f tools/term_map_import.sh ]; then
  bash tools/term_map_import.sh --wait 120 || echo "  [!!] 导入失败，稍后手动: bash tools/term_map_import.sh"
else
  echo "  种子未就绪：术语检索/校验可用，转换映射需另行灌库"
fi

echo "== 健康检查（尽力探测，不强制失败）=="
curl -fsS -m 5 -o /dev/null http://localhost:5001/api/pipelines/status 2>/dev/null && echo "  ok  backend :5001" || echo "  !!  backend :5001 未就绪"
curl -fsS -m 5 -o /dev/null http://localhost 2>/dev/null && echo "  ok  frontend :80" || echo "  !!  frontend :80 未就绪"
curl -fsS -m 5 -o /dev/null -u superuser:SYS http://localhost:52774/terminology/systems 2>/dev/null && echo "  ok  terminology :52774" || echo "  !!  terminology :52774 未就绪"

echo
echo "== 完成：http://localhost（中文）/ http://localhost/en（英文）=="
