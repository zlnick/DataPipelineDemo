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

echo "== 5/5 演示数据前置（CLINIC 源库表结构 + 术语服务器初始化）=="
if [ -f tools/clinic_init.sh ]; then
  bash tools/clinic_init.sh || echo "  [!!] CLINIC 建表失败，稍后手动: bash tools/clinic_init.sh"
else
  echo "  [!!] 缺 tools/clinic_init.sh：跳过 CLINIC 建表"
fi
if [ -f data/terms-inbox/icd10_main.csv ] && [ -f tools/term_data_load.sh ]; then
  bash tools/term_data_load.sh --wait 180 || echo "  [!!] 术语概念导入失败，稍后手动: bash tools/term_data_load.sh"
else
  echo "  [!!] 术语素材缺失：跳过概念导入（CLINIC 演示数据生成会受影响）"
fi
if [ -f data/seeds/term_map_seed.json ] && [ -f tools/term_map_import.sh ]; then
  bash tools/term_map_import.sh --wait 120 || echo "  [!!] 映射导入失败，稍后手动: bash tools/term_map_import.sh"
else
  echo "  映射种子未就绪：术语检索/校验可用，转换映射需另行灌库"
fi

echo "== 等待核心服务就绪（**首次安装**时 IRIS / FHIR 仓库 / 目标表初始化需数分钟）=="
for i in $(seq 1 60); do
  if curl -fsS -m 5 -o /dev/null http://localhost:5001/api/pipelines/status 2>/dev/null; then
    echo "  backend 就绪（约 $((i * 10))s）"
    break
  fi
  [ "$i" = "60" ] && echo "  [!!] 等待 600s 仍未就绪，仍继续做健康检查（可稍后看日志）"
  sleep 10
done

echo "== 健康检查（尽力探测，不强制失败）=="
curl -fsS -m 5 -o /dev/null http://localhost:5001/api/pipelines/status 2>/dev/null && echo "  ok  backend :5001" || echo "  !!  backend :5001 未就绪"
curl -fsS -m 5 -o /dev/null http://localhost 2>/dev/null && echo "  ok  frontend :80" || echo "  !!  frontend :80 未就绪"
curl -fsS -m 5 -o /dev/null -u superuser:SYS http://localhost:52774/terminology/systems 2>/dev/null && echo "  ok  terminology :52774" || echo "  !!  terminology :52774 未就绪"

echo
echo "== 完成：http://localhost（中文）/ http://localhost/en（英文）=="
