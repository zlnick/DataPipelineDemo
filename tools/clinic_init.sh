#!/usr/bin/env bash
# 确保 CLINIC 演示源库四表存在（Patient/Encounter/Diagnosis/MedicationOrder）。
#
# 幂等：**缺表才建，已有表与数据不动**（与旧版 tools/clinic_tables.py 的 DROP 行为不同）。
# 缺表时 SQL 源 BS（CLINIC 四表轮询）会报错、界面「生成演示数据」也会失败，故纳入初始化流程。
#
# 用法: bash tools/clinic_init.sh            # 确保表存在（默认）
#       bash tools/clinic_init.sh --reset    # DROP 四表后重建（会清空数据）
set -euo pipefail
cd "$(dirname "$0")/.."

BE=dataflow-backend

echo "== CLINIC 源库表结构初始化（Patient / Encounter / Diagnosis / MedicationOrder）=="
docker compose up -d iris "$BE" >/dev/null 2>&1 || true

# 工具送进容器执行（backend 容器内 /app 有 backend 包，可 import clinic_schema）
docker exec -i "$BE" sh -c 'cat > /tmp/clinic_tables.py' < tools/clinic_tables.py
docker exec "$BE" python /tmp/clinic_tables.py --host iris "$@"
