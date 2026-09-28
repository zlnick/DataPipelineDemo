#!/usr/bin/env bash
# 术语服务器**向量能力**状态检查（缺表则补建）+ 使用指引；幂等、可选，**不进默认初始化**。
#
# 事实：剪裁版术语服务器的向量表/路由开箱可用，缺的只是向量**数据**（默认 0 行）；
# 演示核心不使用向量（术语转换只用成品映射 data/seeds/term_map_seed.json）。
#
# 用法:
#   bash tools/termsrv_vector_init.sh          # 检查状态（缺表则编译类建表）
#   bash tools/termsrv_vector_init.sh --check  # 只检查
set -euo pipefail
cd "$(dirname "$0")/.."

BE=dataflow-backend

echo "== 术语服务器向量能力（状态检查）=="
docker compose up -d iris-terminology "$BE" >/dev/null 2>&1 || true

docker exec -i "$BE" sh -c 'cat > /tmp/termsrv_vector_init.py' < tools/termsrv_vector_init.py
docker exec "$BE" python /tmp/termsrv_vector_init.py "$@"
