#!/usr/bin/env bash
# =====================================================================
# 测试数据工具箱 —— 统一执行入口
#
# 用法:
#   ./run.sh list                        列出全部脚本（含用途与运行环境）
#   ./run.sh <脚本名> [参数...]           自动判断在宿主机还是容器内执行
#   FORCE_HOST=1 ./run.sh <脚本名> ...   强制在宿主机执行
#   FORCE_CONTAINER=1 ./run.sh <脚本名> ...  强制在 dataflow-backend 容器内执行
#
# 约定:
#   host/      宿主机执行 → 访问 http://localhost:5001（后端 API）
#                            http://localhost:52773（IRIS FHIR Server）
#   container/ 复制到 dataflow-backend 容器内执行 → 使用 backend 包 / 直连 IRIS(1972)
# =====================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
CTR="dataflow-backend"

list_scripts() {
  echo "== host/：宿主机执行（后端 API :5001 / FHIR :52773）=="
  printf '   %-28s %s\n' \
    "gen_test_patient.py"        "★造 FHIR 测试数据：造源数据→触发同步→校验落地" \
    "seed_clinic.py"             "★造 CLINIC 演示数据（等价界面「生成演示数据」按钮）" \
    "check_fhir.py"              "★查 FHIR 落地情况：各资源数量 + UUID 校验" \
    "check_new_patient.py"       "核实新患者 MRN-1003 整户资源及引用关联" \
    "check_name_encoding.py"     "★查中文是否正确落地（打印原始 JSON）" \
    "clean_old_fhir.py"          "清理 FHIR 里非 UUID 格式的旧残留资源" \
    "del_probe_patient.py"       "删除探针患者（保持演示数据干净）" \
    "regenerate_pipeline.py"     "按当前登记重新生成单管道（等价界面「生成」）" \
    "gen_multi_pipeline.py"      "组装并生成多管道（与前端逻辑一致）" \
    "verify_license_budget.py"   "验证许可预算：连生成两条管道，检查自动让路" \
    "verify_license_scheduling.py" "★验证许可调度：超上限分组生成即停用（不再 500）+ 一键切换自动让路（--check 只核对）" \
    "verify_pipeline_instances.py" "验证管道实体：重复生成不新增 + 类别 + 整条启停 + 许可守卫" \
    "e2e_multi_pipeline_isolation.py" "★e2e 验证「一条管道一个 BP」：建登记+造数+两条管道+消息分流（--reuse 只跑生成）" \
    "e2e_diag.py"                "查管道状态 + 最近消息（结果写 /tmp/diag2.log）"
  echo
  echo "== container/：dataflow-backend 容器内执行（backend 包 / IRIS :1972）=="
  printf '   %-28s %s\n' \
    "clinic_tables.py"           "CLINIC 源库四表初始化（幂等）" \
    "clinic_seed_data.py"        "CLINIC 样例数据：10 患者 + 就诊/诊断/药嘱" \
    "add_one_patient.py"         "★追加 1 个患者（含就诊/诊断/药嘱），不清空现有数据" \
    "generate_mock_data.py"      "生成 mock 数据（--fhir N --sql M）" \
    "seed_target_tables.py"      "★往 USER 的 Patient/Observation 目标表造测试数据（--count N --obs M --clear）" \
    "init_fhir_data.py"          "向 FHIR Server 提交示例资源（transaction Bundle）" \
    "rescan_sql_source.py"       "★SQL 源全量重扫（停 Production→清凭证→启→核验）" \
    "reset_ui_env.py"            "★一键重置演示环境（零起点；自带 25 项自检 + 退出码 0=干净；--check-only 只查不改）" \
    "test_llm.py"                "★LLM 连通性自检（配置/端点/模型/真实链路）" \
    "list_prod_items.py"         "列出 Production 组件名与启用状态" \
    "diag_msgs.py"               "★查最近消息 + SQL 源扫描凭证（判断重扫是否发生）" \
    "diag_shared_components.py"  "★查组件归属与派发关系（各管道 BP 使用方 / JavaGateway 依赖 / bp 配置）" \
    "diag_demo_config.py"        "★查 ^demo.Config 配置树（bp/bp_target 权威配置 / pipe 兼容 / soap）" \
    "diag_sql_source_runtime.py"  "★查 SQL 源运行期：last key（凭证残留→零消息）+ job + 消息规模" \
    "check_bp_configname.py"     "验证 BP 能读自己的 Item 名（..%ConfigName）——一管道一 BP 的前提" \
    "verify_multi_bp_pipeline.py" "★零破坏验证一管道一 BP（拓扑归属 + 源 BS 投递 + IRIS 渲染）" \
    "diag_errors.py"             "Ens 事件日志尾部（查运行错误）" \
    "check_pair_sink.py"         "双管道检查：SOAP 落库 PatientEntity + 最近消息" \
    "dump_appdata.py"            "排查 ^Ens.AppData（已处理行/凭证/错误行）" \
    "check_ens_clear_api.py"     "Ens 清理 API 调用语义对照（classMethodVoid ✓ / classMethodValue ✗）" \
    "icd10_import.py"            "术语库：国标 ICD-10 导入 Terminology_Icd10" \
    "rxnorm_import.py"           "术语库：RxNorm RRF 导入" \
    "uscore_condition_import.py" "术语库：US Core Condition SNOMED 值集导入"
  echo
  echo "示例: ./run.sh gen_test_patient.py --family 赵 --given 敏 --count 2 --diagnosis 糖尿病"
  echo "      ./run.sh reset_ui_env.py          # 在容器内重置演示环境（自带自检清单）"
  echo "      ./run.sh reset_ui_env.py --check-only   # 只体检（不改数据，看现在干不干净）"
  echo "      ./run.sh check_fhir.py            # 在宿主机查 FHIR 落地情况"
}

