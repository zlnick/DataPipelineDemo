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
    "e2e_diag.py"                "查管道状态 + 最近消息（结果写 /tmp/diag2.log）" \
    "check_engine_source.py"     "★父类聚合引擎静态守卫（宿主直读 iris/src/demo/TransformProcess.cls；tSC 初始化/回执校验/uuid 小写等 17 项）"
  echo
  echo "== container/：dataflow-backend 容器内执行（backend 包 / IRIS :1972）=="
  printf '   %-28s %s\n' \
    "clinic_tables.py"           "CLINIC 源库四表初始化（幂等）" \
    "clinic_seed_data.py"        "CLINIC 样例数据：10 患者 + 就诊/诊断/药嘱" \
    "add_one_patient.py"         "★追加 1 个患者（含就诊/诊断/药嘱），不清空现有数据" \
    "generate_mock_data.py"      "生成 mock 数据（--fhir N --sql M）" \
    "seed_target_tables.py"      "★往 USER 的 Patient/Observation 目标表造测试数据（--count N --obs M --clear）" \
    "seed_fhir_demo.py"          "手动灌 FHIR 演示样本（10 Patient+30 Obs；已移出启动链，仅需历史存量时跑）" \
    "rescan_sql_source.py"       "★SQL 源全量重扫（停 Production→清凭证→启→核验）" \
    "term_map_build.py"          "★术语映射补录（预检缺映射 → 判定 Agent → 写回术语服务器；--dry 只查）" \
    "reset_ui_env.py"            "★一键重置演示环境（零起点；自带 26 项自检 + 退出码 0=干净；--check-only 只查不改）" \
    "test_llm.py"                "★LLM 连通性自检（配置/端点/模型/真实链路）" \
    "list_prod_items.py"         "列出 Production 组件名与启用状态" \
    "diag_msgs.py"               "★查最近消息 + SQL 源扫描凭证（判断重扫是否发生）" \
    "diag_shared_components.py"  "★查组件归属与派发关系（各管道 BP 使用方 / JavaGateway 依赖 / bp 配置）" \
    "diag_demo_config.py"        "★查 ^demo.Config 配置树（bp/bp_target 权威配置 / pipe 兼容 / soap）" \
    "cleanup_dead_config.py"     "★清理 ^demo.Config 死配置（指向不存在组件的 bp/bp_target/**sql2fhir.layout**；缺省只查，--apply 才删）" \
    "diag_sql_source_runtime.py"  "★查 SQL 源运行期：last key（凭证残留→零消息）+ job + 消息规模" \
    "test_target_dsn.py"         "★验证 DB 目标 DSN 按 jdbc_url 命名空间归一（演示默认 SQL 源 USER / 目标 CLINIC）" \
    "test_mapping_identity.py"   "★验证映射身份含**数据源维度**（3 源同名资产 Patient 不互相覆盖；缺陷 A）" \
    "test_source_ambiguity.py"   "★验证「同名资产跨数据源不得静默猜测归属」（重名不进索引 + 显式回报 source_ambiguous + 缺主表可照做提示）" \
    "test_fhir_schema_facts.py"  "★验证 FHIR 组装**事实注入**（每列 coded/system/system_from_row 由平台按事实决定，引擎不再猜）与「明文落 coding」检查 + *_text 落点" \
    "test_fhir_source_infra_components.py" "★验证 FHIR 源共享生产者 FHIRSyncService 全局仅 1 个且 category=shared（多管道切换不被误停）" \
    "test_pipeline_component_isolation.py" "★验证多管道**组件实例隔离**（同类别多组：聚合 BP 与其 BO/HTTP BO 不共用，组件名唯一、引用自洽；缺陷 A4）" \
    "test_target_type_awareness.py" "★验证「按名取目标列」类型感知（同名跨类型 DB Patient vs FHIR Patient 不混用、DB 映射不被误剔除）" \
    "test_term_gap_check.py"     "★验证术语双 coding 完整性（模型 note 要求双 coding 却写 code/null → term_gap 报错；源无编码列 fail-open）+ Agent A/C1 提示词硬约束在位" \
    "test_term_gate_degrade.py"  "★验证术语门禁**默认降级放行 + 严格开关 + 待办清单**（缺映射不再中止生成；strict_terms=true 才 400；生成后复核只告警）" \
    "test_bp_static_admission.py" "★验证生成 BP 的编译期硬约束静态准入（跨行 TRY 内带参 Quit #1043、As %Library.Object #5373；顶层 Quit/单行 Try/裸 Quit 不误报）" \
    "test_bp_plan_execute.py"    "★验证聚合 BP 的 **Plan → Execute** 链（计划 schema / 骨架 / 方法级准入 / 断点续跑 / 失败显式 / 单测三态）" \
    "test_fhir_engine.py"        "★验证**父类通用聚合引擎**（能力边界 / 薄 BP 渲染过静态准入 / urn:uuid 小写规范 / 纯函数 BundleEntry·InjectBundleRefs）" \
    "test_fhir_spec_fields.py"   "★验证 FHIR 源**无数据也能知道字段**（平台规范快照兜底 + AI 按 R4 规范补全 + 来源审计与暂定字段复采）" \
    "test_dead_config_prune.py"  "★验证死配置收敛清理（bp/bp_target/sql2fhir.layout：dry_run 只报不删、apply 只删死键、全局兜底键与活键完好、幂等）" \
    "test_terminology_bo.py"     "★验证共享术语 BO 平台侧不变量（infra 不进 AI 枚举但进注册表、有/无 term_map 按需挂、多管道全局 1 实例、不进许可调度、拓扑校验放行）" \
    "test_items_enable_disable.py" "★验证组件启停**真的生效**（P3：停用后 ^Ens.Runtime Job=0/许可释放、启用后 Job=1；共享件不受牵连；正常路径不触发生产重启收敛）" \
    "verify_terminology_bo_render.py" "★零破坏验证共享术语 BO 的 Production 渲染（Item/className/Category=shared/三个 Host Settings/全局仅 1 实例）" \
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
