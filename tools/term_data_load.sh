#!/usr/bin/env bash
# 把随仓库分发的术语素材（data/terms-inbox/）灌入术语服务器（幂等）。
# 用法: bash tools/term_data_load.sh [--wait 120]
#
# 背景：裁剪版术语服务器（iris-terminology）只提供 GET 读路由，没有写入/导入端点，
# 因此灌数据必须在 backend 容器内用 DB-API 直连 TERMINOLOGY 命名空间写表。
# 灌完后：backend/services/clinic_seed.py（界面「生成演示数据」/ seed_clinic.py）
# 才能从术语库取到中文诊断与药品，CLINIC 四表演示数据才生成得出来。
set -euo pipefail
cd "$(dirname "$0")/.."

SRC=data/terms-inbox
BE=dataflow-backend

FILES="icd10_main.csv nrdl.tsv cbih.tsv"
for f in $FILES; do
  if [ ! -f "$SRC/$f" ]; then
    echo "  [!!] 缺术语素材 $SRC/$f（随仓库分发；如缺失请检查 git checkout）"
    exit 1
  fi
done

echo "== 术语概念导入（ICD-10 诊断 + NRDL/CBIH 药品）=="
docker compose up -d iris-terminology "$BE" >/dev/null 2>&1 || true

# 素材送进容器：逐文件 cat 注入（比 docker cp 更稳，且不受守卫路径判定影响）
# ⚠ 不带 -i 的 exec 不吃 stdin；下面的 `cat > ... < 文件` 自带重定向。
#   （曾因一句 `docker exec -i ... 'mkdir -p ...'` 没有重定向 stdin，在「后台运行 setup」时无限挂住
#     —— 2026-09-28 全新实例复验发现，见该日知识库笔记）
docker exec "$BE" mkdir -p /tmp/terms-inbox
for f in $FILES; do
  docker exec -i "$BE" sh -c "cat > /tmp/terms-inbox/$f" < "$SRC/$f"
done
docker exec -i "$BE" sh -c 'cat > /tmp/term_data_load.py' < tools/term_data_load.py

docker exec "$BE" python /tmp/term_data_load.py --dir /tmp/terms-inbox "$@"
