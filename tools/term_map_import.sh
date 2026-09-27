#!/usr/bin/env bash
# 薄包装：导入术语映射种子（供 tools/setup.sh 调用；实现见 term_map_sync.py）
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 tools/term_map_sync.py import "$@"