if [ $# -eq 0 ] || [ "$1" = "list" ] || [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
  list_scripts
  exit 0
fi

TARGET="$1"; shift
SCRIPT=""; MODE=""

# 定位脚本：支持「裸名」与「相对/绝对路径」
if [ -f "$TARGET" ]; then
  SCRIPT="$(cd "$(dirname "$TARGET")" && pwd)/$(basename "$TARGET")"
else
  for d in host container; do
    [ -f "$HERE/$d/$TARGET" ] && SCRIPT="$HERE/$d/$TARGET" && MODE="$d"
  done
  if [ -z "$SCRIPT" ]; then
    for cand in "$REPO/$TARGET" "$REPO/tools/$TARGET"; do
      [ -f "$cand" ] && SCRIPT="$cand"
    done
  fi
fi

if [ -z "$SCRIPT" ]; then
  echo "[datakit] 找不到脚本: $TARGET" >&2
  echo "[datakit] 用 ./run.sh list 查看全部可用脚本" >&2
  exit 2
fi

# 模式：显式开关优先，否则按脚本依赖自动判断
if [ -n "${FORCE_HOST:-}" ]; then MODE=host; fi
if [ -n "${FORCE_CONTAINER:-}" ]; then MODE=container; fi
if [ -z "$MODE" ]; then
  if grep -qE '(^|[^a-zA-Z_.])import iris|iris\.dbapi|from backend\.|import backend\.' "$SCRIPT"; then
    MODE=container
  else
    MODE=host
  fi
fi

if [ "$MODE" = "container" ]; then
  BASE="$(basename "$SCRIPT")"
  docker cp "$SCRIPT" "$CTR:/tmp/$BASE" >/dev/null
  # test_llm.py 会对比「容器生效值 vs .env 待生效值」
  if grep -q "pending.env" "$SCRIPT"; then
    docker cp "$REPO/.env" "$CTR:/tmp/pending.env" >/dev/null
  fi
  echo "[datakit] 容器内执行: $BASE $*"
  docker exec -w /app "$CTR" python "/tmp/$BASE" "$@"
else
  echo "[datakit] 宿主机执行: $SCRIPT $*"
  ( cd "$REPO" && python3 "$SCRIPT" "$@" )
fi
