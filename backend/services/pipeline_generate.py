# -*- coding: utf-8 -*-
"""数据管道**生成编排 + 拓扑构建**（自 `routes/pipelines.py` 迁出，方案 A Step 4，2026-09-20）。

内容 = `generate_multi_pipelines()` 的依赖闭包：类型注册表参数化（`build_pipeline_topology` /
`build_multi_pipeline_topology`）、sql2fhir 聚合 BP 生成链（`ensure_sql2fhir_bp*`）、
许可调度与运行态收敛（`license_plan` / `mark_suspended_components` / `converge_component_enabled`）、
术语盘点（`term_precheck` / `term_catalog_brief` / `term_summary`）、运行期配置落地
（`save_pipeline_topology` / `save_fhir_runtime_config` / `save_sql2fhir_layout`）与死配置收敛
（`prune_stale_bp_config`）。

设计约束：
- **服务层不读 Flask 全局**：原先 `generate_multi_pipelines` 里从 `request` 取的三个开关
  （`strict_terms` / `force` / `allow_rule_fallback`）改为**函数参数**，由路由读取后传入；
- 只依赖 `config` / `services` 同层模块，需要 IRIS 细节处沿用迁移前的局部导入；
- `routes/pipelines.py` 以**别名**导入（私有名保持不变），调用点与离线回归无需改动。
"""

import json
import logging
import re
from datetime import datetime

# P0 清理（2026-09-21）：移除迁移后不再使用的导入 —— `time`、`timezone`、`FHIRConfig`、
# `validate_agent`、`sql_source_meta`、`BO_PREFIX`（逐项实测：仅在 import 行出现 1 次）。
from backend.config import Config
from backend.services import (iris_connector, llm_client, pipeline_instances,
                              pipeline_validator, repository,
                              transformation_validator, type_registry,
                              wsdl_importer)
# 同层依赖：上下文归一（Step 2 迁出）与身份/签名（Step 1 迁出）—— 这里是它们的**唯一使用者入口**
from backend.services.pipeline_context import (
    c1_assets, c1_target_models, configs_for_identity, datasource_for_mappings,
    fhir_http_host_port, get_table_columns, merge_fhir_target_config,
    registered_table_columns, resolve_sql_source_tables, scope_source_context,
    source_tables_from_mappings, target_dsn, target_for_mappings,
)
from backend.services.pipeline_identity import (
    dedup_groups_by_identity, frozen_defs_from_instance, group_identity,
    inc_input_signature, running_items, stored_definition_complete,
)

logger = logging.getLogger(__name__)

# 模板库组件枚举（从类型注册表动态生成，供 Agent B 数据管道设计使用）
AVAILABLE_COMPONENTS = type_registry.get_available_components()

# —— 拓扑/配置解析用的模块级常量（Step 4 随函数一并迁入；原定义在 routes/pipelines.py）——
# SQLOperation 的 Query 里解析写入目标表名（`INSERT ... INTO <表>`）
_INTO_RE = re.compile(r"\bINTO\s+([^\s(]+)", re.IGNORECASE)
# prune_stale_bp_config 的保护名单：Agent BP 源码存档（非管道参数，永不清理）
_BP_CONFIG_PROTECTED = ("last_good", "last_good_at")


def save_pipeline_topology(pipeline):
    """保存 Agent B 输出的管道拓扑到 ^demo.Config（并写共享术语 BO 的服务器地址，见下）。"""
    import iris
    conn = iris_connector.get_connection()
    try:
        native = iris.createIRIS(conn)
        native.set(json.dumps(pipeline, ensure_ascii=False), "^demo.Config", "pipeline", "topology")
        # 共享术语 BO（demo.TerminologyOperation）的服务器地址：本次拓扑里有它才写
        # （与 backend 的 TERMSRV_BASE 同源 → 演示/运维改术语服务器不用改类；写完的缺省值
        #   与 BO 内置缺省一致，故不写也能跑）。
        if any("TerminologyOperation" in str((c or {}).get("className") or "")
               for c in (pipeline or {}).get("components") or []):
            from backend.services import term_catalog
            from urllib.parse import urlsplit
            parsed = urlsplit(term_catalog.base_url() if "://" in term_catalog.base_url()
                              else "http://" + term_catalog.base_url())
            if parsed.hostname:
                native.set(json.dumps({"host": parsed.hostname,
                                       "port": int(parsed.port or 52773)},
                                      ensure_ascii=False),
                           "^demo.Config", "termsrv")
    finally:
        iris_connector.reset_connections()


def save_fhir_runtime_config(topology: dict | None, target_config: dict | None = None,
                              mappings: list[dict] | None = None,
                              source_id: str | None = None) -> None:
    """把 FHIR 目标 HTTP Operation 的投递名 / 基础路径 / 凭据写入 ^demo.Config。

    TransformProcess 的 FHIR 分支据此构造 REST PUT（GenericOperation 消息契约）。非决策，仅参数化。

    2026-09-19：额外把**本管道的组装事实**合并进各资源的 schema（`fhir_schema_facts`）：
    每列的「是否编码列 / 源体系 / 体系来自源行哪列」——引擎据此写 coding，**不再自行猜测**
    （原先硬编码取源行 `CodeSystem` → 把整行共享的药品体系误挂到 route/type，且国标码 reasonCode 无体系）。
    """
    if not topology:
        return
    comp = next((c for c in (topology.get("components") or [])
                 if c.get("className") == "EnsLib.HTTP.GenericOperation"), None)
    if not comp:
        return
    base = (target_config or {}).get("base_url") \
        or (target_config or {}).get("endpoint") or ""
    path = "/csp/healthshare/fhirserver/fhir/r4"
    try:
        from urllib.parse import urlparse
        parsed = urlparse(base).path.rstrip("/")
        if parsed:
            path = parsed
    except Exception:  # noqa: BLE001
        pass
    import iris
    from backend.services import fhir_target_model as _ftm
    conn = iris_connector.get_connection()
    try:
        native = iris.createIRIS(conn)
        native.set(comp.get("name") or "FHIR_HTTP_Operation", "^demo.Config", "fhir", "operation")
        native.set(path, "^demo.Config", "fhir", "base_path")
        native.set((target_config or {}).get("username") or "superuser",
                   "^demo.Config", "fhir", "username")
        native.set((target_config or {}).get("password") or "SYS",
                   "^demo.Config", "fhir", "password")
        # 已建模资源组装 schema（path/system/type），供 TransformProcess 通用打包器读取
        # 2026-09-19：合并**本管道的事实**（源编码体系 / 是否编码列 / 体系来源列）。
        # ⚠ 按**每条映射自己的 source_id** 建事实索引：一次生成常含多个数据源的组，
        #   只用一个 source_id 会让别组映射拿错源的事实（实测缺陷）。
        from backend.services import fhir_schema_facts as _fsf
        _sids = {str(m.get("source_id") or source_id or "").strip() for m in (mappings or [])
                 if isinstance(m, dict)}
        _sids.discard("")
        _terms_by_src = {sid: _fsf.source_field_terms(sid) for sid in _sids}
        for _rt in _ftm.DEFAULT_RESOURCE_TYPES:
            _schema = _ftm.model_schema(_rt)
            if not _schema:
                continue
            if _terms_by_src:
                _facts = _fsf.column_facts(_rt, mappings, _terms_by_src,
                                           default_source_id=source_id)
                _schema = _fsf.apply_facts(_schema, _facts)
            native.set(json.dumps(_schema, ensure_ascii=False),
                       "^demo.Config", "fhir", "schema", _rt)
    finally:
        iris_connector.reset_connections()


def save_sql2fhir_layout(layout: dict, bp_name: str | None = None) -> None:
    """把 sql2fhir 布局 JSON 写到 IRIS（Agent 生成的 BP 运行时读取）。

    - `bp_name` 给出时写**实例级**键 `^demo.Config("sql2fhir","layout",<BP名>)`（多管道隔离：每个
      sql2fhir 组各有一个聚合 BP 实例，布局必须跟着实例走，否则两条管道互相串线）；
    - 同时写全局键 `^demo.Config("sql2fhir","layout")` 作为**历史兼容兜底**（单管道口径、
      以及未升级的既有 BP 类仍读它）。
    """
    import iris
    conn = iris_connector.get_connection()
    try:
        native = iris.createIRIS(conn)
        payload = json.dumps(layout, ensure_ascii=False)
        native.set(payload, "^demo.Config", "sql2fhir", "layout")
        if bp_name:
            native.set(payload, "^demo.Config", "sql2fhir", "layout", str(bp_name))
            logger.info("sql2fhir 布局已按实例写入: ^demo.Config(\"sql2fhir\",\"layout\",%s)", bp_name)
    finally:
        iris_connector.reset_connections()


def bp_plan_mode_on() -> bool:
    """BP 是否走"非整类"路径（engine / plan）——此时 Agent B 只出拓扑。"""
    import os
    return os.getenv("BP_GEN_MODE", "engine").strip().lower() in ("engine", "plan")


def bp_plan_mode_for(source_type: str, target_type: str) -> bool:
    """SQL→FHIR（= sql2fhir-patient-tx，唯一需要聚合 BP 的组合）**只让 Agent B 出拓扑**。

    BP 由平台渲染（engine 模式，调父类通用引擎）或计划链逐方法生成；其余组合的 BP 都由平台类
    （`demo.TransformProcess`）承担，不涉及整类生成，无需切换。
    """
    return (bp_plan_mode_on()
            and str(source_type).upper() == "SQL" and str(target_type).upper() == "FHIR")


def ensure_sql2fhir_bp_by_plan(*, layout: dict, mappings: list[dict], components: list[dict],
                                design_skill: str = "sql2fhir-patient-tx",
                                facts: dict | None = None) -> tuple[bool, str, dict]:
    """**Plan → Execute** 链：计划（小输出）→ 逐方法生成（每步编译/单测，可断点续跑）。

    返回 (ok, message, meta)；meta 含 bp_mode/plan_methods/unit 汇总，供响应回显（可审计）。
    失败**显式**返回 False（是否允许回退整类由调用方按开关决定，见 `ensure_sql2fhir_bp_dispatch`）。
    """
    from backend.services import bp_planner as _planner
    from backend.services import generated_bp as _gbp
    from backend.services import bp_method_tests as _bt
    bp_class = _gbp.BP_CLASS_NAME
    try:
        plan = _planner.build_plan(
            bp_class=bp_class, parent_api=set(_gbp.PARENT_API), layout=layout,
            mappings=mappings, components=components, design_skill=design_skill,
            facts=facts or {})
    except llm_client.AgentError as exc:
        return False, f"BP 计划阶段 AI 失败: {exc}", {"bp_mode": "plan", "stage": "plan"}
    _planner.ensure_progress_scope(bp_class, plan)   # 计划未变 → 保留进度（**断点续跑**）；变了才清空
    res = _gbp.generate_from_plan(plan=plan, layout=layout, mappings=mappings,
                                  components=components, parent_api=set(_gbp.PARENT_API),
                                  facts=facts or {}, bp_class=bp_class)
    meta = {"bp_mode": "plan", "plan_methods": [m.get("name") for m in (plan.get("methods") or [])],
            "plan_summary": str(plan.get("summary") or "")[:200],
            "bp_methods": res.get("methods") or {}, "resumed": res.get("resumed") or []}
    meta.update({k: v for k, v in _bt.units_summary(res.get("methods") or {}).items()})
    if res.get("ok"):
        logger.info("Plan 链 BP 生成成功：%s（方法 %s；单测 通过 %s / 跳过 %s）",
                    res.get("message"), meta["plan_methods"],
                    meta.get("unit_passed"), meta.get("unit_skipped"))
        return True, str(res.get("message") or ""), meta
    logger.warning("Plan 链 BP 生成失败：%s", str(res.get("message"))[:300])
    return False, str(res.get("message") or ""), meta


def ensure_sql2fhir_bp_dispatch(p_result, mappings, source_type: str, target_type: str,
                                 available, source_runtime, target_runtime,
                                 *, layout: dict | None = None,
                                 components: list[dict] | None = None,
                                 design_skill: str = "sql2fhir-patient-tx",
                                 facts: dict | None = None) -> tuple[bool, str, dict]:
    """BP 生成入口（单/多管道共用）：默认走 **Plan 链**；整类生成为**显式**回退开关。

    开关（环境变量）：
      · `BP_GEN_MODE` = engine（默认，平台渲染薄 BP 调父类引擎）| plan（AI 计划+逐方法生成）| full（旧：整类生成）
      · `BP_ALLOW_FULL_FALLBACK` = 1 时允许显式回退链 engine → plan → full（每级都标注 bp_mode）
    """
    import os
    mode = os.getenv("BP_GEN_MODE", "engine").strip().lower()
    fallback_ok = os.getenv("BP_ALLOW_FULL_FALLBACK", "0") == "1"
    if mode == "full" or not layout:
        ok, msg = ensure_sql2fhir_bp(p_result, mappings, source_type, target_type,
                                      available, source_runtime, target_runtime)
        return ok, msg, {"bp_mode": "full"}
    if mode == "engine":
        from backend.services import generated_bp as _gbp
        issues = _gbp.engine_supported(layout)
        if not issues:
            src = _gbp.render_engine_bp(_gbp.BP_CLASS_NAME)
            comp = _gbp._write_and_compile(_gbp.BP_CLASS_NAME, src)
            if comp.get("ok"):
                _gbp.save_last_good(src)
                logger.info("薄适配 BP 渲染+编译通过（engine 模式：聚合机制在父类 ProcessFHIRBundle）")
                return True, "engine: 平台渲染薄 BP（聚合机制在父类）", {
                    "bp_mode": "engine", "bp_engine": "demo.TransformProcess.ProcessFHIRBundle",
                    "bp_source_len": len(src)}
            logger.warning("薄适配 BP 编译失败（engine 模式）→ %s", str(comp.get("message"))[:200])
            if not fallback_ok:
                return False, f"engine 模式薄 BP 编译失败（未开启回退）: {comp.get('message')}", \
                    {"bp_mode": "engine"}
        else:
            logger.info("布局超出引擎能力（%s）→ 转 Plan 链", "；".join(issues[:3]))
            if not fallback_ok and mode == "engine":
                mode = "plan"          # 能力边界：直接走 Plan 链（AI 生成定制 BP），不算"回退"
    ok, msg, meta = ensure_sql2fhir_bp_by_plan(
        layout=layout, mappings=mappings, components=components or [],
        design_skill=design_skill, facts=facts)
    if ok or not fallback_ok:
        return ok, msg, meta
    logger.warning("Plan 链失败 → 按 BP_ALLOW_FULL_FALLBACK=1 回退整类生成（显式标注）: %s",
                   str(msg)[:200])
    ok2, msg2 = ensure_sql2fhir_bp(p_result, mappings, source_type, target_type,
                                    available, source_runtime, target_runtime)
    meta2 = dict(meta)
    meta2.update({"bp_mode": "full_fallback", "bp_mode_reason": str(msg)[:200]})
    return ok2, msg2, meta2


def ensure_sql2fhir_bp(p_result, mappings, source_type: str, target_type: str,
                        available, source_runtime, target_runtime) -> tuple[bool, str]:
    """确保 Agent 生成的 BP 编译可用：优先复用本次 p_result.generated_bp（静态+编译通过即用），
    缺失/失败则走 generate_and_compile_bp（编译错误回 feed ≤3 轮）。返回 (ok, message)。"""
    from backend.services import generated_bp as _gbp
    bp = p_result.get("generated_bp") if isinstance(p_result, dict) else None
    if bp:
        errs = _gbp.static_check_generated_bp(bp)
        if not errs:
            comp = _gbp.compile_generated_bp(bp)
            if comp.get("ok"):
                logger.info("Agent 首次输出 BP 即通过编译")
                _gbp.save_last_good(_gbp.get_generated_source())
                return True, comp.get("message", "")
            logger.warning("Agent 首次 BP 编译失败，进入反馈轮: %s", comp.get("message"))
        else:
            logger.warning("Agent 首次 BP 静态准入失败: %s", "；".join(errs))
    res = _gbp.generate_and_compile_bp(
        mappings=mappings, source_type=source_type, target_type=target_type,
        available_components=available,
        source_runtime=source_runtime, target_runtime=target_runtime)
    if res.get("ok"):
        _gbp.save_last_good(_gbp.get_generated_source())
        return True, str(res.get("message") or "")
    # 兜底 1（显式告警，不静默）：回滚到「上次通过验证的 Agent BP」（仍是 Agent 产物）
    rolled = _gbp.restore_last_good()
    if rolled.startswith("restored"):
        logger.warning("本次 Agent BP 未通过（%s）→ 已回滚 last_good BP（agent_bp_rolled_back）",
                       str(res.get("message"))[:200])
        return True, f"agent_bp_rolled_back:{rolled}"
    # 兜底 2：复用已存在且可编译的同类 BP（类库里的上一版）
    try:
        existed = iris_connector.class_method_value(
            "%Dictionary.ClassDefinition", "%ExistsId", "demo.SqlFhirPatientTxProcess")
        if int(existed or 0):
            st = str(iris_connector.class_method_value(
                "%SYSTEM.OBJ", "Compile", "demo.SqlFhirPatientTxProcess", "ck") or "")
            if st.startswith("1"):
                logger.warning("本次 Agent BP 未通过（%s）→ 复用类库里已编译的 BP（agent_bp_reused）",
                               str(res.get("message"))[:200])
                return True, "agent_bp_reused: " + str(res.get("message") or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("BP 回退复用检查失败: %s", exc)
    return False, str(res.get("message") or "")


def build_pipeline_topology(mappings: list[dict], source_type: str = "FHIR",
                            target_type: str = "DB",
                            suggested_types: list[str] | None = None,
                            source_config: dict | None = None,
                            target_config: dict | None = None,
                            ai_components: list[dict] | None = None,
                            bp_name: str = "TransformProcess") -> dict:
    """按源/目标类型 + 映射构建完整管道拓扑 JSON（组件带 className + settings）。

    - ai_components（Agent B/LLM 输出）非空时：**由 AI 决定组件构成与顺序**，
      Python 注册表只负责补全 className/settings（AI 决策 + 模板参数化），
      仅在 AI 遗漏必需组件时补齐保底，并把补齐清单写入 ai_supplemented 键。
    - 源/目标/通用组件模板来自类型注册表（type_registry）；
    - SQLOperation 的 Query/InputParameters 在此由 Python 依据目标表列构建
      （信息来自 information_schema），不再由 IRIS 侧查询；
    - SOAP 目标为「WSDL 导入型」：target_config 提供 wsdl/service/packages，
      先调 %SOAP.WSDL.Reader 生成 BO，再用 BO 类作为 Operation 组件；
    - SQL 源：source_config 提供 dsn/query/key_field 填充 SQLService settings；
    - PipelineGenerator 按通用规则渲染（name/className/settings）；
    - suggested_types: 仅影响排序（保留兼容旧规则路径）。
    - bp_name：转换 BP 的**业务主机名**（Ens 组件身份 = Item 名，class 可复用）。
      多管道并存时每条管道给自己的 BP 取名（如 TransformProcess__sql2soap），
      源 BS 的 TargetConfigNames 指向本管道自己的 BP —— 管道之间零耦合。
    """
    if ai_components:
        return build_from_ai_components(
            mappings, source_type=source_type, target_type=target_type,
            ai_components=ai_components,
            source_config=source_config, target_config=target_config,
            bp_name=bp_name)

    def _s(target: str, name: str, value: str) -> dict:
        """构造一个 Setting 项：{"target": "Adapter|Host", "name": ..., "value": ...}。"""
        return {"target": target, "name": name, "value": value}

    def _from_template(template: dict, name: str, comment: str = "") -> dict:
        """按注册表模板构建组件（adapter_settings → Adapter，host_settings/settings → Host）。"""
        settings = []
        for k, v in template.get("adapter_settings", {}).items():
            settings.append(_s("Adapter", k, v))
        for k, v in template.get("host_settings", {}).items():
            settings.append(_s("Host", k, v))
        for k, v in template.get("settings", {}).items():
            settings.append(_s("Host", k, v))
        return {
            "type": template["type"],
            "name": name,
            "className": template["className"],
            "comment": comment or template.get("comment", ""),
            "settings": settings,
        }

    components = []

    # 1. 源组件（按 source_type）
    for c in type_registry.get_source_components(source_type):
        if c["type"] == "SQLService":
            # SQL 源：用 source_config 填充 DSN（Adapter）/ Query、KeyFieldName（Host）
            cfg = source_config or {}
            query_val = cfg.get("query") or "SELECT * FROM SQLUser.PatientSource"
            key_field_val = cfg.get("key_field") or "ID"
            dsn_val = cfg.get("dsn") or "localTarget"
            components.append({
                "type": "SQLService", "name": "SQLService",
                "className": c["className"], "comment": c["comment"],
                "settings": [
                    _s("Adapter", "DSN", dsn_val),
                    _s("Adapter", "Query", query_val),
                    _s("Adapter", "KeyFieldName", key_field_val),
                    _s("Adapter", "JGService", "EnsLib.JavaGateway.Service"),
                    _s("Host", "TargetConfigNames", bp_name),
                ],
            })
        else:
            components.append(_from_template(c, c["type"]))

    # 2. 转换组件（转换 BP：本管道的实例名由调用方给定，class 可跨管道复用）
    for c in type_registry.get_common_components():
        if c["type"] == "TransformProcess":
            components.append(_from_template(c, bp_name))

    # 3. 目标组件（按 target_type）
    for c in type_registry.get_target_components(target_type):
        if c["type"] == "SQLOperation":
            # DB 目标：每个目标表一个 SQLOp，UPSERT 幂等
            for m in mappings:
                table = m.get("target_table", "")
                if not table:
                    continue
                cols = get_table_columns(table, target_config)
                if not cols:
                    logger.warning("目标表 %s 列结构为空，跳过 SQLOp", table)
                    continue
                col_sql = ", ".join(cols)
                q_marks = ", ".join("?" for _ in cols)
                query = f"INSERT OR UPDATE INTO {table} ({col_sql}) VALUES ({q_marks})"
                params = ",".join(f"*{col}" for col in cols)
                components.append({
                    "type": "SQLOperation", "name": f"SQLOp_{table}",
                    "className": c["className"], "comment": f"写入目标表 {table}",
                    "settings": [
                        _s("Adapter", "DSN", target_dsn(target_config)),
                        _s("Adapter", "JGService", "EnsLib.JavaGateway.Service"),
                        _s("Host", "Query", query),
                        _s("Host", "InputParameters", params),
                    ],
                })
        elif c.get("import_based"):
            # SOAP 目标（WSDL 导入型）：先导入 WSDL 生成 BO，再用 BO 类作为 Operation 组件
            wsdl = (target_config or {}).get("wsdl", "")
            service = (target_config or {}).get("service", "default")
            bo_class = (target_config or {}).get("bo_class", "")

            imp = {}
            if wsdl:
                imp = wsdl_importer.import_soap_operation(
                    wsdl, service, (target_config or {}).get("packages"))

            if imp.get("ok") and imp.get("boClass"):
                bo_class = imp["boClass"]
            elif not bo_class:
                bo_class = wsdl_importer.get_bo_class(service)

            if bo_class:
                wsdl_importer._save_bo_mapping(service, bo_class)
                components.append({
                    "type": "SOAPOperation", "name": f"SOAPOp_{service}",
                    "className": bo_class,
                    "comment": f"SOAP 调用 {service}（WSDL 导入 BO）",
                    "settings": [
                        # Adapter WebServiceURL 覆盖 WSDL 内 soap:address，指向远端 SOAP 服务
                        # （demo 默认 Python mock 第三方系统，见 services/mock_soap.py / Config.MOCK_SOAP_URL）
                        _s("Adapter", "WebServiceURL", target_config.get("endpoint") or Config.MOCK_SOAP_URL),
                    ],
                })
            else:
                logger.warning("SOAP 目标 WSDL 导入失败或未找到 bo_class: %s", imp.get("message"))
        elif c["type"] == "HTTPOperation":
            # FHIR 目标 = 通用 HTTP Operation：Adapter 配远端 server/port（参数归一，非决策）
            base = (target_config or {}).get("base_url") \
                or (target_config or {}).get("endpoint") or ""
            host, port = fhir_http_host_port(base)
            components.append({
                "type": "HTTPOperation", "name": "HTTPOperation",
                "className": c["className"], "comment": c["comment"],
                "settings": [
                    _s("Adapter", "HTTPServer", host),
                    _s("Adapter", "HTTPPort", port),
                ],
            })
        else:
            components.append(_from_template(c, c["type"]))

    # 4. JavaGateway（通用组件）
    for c in type_registry.get_common_components():
        if c["type"] == "JavaGateway":
            components.append(_from_template(c, c["className"]))

    # 4b. 共享术语 BO（仅当本次映射含 term_map 决策）：术语转换能力的唯一出口，
    #     与 JavaGateway 同级（category=shared、不参与许可调度、不被按类别让路停用）。
    if needs_terminology(mappings):
        _term_comp = terminology_component()
        if _term_comp:
            components.append(_term_comp)
            logger.info("本次映射含 term_map 决策 → 追加共享术语 BO %s", _term_comp.get("name"))

    # 5. 按 Agent B 建议的 type 顺序排序（未出现的排在后；sort 稳定保持原序）
    if suggested_types:
        fallback = len(suggested_types)
        components.sort(
            key=lambda c: (suggested_types.index(c["type"])
                           if c["type"] in suggested_types else fallback))

    return {"production": "demo.DataflowProduction", "components": components}


def build_from_ai_components(mappings: list[dict], *, source_type: str, target_type: str,
                              ai_components: list[dict],
                              source_config: dict | None = None,
                              target_config: dict | None = None,
                              bp_name: str = "TransformProcess") -> dict:
    """Agent B（LLM）驱动拓扑构建：AI 决定组件构成与顺序，注册表只补全参数。

    - 每个 AI 组件的 type 必须来自可用枚举（源/目标/通用模板全集），按 AI 的
      name/table 生成；className 与连接参数（DSN/Query/表 UPSERT 列等）由注册表注入；
    - AI 遗漏确属必需的组件（源服务全集/TransformProcess/目标操作/JavaGateway）时
      按模板补齐保底，补齐清单写入 topology["ai_supplemented"]（Python 不再自行
      决定整体构成，仅做完整性校验与参数化）。
    - bp_name：转换 BP 的主机名（Ens Item 名）；源 BS 的 TargetConfigNames 指向它。
      Agent B 若自带该组件名，仍统一改写为 bp_name —— 保证「一管道一 BP」的隔离。
    """

    def _s(target: str, name: str, value: str) -> dict:
        return {"target": target, "name": name, "value": value}

    def _from_template(template: dict, name: str, comment: str = "") -> dict:
        settings = []
        for k, v in template.get("adapter_settings", {}).items():
            settings.append(_s("Adapter", k, v))
        for k, v in template.get("host_settings", {}).items():
            settings.append(_s("Host", k, v))
        for k, v in template.get("settings", {}).items():
            settings.append(_s("Host", k, v))
        return {"type": template["type"], "name": name,
                "className": template["className"],
                "comment": comment or template.get("comment", ""),
                "settings": settings}

    templates: dict[str, dict] = {}
    for c in type_registry.get_source_components(source_type):
        templates[c["type"]] = c
    for c in type_registry.get_target_components(target_type):
        templates[c["type"]] = c
    for c in type_registry.get_common_components():
        templates[c["type"]] = c

    def build_component(tpl: dict, name: str | None = None,
                        table: str | None = None) -> dict | None:
        """按模板 + AI 命名/表选择构建单个组件；无法构建（如表列空）返回 None。"""
        ttype = tpl["type"]
        cfg_src = source_config or {}
        cfg_tgt = target_config or {}
        if ttype == "SQLService":
            return {
                "type": "SQLService", "name": name or "SQLService",
                "className": tpl["className"], "comment": tpl["comment"],
                "settings": [
                    _s("Adapter", "DSN", cfg_src.get("dsn") or "localTarget"),
                    _s("Adapter", "Query",
                       cfg_src.get("query") or "SELECT * FROM SQLUser.PatientSource"),
                    _s("Adapter", "KeyFieldName", cfg_src.get("key_field") or "ID"),
                    _s("Adapter", "JGService", "EnsLib.JavaGateway.Service"),
                    _s("Host", "TargetConfigNames", bp_name),
                ],
            }
        if ttype == "TransformProcess":
            # 转换 BP：统一用本管道给定的主机名（AI 给的名字不采用，保证一管道一实例）
            return _from_template(tpl, bp_name)
        if ttype == "JavaGateway":
            return {
                "type": "JavaGateway", "name": name or tpl["className"],
                "className": tpl["className"], "comment": tpl["comment"],
                "settings": [_s("Host", k, v) for k, v in tpl.get("settings", {}).items()],
            }
        if ttype == "SQLOperation":
            tbl = table or ""
            if not tbl and name:
                tbl = name[len("SQLOp_"):] if name.startswith("SQLOp_") else name
            if not tbl:
                for _m in mappings:
                    if _m.get("target_table"):
                        tbl = _m["target_table"]
                        break
            cols = get_table_columns(tbl, cfg_tgt) if tbl else []
            if not cols:
                logger.warning("目标表 %s 列结构为空，跳过 SQLOp（Agent B 表选择不可用）", tbl)
                return None
            col_sql = ", ".join(cols)
            q_marks = ", ".join("?" for _ in cols)
            return {
                "type": "SQLOperation", "name": name or f"SQLOp_{tbl}",
                "className": tpl["className"], "comment": f"写入目标表 {tbl}",
                "settings": [
                    _s("Adapter", "DSN", target_dsn(cfg_tgt)),
                    _s("Adapter", "JGService", "EnsLib.JavaGateway.Service"),
                    _s("Host", "Query",
                       f"INSERT OR UPDATE INTO {tbl} ({col_sql}) VALUES ({q_marks})"),
                    _s("Host", "InputParameters", ",".join(f"*{c}" for c in cols)),
                ],
            }
        if ttype == "SOAPOperation":
            service = cfg_tgt.get("service") or "default"
            bo_class = cfg_tgt.get("bo_class", "")
            imp = {}
            if cfg_tgt.get("wsdl"):
                imp = wsdl_importer.import_soap_operation(
                    cfg_tgt["wsdl"], service, cfg_tgt.get("packages"))
            if imp.get("ok") and imp.get("boClass"):
                bo_class = imp["boClass"]
            elif not bo_class:
                bo_class = wsdl_importer.get_bo_class(service)
            if not bo_class:
                logger.warning("SOAP 目标 bo_class 未找到（service=%s），跳过 SOAPOp", service)
                return None
            wsdl_importer._save_bo_mapping(service, bo_class)
            return {
                "type": "SOAPOperation", "name": name or f"SOAPOp_{service}",
                "className": bo_class,
                "comment": f"SOAP 调用 {service}（WSDL 导入 BO）",
                "settings": [_s("Adapter", "WebServiceURL",
                                cfg_tgt.get("endpoint") or Config.MOCK_SOAP_URL)],
            }
        if ttype == "HTTPOperation":
            base = cfg_tgt.get("base_url") or cfg_tgt.get("endpoint") or ""
            host, port = fhir_http_host_port(base)
            return {
                "type": "HTTPOperation", "name": name or "HTTPOperation",
                "className": tpl["className"], "comment": tpl["comment"],
                "settings": [
                    _s("Adapter", "HTTPServer", host),
                    _s("Adapter", "HTTPPort", port),
                ],
            }
        return _from_template(tpl, name or ttype)

    def _name_table(n: str) -> str:
        return n[len("SQLOp_"):] if n.startswith("SQLOp_") else n

    components: list[dict] = []
    supplemented: list[str] = []
    for ai in ai_components:
        ttype = ai.get("type", "")
        tpl = templates.get(ttype)
        if not tpl:
            logger.warning("Agent B 选择了未注册组件 type=%s，忽略", ttype)
            continue
        comp = build_component(tpl, name=ai.get("name"), table=ai.get("table"))
        if comp:
            components.append(comp)

    def add_if_missing(ttype: str, *, name: str | None = None, table: str | None = None):
        """AI 未提供该必需组件时按注册表模板补齐保底（不覆盖 AI 已有决策）。"""
        if any(c["type"] == ttype for c in components):
            return
        comp = build_component(templates[ttype], name=name, table=table)
        if comp:
            components.append(comp)
            supplemented.append(comp["type"])

    # 保底补齐（仅限确属必需而 AI 遗漏者；AI 已提供则尊重 AI 的命名/表）
    for c in type_registry.get_source_components(source_type):
        if not any(x["type"] == c["type"] for x in components):
            add_if_missing(c["type"])
    add_if_missing("TransformProcess", name=bp_name)
    if target_type == "DB":
        have_tables = {_name_table(c.get("name", "")).lower()
                       for c in components if c["type"] == "SQLOperation"}
        for m in mappings:
            tbl = m.get("target_table", "")
            if not tbl or tbl.lower() in have_tables:
                continue
            add_if_missing("SQLOperation", name=f"SQLOp_{tbl}", table=tbl)
            have_tables.add(tbl.lower())
        add_if_missing("SQLOperation")
    else:
        # 非 DB 目标（SOAP/FHIR 等）：按当前 target_type 的注册表目标组件模板补齐保底
        for tpl in type_registry.get_target_components(target_type):
            add_if_missing(tpl["type"])
    add_if_missing("JavaGateway")
    # 共享术语 BO：本次映射含 term_map 决策时挂上（术语转换能力的唯一出口；
    # 平台按需追加、不进 AI 可选枚举 —— 与 JavaGateway 同级的基础设施）
    if needs_terminology(mappings) and not any(
            c["type"] == "TerminologyOperation" for c in components):
        _term_comp = terminology_component()
        if _term_comp:
            components.append(_term_comp)
            supplemented.append("TerminologyOperation")
            logger.info("本次映射含 term_map 决策 → 追加共享术语 BO %s", _term_comp.get("name"))

    topology = {"production": "demo.DataflowProduction", "components": components}
    if supplemented:
        topology["ai_supplemented"] = supplemented
        logger.info("Agent B 已生成拓扑：AI 决策 %d 个组件，保底补齐缺失 %s",
                    len(components) - len(supplemented), supplemented)
    return topology


def dedupe_components(components: list[dict]) -> list[dict]:
    """组件按 name 去重合并；同名不同 className（真冲突）自动加序号后缀。"""
    seen: dict[str, dict] = {}
    out: list[dict] = []
    for c in components:
        name = c.get("name", "")
        prev = seen.get(name)
        if prev is not None:
            if prev.get("className") == c.get("className"):
                continue  # 完全同定义，去重
            n = 2
            while seen.get(f"{name}_{n}"):
                n += 1
            c = {**c, "name": f"{name}_{n}"}
        seen[c.get("name")] = c
        out.append(c)
    return out


def common_component(template: dict) -> dict:
    """按注册表模板构造共享组件（settings → Host）。"""
    settings = []
    for k, v in (template.get("settings") or {}).items():
        settings.append({"target": "Host", "name": k, "value": v})
    return {
        "type": template["type"],
        "name": template["className"] if template["type"] == "JavaGateway" else template["type"],
        "className": template["className"],
        "comment": template.get("comment", ""),
        "settings": settings,
    }


def setting_value(comp: dict, name: str) -> str:
    """取组件某个 setting 的值（任意 Target：Adapter/Host）。"""
    for st in (comp.get("settings") or []):
        if str(st.get("name")) == name:
            return str(st.get("value") or "")
    return ""


def dispatch_targets(comps: list[dict], rename: dict[str, str] | None = None,
                      target_config: dict | None = None) -> tuple[dict, str]:
    """本组「转换 BP → 目标 BO」的**显式主机名**（必须按实例名，不能靠按约定拼名）。

    背景（2026-09-17 实测缺陷 A6）：多管道并存时目标 BO 会按管道实例改名
    （`SQLOp_PatientSource` → `SQLOp_PatientSource__sql2db`），而 `demo.TransformProcess` 原按约定
    拼名（`SQLOp_{表}` / `SOAPOp_{服务}`）→ 指向不存在的主机 → `ErrBusinessDispatchNameNotRegistered`、
    目标零落地而消息状态看似正常（静默失败）。故生成端把实际主机名写进 BP 自己的配置：
    `bp[<BP名>] = {..., items:{<表|服务>: <主机名>}, item:<单一主机名>}`。

    返回 `(items, single)`：
    - `items`：键与 BP 查表口径一致（DB=目标表名、SOAP=服务名，取自**改名前**名 `SQLOp_{表}` /
      `SOAPOp_{服务}`；DB 名不合约定时从 Query 文本 `INTO <表>` 解析）→ 值 = 最终主机名；
    - `single`：本组 Operation 主机名（FHIR 目标用；多目标组取首个作为兜底）。
    查询 BO（`SELECT ...`，如 sql2fhir 的子表查询 BO）不是路由 BP 的派发目标，不进 items。
    """
    rename = rename or {}
    items: dict[str, str] = {}
    single = ""
    for c in comps or []:
        ctype = str(c.get("type") or "")
        old = str(c.get("name") or ctype)
        final = rename.get(old, old)
        if ctype == "SQLOperation":
            key = old[len("SQLOp_"):] if old.startswith("SQLOp_") else ""
            if not key:
                m = _INTO_RE.search(setting_value(c, "Query"))
                key = m.group(1).strip().strip('"') if m else ""
            if not key:
                continue                      # 查询 BO（SELECT）不是路由 BP 的派发目标
            items[key] = final
            single = single or final
        elif ctype == "SOAPOperation":
            key = old[len("SOAPOp_"):] if old.startswith("SOAPOp_") else ""
            if not key:
                key = str((target_config or {}).get("service") or "")
            if key:
                items[key] = final
            single = single or final
        elif ctype == "HTTPOperation":
            single = single or final
    return items, single


def assert_dispatch_targets(groups: list[dict], components: list[dict]) -> None:
    """生成前自检：转换 BP 的**显式派发目标**必须真实存在于本拓扑（防"改名后引用悬空"）。

    2026-09-17 实测：组件按管道实例改名后 BP 仍按约定拼名 → 派发到不存在的主机，
    消息看似正常（BS→BP 那跳 Completed）、目标零落地、错误只在 Ens_Util.Log 里
    （`ErrBusinessDispatchNameNotRegistered`）。此处把"引用悬空"提前到生成期显式失败。
    """
    names = {str(c.get("name")) for c in (components or []) if c.get("name")}
    missing: list[str] = []
    for g in groups or []:
        cands = list((g.get("_bp_items") or {}).values())
        if g.get("_bp_item"):
            cands.append(g["_bp_item"])
        for item in cands:
            if item and item not in names:
                missing.append("%s→%s" % (g.get("_category") or "?", item))
    if missing:
        raise ValueError("转换 BP 派发目标不存在（组件改名后引用悬空）: " + "; ".join(missing))


def config_subs(native, *path: str) -> list[str]:
    """枚举 `^demo.Config(<path...>)` 的下一级下标（Native SDK nextSubscript，顺序确定）。

    `path` 为空时枚举一级下标；多级（如 `"sql2fhir","layout"`）用于实例级布局键。
    """
    prefix = '^demo.Config(%s)' % ",".join('"%s"' % p for p in path)
    subs, s = [], native.nextSubscript(False, prefix, "")
    while s:
        subs.append(str(s))
        s = native.nextSubscript(False, prefix, s)
    return subs


def prune_stale_bp_config(keep_bps: set[str], keep_srcs: set[str],
                          dry_run: bool = False,
                          only_prefix: str | None = None) -> dict:
    """生成成功后收敛清理 `^demo.Config` 里**陈旧键**（无对应组件 = 死配置）。

    覆盖三类：
      - `bp[<BP名>]`                      → 键必须是当前 Production 里的组件名（否则没有读者）；
      - `bp_target[<源BS>]`               → 键必须是组件名，值必须指向存在的组件（否则悬空派发）；
      - `sql2fhir.layout[<聚合BP名>]`     → 键必须是组件名（该 BP 自己就是读者）。

    背景：单次生成会**整份替换** Production（`PipelineGenerator.GenerateProduction`）→ 本次生成
    之后的正确登记集合就是本次渲染出的 BP / 源 BS，其余键指向的组件已不存在、没有任何读者。
    原实现"只写不清"：Skill 自带聚合 BP 的组（sql2fhir）没有 TransformProcess 主机，却仍登记
    `bp[TransformProcess__sql2fhir_*]` 与 `bp_target[源BS]`（实测 4 条残留）—— 诊断工具据此误读
    「登记数 ≠ 组件数 = 生成不完整」，且悬空 `bp_target` 一旦被读到就是 A6 式静默派发失败。

    保护（fail-open，宁可少删）：
      - `last_good` / `last_good_at`（源码存档，非管道参数）；
      - `keep_bps` / `keep_srcs`（本次生成渲染出的 BP / 源 BS）；
      - **当前组件清单里仍存在**的同名主机（生成失败/部分渲染时绝不误删）。

    参数:
        keep_bps: 本次生成的转换 BP 主机名集合。
        keep_srcs: 本次生成的源 BS 主机名集合。
        dry_run: True 时**只报告不删除**（供手动清理工具 `--check`/体检使用）。
        only_prefix: 只处理**键名以该前缀开头**的键（默认 None = 处理全部，生产语义不变）。
            用途 = **自测沙箱化**：测试只允许清理自己造的前缀键（`__dead_test_`），避免顺带清掉
            环境里真实存在的同类死键（2026-09-21 实测：测试顺带删掉了 `bp[TransformProcess__sql2soap]`）。

    返回:
        {"removed": {"bp": [...], "bp_target": [{"key","value"}...], "layout": [...]},
         "skipped": <原因，可选>}
    """
    removed: dict = {"bp": [], "bp_target": [], "layout": []}
    protected = set(_BP_CONFIG_PROTECTED)
    try:
        raw = iris_connector.class_method_value("demo.PipelineQuery", "GetItems") or "[]"
        comps = {str((i or {}).get("name") or "") for i in (json.loads(raw) or [])}
    except Exception as exc:  # noqa: BLE001 - 读不到事实就不动配置（fail-open）
        logger.warning("配置收敛：组件清单不可读，跳过清理（%s）", exc)
        return {"removed": removed, "skipped": "组件清单不可读: %s" % exc}

    import iris
    conn = iris_connector.get_connection()
    try:
        native = iris.createIRIS(conn)
        for key in config_subs(native, "bp"):
            if only_prefix and not str(key).startswith(only_prefix):
                continue
            if key in protected or key in keep_bps or key in comps:
                continue
            if not dry_run:
                native.kill("^demo.Config", "bp", key)
            removed["bp"].append(key)
        for key in config_subs(native, "bp_target"):
            if only_prefix and not str(key).startswith(only_prefix):
                continue
            val = str(native.get("^demo.Config", "bp_target", key) or "")
            if key in keep_srcs or (key in comps and val in comps):
                continue
            if not dry_run:
                native.kill("^demo.Config", "bp_target", key)
            removed["bp_target"].append({"key": key, "value": val})
        # 实例级布局 `^demo.Config("sql2fhir","layout",<聚合BP名>)`：读者是**该 BP 自己**
        # （运行期 `..%ConfigName` 查自己那份）→ 组件不在位 = 无读者，与 bp 同属死配置。
        # ⚠ 只清**带下标的实例键**，不动全局兜底键 `^demo.Config("sql2fhir","layout")`（历史兼容路径仍在读）。
        for key in config_subs(native, "sql2fhir", "layout"):
            if only_prefix and not str(key).startswith(only_prefix):
                continue
            if key in keep_bps or key in comps:
                continue
            if not dry_run:
                native.kill("^demo.Config", "sql2fhir", "layout", key)
            removed["layout"].append(key)
    except Exception as exc:  # noqa: BLE001 - 收敛失败不阻断生成（已生成事实不变）
        logger.warning("配置收敛失败（不影响本次生成）: %s", exc)
        return {"removed": removed, "error": str(exc)}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - 关闭失败可忽略
            pass
    if removed["bp"] or removed["bp_target"] or removed["layout"]:
        logger.warning("配置收敛：%s陈旧登记 %s（对应组件已不存在 → 无读者）",
                       "检出（未删除，dry_run）" if dry_run else "清理",
                       json.dumps(removed, ensure_ascii=False))
    return {"removed": removed, "dry_run": bool(dry_run)}


def unkeyed_operations(comps: list[dict]) -> list[str]:
    """本组中「解析不出派发键」的 DB/SOAP Operation 组件名（生成期必须为空）。

    路由 BP 对 DB/SOAP 目标按**键**（目标表名 / 服务名）查派发表；键取不到就只能按约定拼名，
    而多管道改名后该拼名并不存在 → 运行期红消息 + 目标零落地（缺陷 A6 的静默面）。
    故生成期显式拦下：要么组件名 `SQLOp_{表}` / `SOAPOp_{服务}`，要么 Query 文本含 `INTO {表}`。
    """
    bad: list[str] = []
    for c in comps or []:
        ctype = str(c.get("type") or "")
        old = str(c.get("name") or ctype)
        if ctype == "SQLOperation":
            if old.startswith("SQLOp_"):
                continue
            if _INTO_RE.search(setting_value(c, "Query")):
                continue
            if "SELECT" in setting_value(c, "Query").upper():
                continue                       # 查询 BO（SELECT）：不是路由 BP 的派发目标
            bad.append(old)
        elif ctype == "SOAPOperation" and not old.startswith("SOAPOp_"):
            bad.append(old)
        elif ctype not in ("SQLOperation", "SOAPOperation", "HTTPOperation", "TransformProcess",
                           "SQLService", "FHIRService", "PatientTxProcess", "JavaGateway"):
            if ctype.endswith("Operation"):
                bad.append(old)
    return bad


def auto_join_existing_pipelines(submitted: list[dict],
                                  submitted_ids: set[str]) -> list[dict]:
    """**把未提交的既有管道按存储定义并入本次生成**（增量生成的关键保障）。

    单一 Production 架构下，一次生成会**整份替换**渲染源（`^demo.Config("pipeline","topology")`
    → 重写整个 `demo.DataflowProduction`）；若只提交"新增/变更组"，未提交的既有管道会从
    Production 里**消失**（历史行为：单管道生成 = 替换整份）。故这里把它们的**已存组件定义**
    自动并入：

    · 输入签名与存储一致 → 标 `_unchanged`（复用组件、跳过 Agent B/C1，运行态按 P2 保留）；
    · 签名不一致（映射被改过）→ 作为"变更组"参与本次生成（自愈，不静默用旧组件）；
    · `superseded`（组件已被取代/映射已不存在）→ **不并入**（避免复活幽灵管道）。
    """
    out: list[dict] = []
    try:
        recs = pipeline_instances.list_instances()
        all_maps = repository.list_mappings()
    except Exception as exc:  # noqa: BLE001 - 读不到既有管道 → 退化为"本次提交即全集"
        logger.warning("增量生成：读取既有管道失败（跳过自动并入）: %s", exc)
        return out
    for rec in recs:
        pid = str(rec.get("id") or "")
        if pid in submitted_ids or rec.get("status") == "superseded":
            continue
        comps = rec.get("ai_components") or []
        if not comps:
            continue
        ids = {str(x) for x in (rec.get("mapping_ids") or [])}
        maps = [m for m in all_maps if str(m.get("id")) in ids]
        if not maps:
            logger.warning("增量生成：既有管道 %s 的映射已不存在 → 不并入（需重新匹配后生成）", pid)
            continue
        src_cfg, tgt_cfg = configs_for_identity(rec.get("source_id"), rec.get("target_id"))
        g = {"source_type": rec.get("source_type") or "", "target_type": rec.get("target_type") or "",
             "source_id": rec.get("source_id"), "target_id": rec.get("target_id"),
             "source_config": src_cfg, "target_config": tgt_cfg, "mappings": maps,
             "design_skill": rec.get("design_skill"), "ai_components": comps,
             "_sql2fhir": bool(rec.get("is_sql2fhir")), "_auto_joined": True}
        if rec.get("layout"):
            g["_layout"] = rec.get("layout")
        try:
            g["_rt_src"] = repository.datasource_runtime(repository.get_datasource(rec.get("source_id")))
            g["_rt_tgt"] = repository.target_runtime(repository.get_target(rec.get("target_id")))
        except Exception:  # noqa: BLE001
            g["_rt_src"], g["_rt_tgt"] = None, None
        g["_sig_in"] = inc_input_signature(g)
        # 冻结定义（复用渲染用：参数完整 + 改名前名字）
        g["_frozen_components"] = frozen_defs_from_instance(rec)
        g["_frozen_infra"] = [dict(c) for c in comps
                              if isinstance(c, dict) and is_infra_component(c)]
        _ok_def, _why = stored_definition_complete(
            frozen_defs_from_instance(rec), rec.get("target_type"))
        if str(rec.get("signature") or "") == g["_sig_in"] and _ok_def:
            g["_unchanged"] = True
        elif not _ok_def:
            logger.warning("增量生成：管道 %s 的存储定义不完整（%s）→ 本次按**变更组**重新生成"
                           "（重建完整组件定义 = 自愈）", pid, _why)
        else:
            logger.info("增量生成：既有管道 %s 的输入已变 → 本次按**变更组**重新生成", pid)
        out.append(g)
    if out:
        logger.info("增量生成：未提交的既有管道自动并入 %d 条（其中复用 %d 条）：%s",
                    len(out), sum(1 for x in out if x.get("_unchanged")),
                    [x.get("source_id") + "→" + str(x.get("target_id")) for x in out])
    return out


def write_mapping_patch(m: dict) -> None:
    """把**映射修正结果**写回 `^demo.Mapping` —— 以**库存记录为底合并**（按 id 取库存）。

    为什么必须合并写（2026-09-19 P0）：C1/L2 或 LLM 产出的映射 dict 可能**缺字段**
    （最典型是 `source_id` 数据源维度）→ 整条覆盖会把已登记信息静默抹掉 → 之后
    `save_mappings` 判定身份不同 → **每次生成派生重复映射**（实测 `M714233` → `_2` → `_3`…）。
    合并写保证「修正的是内容，不动身份与其它登记字段」。
    """
    if not isinstance(m, dict) or not m.get("id"):
        return
    mid = str(m["id"])
    cur = repository.get_json("^demo.Mapping", mid) or {}
    merged = {**cur, **m}
    merged["id"] = mid
    repository.set_json("^demo.Mapping", mid, merged)


def group_components(g: dict) -> list[dict]:
    """组的**预改名**组件定义（AI/executor 产出，含基础设施）——增量注入与存量校验共用。"""
    return [dict(c) for c in (g.get("ai_components") or []) if isinstance(c, dict)]


def build_multi_pipeline_topology(pipelines: list[dict]) -> dict:
    """按「多管道组」构建合并拓扑（单 Production 内每管道一套 BS/BP/BO + 共享基础设施）。

    **一条数据管道一个转换 BP**（Ens 组件身份 = Ens.Config.Item 的 Name，class 可复用）：
    - 每组给自己的 BP 取名 `TransformProcess__{管道类别}`（同类别多组时加序号），
      源 BS 的 TargetConfigNames 指向**本组自己的 BP** → 管道之间零耦合：
      停一条管道不会带动另一条，也不会出现「共享 BP 被豁免启停 → 许可不放」；
    - 自带聚合 BP 的 Skill 组（sql2fhir-patient-tx 的 SqlFhirPatientTxProcess）不生成它；
    - 真正跨管道共享的只剩基础设施 **JavaGateway**（JDBC 网关，多管道共用 1 个实例）。

    源 BS 命名加固（消除「名字漂移」）：源 BS 同名跨多组冲突时，**按管道类别统一改名**为
    `{原名}__{类别}`（如 SQLService_Patient__sql2soap）——与组顺序无关，重复生成名字恒定；
    未冲突的组保持原名。
    """
    def _bp_name_for(cat: str, taken: set[str]) -> str:
        """本组转换 BP 的主机名（`TransformProcess__{类别}`，重复类别加序号，结果确定）。"""
        base = f"TransformProcess__{pipeline_instances.slug(cat) or 'pipeline'}"
        name, k = base, 2
        while name in taken:
            name = f"{base}_{k}"
            k += 1
        taken.add(name)
        return name

    def group_components(g: dict, bp_name: str) -> tuple[list[dict], bool, list[str]]:
        """生成单组组件（剔除基础设施 JavaGateway，稍后统一追加）+ 该组是否需要转换 BP。

        返回 (own_components, needs_router, supplemented)：needs_router 依据**本组原始拓扑**
        是否含 TransformProcess 判定——通用组（源 BS 的 TargetConfigNames 指向它）需要；
        Skill 自带 BP 的组不需要（本组拓扑里根本没有它）。本组自己的 BP 属本组组件，
        不再当共享件剔除。supplemented = 本组 Agent B 遗漏后**注册表保底补齐**的组件类型
        （红线审计：必须按组可区分，不能与 AI 决策混在一起）。
        """
        if g.get("_sql2fhir"):
            # sql2fhir 组：拓扑由 Skill executor 按布局生成（含 Agent 编译的 BP），直接采用
            topo = {"production": "demo.DataflowProduction",
                    "components": g.get("ai_components") or []}
        elif g.get("_unchanged") and (g.get("_frozen_components") or []):
            # —— 增量复用（P1）：渲染**冻结定义**（上一轮渲染成功的参数 + 改名前名字）——
            # 直通 Agent 原始 `ai_components`（无 className/settings）会被拓扑校验拒（500）；
            # 走注册表参数化链路又会丢目标 BO（实测 2026-09-20）。冻结定义两头都对。
            # ⚠ **必须把源 BS 的投递目标改写成"本次"的 BP 名**：同类别多管道并存时本组的 BP 会被
            #   重命名为 `…__{类别}_{k}`，而冻结定义里记的是上一轮的名字（可能属于**另一条**管道）
            #   → 消息投给别的（可能停用的）BP → 永久 `Queued`（实测 2026-09-20：S2 的源 BS 指向
            #   `TransformProcess__sql2soap`（S3 组的、已停用），而自己那台叫 `..._2`）。
            _frozen = [dict(c) for c in g["_frozen_components"]]
            for _c in _frozen:
                _sts = _c.get("settings")
                if not isinstance(_sts, list):
                    continue
                for _s in _sts:
                    if str(_s.get("name")) == "TargetConfigNames" and bp_name:
                        _s["value"] = bp_name
            topo = {"production": "demo.DataflowProduction",
                    "components": _frozen + [dict(c) for c in (g.get("_frozen_infra") or [])]}
        else:
            topo = build_pipeline_topology(
                g.get("mappings") or [],
                source_type=g.get("source_type") or "FHIR",
                target_type=g.get("target_type") or "DB",
                suggested_types=g.get("suggested_types"),
                source_config=g.get("source_config") or {},
                target_config=g.get("target_config") or {},
                ai_components=g.get("ai_components"),
                bp_name=bp_name)
        raw = topo.get("components") or []
        needs_router = any(str(c.get("type") or "") == "TransformProcess" for c in raw)
        supplemented = [str(x) for x in (topo.get("ai_supplemented") or [])]
        # 组内拓扑携带的基础设施（JavaGateway / FHIRSyncService 等）**不属于任何一条管道**：
        # 从本组组件里剔除，但必须收集起来由外层**全局追加一次**——否则会整个从 Production 消失
        # （2026-09-16 实测缺陷：修 J 把 FHIRSyncService 归为 infra 后忘了追加，
        #   结果源侧「拉取+入队」生产者不存在 → 所有 FHIR 管道零消息、校验全 FAIL）。
        for c in raw:
            if not is_infra_component(c):
                continue
            if not any(str(c.get("name")) == str(x.get("name")) for x in infra_seen):
                infra_seen.append(c)
        return [c for c in raw if not is_infra_component(c)], needs_router, supplemented

    # 基础设施收集桶（见 group_components 内注释）：跨管道共享、全局仅 1 个实例
    infra_seen: list[dict] = []

    categories = [str(g.get("_category") or pipeline_instances.category_of(
        g.get("design_skill"), g.get("source_type"), g.get("target_type")))
        for g in pipelines]
    # 每组一个专属 BP（按管道类别命名；同类别多组自动加序号）
    taken_bp: set[str] = set()
    bp_names = [_bp_name_for(cat, taken_bp) for cat in categories]
    group_parts = [group_components(g, bp)
                   for g, bp in zip(pipelines, bp_names)]
    group_comps = [p[0] for p in group_parts]
    need_router = any(p[1] for p in group_parts)
    # 每组的「注册表保底补齐」清单落回组上（管道实体的 ai.supplemented 必须按组归属，
    # 否则多管道提交时该标记会丢失 → 规则补齐被误报成纯 AI 决策）
    for g, part in zip(pipelines, group_parts):
        if part[2]:
            g["_ai_supplemented"] = part[2]
    # 组件名**跨组冲突 → 按管道实例改名**（`{原名}__{类别}`，同类别多组再 `_k`）：
    # ⚠ 不止源 BS —— Skill 自带的聚合 BP 及其 BO（`SqlFhirPatientTxProcess` / `HTTPOperation` /
    #   `SQLQueryOp_*`）同样必须"一管道一实例"：3 源并存时两个 sql2fhir 组会共用同一个 BP 实例，
    #   而该 BP 的布局取自**实例级**配置（`^demo.Config("sql2fhir","layout",<BP名>)`）→ 两条管道串线
    #   （2026-09-17 实测：S2 的 USER 源行被按 S3 的 CLINIC 布局组装）。
    #   改名同时改写组内引用（源 BS 的 TargetConfigNames 指向本组 BP）。
    name_counts: dict[str, int] = {}
    for comps in group_comps:
        for c in comps:
            _n = str(c.get("name") or c.get("type") or "")
            name_counts[_n] = name_counts.get(_n, 0) + 1

    components: list[dict] = []
    used_names: set[str] = set()
    for gi, (comps, cat) in enumerate(zip(group_comps, categories)):
        src_names: list[str] = []
        own_names: list[str] = []
        agg_bp = ""
        slug = pipeline_instances.slug(cat) or "pipeline"
        # 先算本组的改名映射（一次性决定，避免组内引用与宿主名不一致）
        rename: dict[str, str] = {}
        for c in comps:
            nm = str(c.get("name") or c.get("type") or "")
            if name_counts.get(nm, 0) <= 1 or nm in rename:
                continue
            base = f"{nm}__{slug}"
            new_nm, k = base, 2
            while new_nm in used_names or new_nm in rename.values():
                new_nm = f"{base}_{k}"
                k += 1
            rename[nm] = new_nm
        # 派发目标（**改名前的名**参与推导，值取最终名）：转换 BP 不再靠按约定拼名，
        # 保证"组件改名后引用不悬空"（缺陷 A6）。
        _bp_items, _bp_item = dispatch_targets(
            comps, rename, pipelines[gi].get("target_config"))
        # 本组走通用路由时，DB/SOAP 目标的**派发键必须可解析**（否则 BP 会按不存在的拼名派发）
        if group_parts[gi][1]:
            _bad = unkeyed_operations(comps)
            if _bad:
                raise ValueError(
                    "管道 %s 的 DB/SOAP 目标无法确定派发键（组件名既非 SQLOp_{表}/SOAPOp_{服务}，"
                    "Query 也没有 INTO {表}）: %s —— 路由 BP 会按不存在的拼名派发（目标零落地）"
                    % (cat, "、".join(_bad)))
        for c in comps:
            ctype = str(c.get("type") or "")
            nm = str(c.get("name") or ctype)
            if nm in rename:
                new_nm = rename[nm]
                logger.info("组件名跨管道冲突 → 按管道实例命名: %s -> %s", nm, new_nm)
                c["name"] = new_nm
                nm = new_nm
                for st in (c.get("settings") or []):
                    if str(st.get("name")) == "TargetConfigNames" and st.get("value"):
                        st["value"] = ",".join(
                            rename.get(str(p).strip(), str(p).strip())
                            for p in str(st["value"]).split(","))
            c["category"] = cat
            used_names.add(nm)
            own_names.append(nm)
            if ctype in ("SQLService", "FHIRService"):
                src_names.append(nm)
            elif ctype == "PatientTxProcess":
                agg_bp = nm
            components.append(c)
        # 本组**专属组件名**（供管道实体精确归属：同类别多组时不能按 category 取，否则
        # 两条同类别管道会共用一份组件清单 → 一键切换会连带启停另一条，2026-09-17 实测）
        pipelines[gi]["_own_names"] = own_names
        # 该组实际源 BS 名（供路由表 ^demo.Config("pipe", <BS名>) 使用，必须与拓扑一致）
        if src_names:
            pipelines[gi]["_src_bn"] = src_names[0]
        # 本组专属转换 BP 名：**仅当本组真的有 TransformProcess 主机时**才登记
        # （group_parts[gi][1] = needs_router）。Skill 自带聚合 BP 的组（sql2fhir-patient-tx）
        # 拓扑里没有该主机 → 原实现无条件登记 `bp[TransformProcess__sql2fhir_*]` +
        # `bp_target[源BS]` 却没有任何读者（死配置，2026-09-17 实测残留 4 条）。
        # ⚠ 这里必须 gate：`_bp_name` 同时是生成端「写 bp[<名>]」与生成后「收敛清理」的
        #   **keep 名单** —— 登记了不存在的名字，清理器会把死键当有效键保留下来。
        pipelines[gi]["_has_router"] = bool(group_parts[gi][1])
        if pipelines[gi]["_has_router"]:
            pipelines[gi]["_bp_name"] = bp_names[gi]
        # 派发目标（items / item）：**始终登记**（与"写不写 global"是两件事）——它是
        # 「生成期悬空自检 `assert_dispatch_targets`」与「布局 http_bo 兜底」的输入；
        # sql2fhir 组没有路由 BP，但其 FHIR 派发名仍必须落进实例级布局 `layout.http_bo`。
        pipelines[gi]["_bp_items"] = _bp_items
        if _bp_item:
            pipelines[gi]["_bp_item"] = _bp_item
        # sql2fhir 组：布局里的**派发名**同步改成最终（改名后）组件名——聚合 BP 运行时按
        # `layout.query_bos[].bo_name` / `layout.http_bo` 派发，布局与组件名必须一致
        # （否则同样是"引用悬空"，且只在运行期以红消息暴露）。
        _lay = pipelines[gi].get("_layout")
        if isinstance(_lay, dict):
            for _b in (_lay.get("query_bos") or []):
                _old_bo = str((_b or {}).get("bo_name") or "")
                if _old_bo in rename:
                    _b["bo_name"] = rename[_old_bo]
            _hb = str(_lay.get("http_bo") or "")
            if _hb in rename:
                _lay["http_bo"] = rename[_hb]
            elif not _hb and _bp_item:
                _lay["http_bo"] = _bp_item
        # 本组聚合 BP 名（sql2fhir Skill 组；供按实例写布局 ^demo.Config("sql2fhir","layout",<BP名>)）
        if agg_bp:
            pipelines[gi]["_agg_bp_name"] = agg_bp
    components = dedupe_components(components)
    # 基础设施（真正跨管道共享，各 1 个）：JavaGateway（JDBC 网关）；
    # 转换 BP 不再共享——每组在自己的拓扑里各有一个专属实例（见 _bp_name_for）。
    for t in type_registry.get_common_components():
        if t["type"] == "JavaGateway":
            comp = common_component(t)
            comp["category"] = "shared"
            components.append(comp)
    # 共享术语 BO（术语转换能力的唯一出口）：本次任何一组含 term_map 决策就全局挂 1 个实例
    # （category=shared → 不参与许可调度、不被按类别让路停用；各管道的 BP 共用它取判码）。
    _all_mappings = [m for g in pipelines for m in (g.get("mappings") or [])]
    if needs_terminology(_all_mappings):
        _term_comp = terminology_component()
        if _term_comp:
            components.append(_term_comp)
            logger.info("本次含 term_map 决策 → 追加共享术语 BO %s", _term_comp.get("name"))
    # 组内拓扑自带的基础设施（如 FHIR 源的 FHIRSyncService 生产者）：**全局追加一次**，
    # 归 shared（不参与许可调度、不被按类别让路停用，修 J 语义）。
    for comp in infra_seen:
        comp["category"] = "shared"
        components.append(comp)
    components = dedupe_components(components)
    if not need_router:
        logger.info("本次拓扑无组走通用路由 → 不生成转换 BP（无需该组件，省 1 个许可单元）")
    # 生成期自检：派发目标必须真实存在（改名后引用悬空 → 运行期红消息 + 目标零落地，静默）
    assert_dispatch_targets(pipelines, components)
    return {"production": "demo.DataflowProduction",
            "categories": categories, "components": components}


def needs_terminology(mappings: list[dict] | None) -> bool:
    """本次映射里是否存在术语转换决策（`transform=term_map:<skill>`）→ 是否要挂共享术语 BO。

    判据与 C1/运行期同源（都读 field_mappings[].transform），避免"AI 决策了术语转换、
    但运行期没有可用的术语能力"这种静默缺口。
    """
    for m in mappings or []:
        for fm in (m or {}).get("field_mappings") or []:
            if str((fm or {}).get("transform") or "").startswith("term_map:"):
                return True
    return False


def terminology_component() -> dict | None:
    """共享术语 BO 组件（`demo.TerminologyOperation`，全局 1 实例，category 由 _stamp/追加处置 shared）。"""
    tpl = next((c for c in type_registry.get_common_components()
                if c.get("type") == "TerminologyOperation"), None)
    if not tpl:
        return None
    comp = common_component(tpl)
    comp["category"] = "shared"
    return comp


def is_infra_component(comp: dict) -> bool:
    """基础设施组件判定（真正跨管道共享、全局仅 1 个实例）：JavaGateway（JDBC 网关）
    + **FHIRSyncService（FHIR 源增量拉取/入队生产者）**
    + **TerminologyOperation（术语转换共享 BO，2026-09-18 起）**。

    注意：转换 BP（TransformProcess）**不是**共享组件——Ens 的业务主机身份 = Item 名
    （className 可复用），每条数据管道各有一个专属 BP 实例（如 TransformProcess__sql2soap），
    随其管道一起启停/让路，不再靠 `shared` 豁免（否则停管道时许可不放）。

    FHIRSyncService 归属（2026-09-16 修 J，实测缺陷）：它把 FHIR **源**的新资源按
    「全部 mapping」写入全局 FHIRQueue（每个组各自的源 BS 再按 `^demo.Config("bs_mappings")`
    只消费自己那份），因此**一条 Production 里只需要一个实例**。原先它被算进"第一个用到它的组"
    （实测 category=fhir2db）→ 用「一键切换」启用另一条 FHIR 管道（fhir2fhir）时，
    fhir2db 整类让路把**生产者一起停用** → 新启用的组拿不到任何输入（消息一条都不来，静默失败）。
    故与 JavaGateway 同级：category=shared、不参与许可调度、不被按类别让路停用。

    TerminologyOperation 归属（2026-09-18）：术语转换能力做成**共享 BO**（任何管道的 BP 都
    经它取判码，运行期不再有本地码表副本）→ 同样全局 1 实例、category=shared、
    不参与许可调度、不被按类别让路停用（否则切换管道会把术语能力一起停掉 → 运行期静默缺第二 coding）。
    """
    ttype = str((comp or {}).get("type") or "")
    cls = str((comp or {}).get("className") or "")
    return (ttype == "JavaGateway" or "JavaGateway" in cls
            or ttype == "FHIRSyncService" or "FHIRSyncService" in cls
            or ttype == "TerminologyOperation" or "TerminologyOperation" in cls)


def pipeline_categories(*categories) -> list[str]:
    """本次生成的管道类别集合（许可预算 keep 名单；shared 由 IRIS 侧自动保留）。"""
    return [c for c in dict.fromkeys(str(x) for x in categories if x)]


def topology_groups(topology: dict) -> list[dict]:
    """按组件 Category 聚合「管道分组」（许可调度粒度；基础设施 JavaGateway 不参与预算）。

    返回 [{"category", "items": [业务主机名, ...]}]，顺序 = 拓扑中组件首次出现顺序
    （即用户提交的分组顺序），保证调度结果确定（先进先服务）。
    """
    order: list[str] = []
    buckets: dict[str, list[str]] = {}
    for c in (topology or {}).get("components") or []:
        if is_infra_component(c):
            continue
        cat = str((c or {}).get("category") or "unknown")
        nm = str((c or {}).get("name") or "").strip()
        if not nm:
            continue
        if cat not in buckets:
            buckets[cat] = []
            order.append(cat)
        if nm not in buckets[cat]:
            buckets[cat].append(nm)
    return [{"category": cat, "items": buckets[cat]} for cat in order]


def mark_suspended_components(topology: dict, plan: dict) -> list[str]:
    """把被许可调度停用的分组组件标 `enabled=False`（渲染为 Ens Item Enabled="false"）。

    「许可调度」的落地点：放不下的分组**组件照旧生成**（Production 里可查、可供验证），
    只是初始为停用状态（不占许可单元），用户在 UI 一键切换即可启用。返回被标记的组件名。
    """
    cats = {str(c) for c in (plan.get("suspended") or [])}
    marked: list[str] = []
    for c in (topology or {}).get("components") or []:
        if is_infra_component(c):
            continue
        if str((c or {}).get("category") or "") in cats:
            c["enabled"] = False
            nm = str((c or {}).get("name") or "")
            if nm:
                marked.append(nm)
    return marked


def converge_component_enabled(topology: dict, plan: dict, unchanged_cats: set,
                                suspended_cats: set) -> list:
    """按**许可调度结论**收敛每个组件的 `enabled`（**保证组内一致**）。

    规则（2026-09-20 两次实测缺陷后固化）：
      ① 被调度停用的组（`plan["suspended"]`）→ **该组所有组件一律停用**：不允许残留半启用的
         BO/BP 白占许可（曾出现 `SQLService_*`=0 而 `TransformProcess`/`SOAPOp_*`=1）；
      ② 未变更（复用）且**其实例本就是 suspended**（= 用户此前的选择）→ 保持停用（不因本次调度复活）；
      ③ 其余 → **遵循调度结论**（调度说启用就启用）。
    ⚠ 不再"按逐组件历史快照回填"：历史快照可能是上一轮失败/半渲染的残留（实测把调度判定应启用的
      组又按快照停掉 → 两组全停）。
    返回被改动的项（供日志审计）。
    """
    susp = {str(c) for c in (plan.get("suspended") or [])}
    changed: list = []
    for c in (topology.get("components") or []):
        if not isinstance(c, dict):
            continue
        cat = str(c.get("category") or "")
        if cat in susp:
            if bool(c.get("enabled", True)):
                changed.append("%s→停用(调度)" % c.get("name"))
            c["enabled"] = False
            continue
        if cat in unchanged_cats and cat in suspended_cats:
            if bool(c.get("enabled", True)):
                changed.append("%s→停用(用户此前停用)" % c.get("name"))
            c["enabled"] = False
    return changed


def license_plan(topology: dict) -> dict:
    """本拓扑的许可容量调度计划（超容量的分组停用而非失败，见 schedule_groups）。"""
    return pipeline_validator.schedule_groups(topology_groups(topology))


def term_catalog_brief(gate: dict) -> dict:
    """术语门禁/目录摘要（生成响应与前端展示用）。"""
    gate = gate or {}
    return {"jobs": gate.get("jobs") or [], "pairs": gate.get("pairs") or [],
            "covered": int(gate.get("covered") or 0),
            "negative": int(gate.get("negative") or 0),
            "missing": gate.get("missing") or [], "pending": gate.get("pending") or [],
            "unresolved": gate.get("unresolved") or [],
            "namespaces": gate.get("namespaces") or {}, "notes": gate.get("notes") or [],
            "catalogOk": bool(gate.get("catalogOk")), "error": gate.get("error") or "",
            "skipped": gate.get("skipped") or ""}


def term_precheck(mappings: list[dict] | None, *, pause_items: list[str] | None = None,
                   strict: bool = False) -> dict:
    """**术语映射生成期盘点**（术语服务器 = 唯一事实源；**只读，不写缓存**）。

    行为（2026-09-18 口径，用户确认）：
      ① 盘点：把映射里 `term_map:<skill>` 涉及的源表实际编码值送 `/mapping/availability` →
         covered / negative / missing（**待办清单**）/ pending / unresolved；
      ② **默认放行**：缺映射**不中止生成**（真实世界形态：数据先落地，术语缺口走"待办 + AI 补录"治理），
         运行期由**共享 BO** 命中 `missing` 时默认降级（保留源编码 + `meta.tag=unmapped`，不静默）；
      ③ **严格模式**（`strict=True`，请求体 `strict_terms=true`）：缺映射 → `ok=False`，调用方 400。

    参数 ``pause_items`` 保留仅为调用方兼容（本函数已不再暂停组件：不写缓存就没有许可压力）。

    返回 ``{"ok", "skipped", "strict", "gate", "todo", "message"}``：``skipped=True`` 表示本次没有任何
    term_map 决策（完全不触发术语服务器，避免影响无关管道）。
    """
    from backend.services import term_precheck

    try:
        gate = term_precheck.precheck(mappings or [])
    except Exception as exc:  # noqa: BLE001 - 盘点异常也不阻断（默认口径）：显式告警 + 记账
        logger.error("术语盘点异常（默认放行，运行期将降级）: %s", exc, exc_info=True)
        return {"ok": not strict, "skipped": False, "strict": strict, "gate": {}, "todo": [],
                "message": (f"术语盘点失败（术语服务器/IRIS 不可用？）: {exc}"
                            if strict else
                            f"术语盘点失败（默认放行，运行期按降级处理）: {exc}")}
    if not (gate.get("jobs") or []):
        return {"ok": True, "skipped": True, "strict": strict, "gate": gate, "todo": [],
                "message": ""}
    todo = list(gate.get("todo") or [])
    if strict and not gate.get("ok"):
        msg = term_precheck.gate_message(gate) or "术语预检未通过（严格模式）"
        logger.error("术语严格预检未通过: %s", msg)
        return {"ok": False, "skipped": False, "strict": True, "gate": gate, "todo": todo,
                "message": msg}
    msg = term_precheck.degrade_message(gate)
    if msg:
        logger.warning("术语盘点：%s", msg)
    return {"ok": True, "skipped": False, "strict": strict, "gate": gate, "todo": todo,
            "message": msg}


def term_summary(mappings: list[dict] | None, topology: dict | None,
                  validation: dict | None) -> dict:
    """生成后**复核术语覆盖**（只读盘点；不写缓存、不调 LLM、不阻断）。

    运行期由**共享 BO** `demo.TerminologyOperation` 实时查术语服务器：
    `active` → 追加目标体系 coding（双 coding）；`negative` → 不追加；**`missing`/`error` → 默认降级**
    （保留源编码 + `meta.tag=unmapped`，不静默）。故这里只做**事实复核 + 待办清单 + 告警**：

    - 缺映射（待办）→ 验证 warning（不判失败：真实世界形态就是"先落地、后补录"）；
    - 源表此刻无编码值但确有 term_map 决策 → warning（提示"造数后即可自动生效"，无需重生成）。

    返回 ``{"ok","jobs","todo","missing","covered","negative","gate","note"}``。
    """
    from backend.services import term_precheck

    try:
        gate = term_precheck.precheck(mappings or [])
    except Exception as exc:  # noqa: BLE001 - 复核异常不阻断（生成已完成），显式告警
        logger.warning("术语复核异常（不影响生成）: %s", exc)
        return {"ok": False, "jobs": [], "todo": [], "missing": [], "covered": 0, "negative": 0,
                "gate": {}, "error": str(exc)[:300],
                "note": "术语复核异常；运行期由共享 BO 实时查询（缺映射会降级并打 meta.tag）"}

    jobs = list(gate.get("jobs") or [])
    if not jobs:
        return {"ok": True, "jobs": [], "todo": [], "missing": [], "covered": 0, "negative": 0,
                "gate": term_catalog_brief(gate), "skipped": gate.get("skipped") or "",
                "note": "本次无 term_map 决策（不触发术语能力）"}

    todo = list(gate.get("todo") or [])
    covered = int(gate.get("covered") or 0)
    negative = int(gate.get("negative") or 0)
    if todo:
        msg = term_precheck.degrade_message(gate)
        logger.warning("术语待办（不影响生成，运行期降级）: %s", msg or todo[:6])
        if isinstance(validation, dict):
            validation.setdefault("issues", []).append(
                {"severity": "warning", "check": "term_map_todo", "message": msg})
    if covered == 0 and negative == 0:
        msg = ("存在 term_map 术语映射决策，但源表现在没有编码值可盘点 → 造数后运行期由共享 BO "
               "实时查询即生效（**无需重新生成**）")
        logger.warning("术语盘点为空：%s", msg)
        if isinstance(validation, dict):
            validation.setdefault("issues", []).append(
                {"severity": "warning", "check": "term_map_todo", "message": msg})
    return {"ok": not todo, "jobs": jobs, "todo": todo, "missing": todo,
            "covered": covered, "negative": negative,
            "gate": term_catalog_brief(gate),
            "note": ("术语转换运行期由**共享 BO** demo.TerminologyOperation 实时查术语服务器："
                     "active→追加目标体系 coding（双 coding）；negative→不追加（服务器判定无匹配）；"
                     "missing/error→**默认降级**（保留源编码 + meta.tag=unmapped，不静默）；"
                     "todo=服务器尚无该转换的源编码（补录：tools/term_map_build.py，补录后无需重生成）")}


def missing_patient_root_hint(mappings: list[dict] | None, group_source_id: str | None,
                               target_type: str = "FHIR",
                               entity: str = "Patient") -> str:
    """「本组缺患者主表」时给出**可照做的提示**（只报登记事实，不做任何决策）。

    背景（2026-09-19 实测）：两个数据源都有 `SQLUser.Patient`（USER 与 Clinic）时，一条
    `Patient → Patient(FHIR)` 映射可能**属于另一个源**；平台按 `(源数据源, 目标)` 分组，
    本组因此缺主表 → 报错只说"没有目标为 Patient 的源表"，用户不知道 Patient 其实在别处。
    此处把"已存在的同类映射归属哪个源 / 本组源是谁"一并报出，并给出两条可照做的修法。
    """
    try:
        gid = str(group_source_id or "").strip()
        want = str(entity or "").strip().lower()
        outs: list[str] = []
        has_same = False          # 本组源下**已有**该实体映射 → 失败另有原因（如 key_hint），不出提示
        for m in repository.list_mappings() or []:
            if not isinstance(m, dict):
                continue
            if str(m.get("target_type") or "").upper() != str(target_type or "").upper():
                continue
            tgt = str(m.get("target_table") or "").split(".")[-1].strip().lower()
            if tgt != want:
                continue
            sid = str(m.get("source_id") or "").strip()
            if not sid:
                # 2026-09-19：`source_id` 为空 = 同名资产跨源时平台**不猜**（留空 + 已回报 source_ambiguous）
                # → 该映射按 (源,目标) 分组会掉进"无源"组，本组因此缺主表。必须显式告知。
                outs.append("%s（映射 %s）**未声明数据源**（同名资产在多个源下存在时平台不猜）"
                            % (m.get("source"), m.get("id")))
            elif sid != gid:
                outs.append("%s（映射 %s）归属数据源 %s" % (m.get("source"), m.get("id"), sid))
            else:
                has_same = True
        if not outs:
            if has_same:
                return ""      # 本组源下已有该实体映射 → 报错原因不是"缺主表"，不产生噪声提示
            # 完全没有任何该实体的映射（最常见：用户只匹配了子表）→ 直接给出可照做的做法
            hint0 = ("。提示：本组（源 %s → %s %s）没有任何 `%s → %s(%s)` 映射 —— "
                     "sql2fhir 以**患者为起始**，缺主表就无法推导布局。"
                     "请在「AI 智能匹配」页同时勾选**本组源的该表**与目标的该资源，做一次匹配并确认"
                     "（同名跨源时只勾本组源的资产）。" % (
                         gid or "(未声明)", target_type, entity, entity, entity, target_type))
            logger.info("sql2fhir 缺主表提示（无同类映射）：%s", hint0)
            return hint0
        hint = ("提示：登记里已有 `%s → %s(%s)` 映射 —— %s；而本组源是 %s。"
                "请在本组源下对该表重新做一次 AI 智能匹配并确认（推荐；同名跨源时**只勾选本组源的资产**），"
                "或把该映射的 source_id 修正为本组源。" % (
                    entity, entity, target_type, "；".join(sorted(set(outs))[:4]),
                    gid or "(未声明)"))
        logger.info("sql2fhir 缺主表提示：%s", hint)
        return "。" + hint
    except Exception as exc:  # noqa: BLE001 - 提示构造失败不影响原始报错
        logger.debug("缺主表提示构造失败: %s", exc)
        return ""


def reset_ens_messages_for_generate() -> None:
    """生成前停 Production 并清空 Ens 消息历史。

    为什么：消息历史里遗留的 Error 消息（如上一版 Agent BP 的运行期报错）会被 smoke 校验
    当成本次生成的问题 → 触发"自动重建一次"后仍失败。清空后校验只反映本次生成的结果。
    """
    try:
        iris_connector.class_method_value("Ens.Director", "StopProduction", 30)
    except Exception as exc:  # noqa: BLE001 - 未运行/类不存在时忽略
        logger.info("生成前停 Production 跳过: %s", str(exc)[:120])
    try:
        iris_connector.execute("DELETE FROM Ens.MessageBody")
        iris_connector.execute("DELETE FROM Ens.MessageHeader")
        logger.info("已清空 Ens 消息历史（避免历史错误干扰 smoke 校验）")
    except Exception as exc:  # noqa: BLE001 - 清理失败不阻断生成
        logger.warning("清空消息历史失败（不阻断）: %s", str(exc)[:120])


def generate_multi_pipelines(pipelines: list[dict], *, strict_terms: bool = False,
                           force: bool = False,
                           allow_rule_fallback: bool = False) -> dict:
    """多管道生成：单 Production 内多套 BS/BP/BO（阶段1，简化验证闭环）。

    每组 pipeline（body.pipelines 元素）:
      {source_type?, source_id?, source_config?, target_type?, target_id?, target_config?, mappings:[...]}

    流程：逐组补齐源/目标配置 → build_multi_pipeline_topology 合并拓扑 →
          写路由表 ^demo.Config("pipe", <源BS名>) → PipelineGenerator 渲染启动。
    返回 success data（result/validation）。
    """
    import iris

    # —— 术语映射**盘点**（术语服务器 = 术语转换的唯一事实源；**默认放行**）——
    # 位置：停 Production / 清消息历史**之前**（盘点只读，不改环境）。
    # 2026-09-18 口径：缺映射**不再中止生成**（真实世界形态：数据先落地，术语缺口走待办 + 补录治理），
    # 运行期由共享 BO 命中 missing 时默认降级（保留源编码 + meta.tag=unmapped，不静默）；
    # 仅当显式 `strict_terms=true`（合规场景）才在缺映射时中止（TERM_MAP_INCOMPLETE）。
    _strict = bool(strict_terms)     # 由路由从请求体读取后传入（服务层不读 Flask 全局）
    # 增量生成逃生开关：force=true 时**不**复用存量（强制全量重生成，用于手工改动组件后复位）
    _force_regen = bool(force)
    # 规则兜底开关（与单管道同口径，差异 #2 合并）：**默认不静默回退** —— 仅当调用方显式
    # `allow_rule_fallback=true` 时才允许注册表规则兜底，且结果在响应 `ai.rule_fallback` 标注。
    _allow_rule_fallback = bool(allow_rule_fallback)
    _tgate = term_precheck([m for g in pipelines for m in (g.get("mappings") or [])],
                            strict=_strict)
    _term_todo = list(_tgate.get("todo") or [])
    if not _tgate.get("ok"):
        logger.error("多管道生成被术语**严格**预检中止: %s", _tgate.get("message"))
        _brief = term_catalog_brief(_tgate.get("gate") or {})
        return {"result": "TERM_MAP_INCOMPLETE", "reason": "TERM_MAP_INCOMPLETE",
                "message": _tgate.get("message") or "", "gate": _brief,
                "validation": {"ok": False, "issues": [
                    {"severity": "error", "check": "term_map_precheck",
                     "message": _tgate.get("message") or "术语预检未通过（严格模式）"}]}}

    # 生成前先停 Production 并清消息历史（否则历史 Error 消息会让 smoke 校验误判本次失败）
    reset_ens_messages_for_generate()

    groups: list[dict] = []
    all_mappings: list[dict] = []
    fhir_cfg: dict = {}

    conn = iris_connector.get_connection()
    try:
        native = iris.createIRIS(conn)
        for g in pipelines:
            src_type = g.get("source_type") or ""
            tgt_type = g.get("target_type") or ""
            src_cfg = dict(g.get("source_config") or {})
            tgt_cfg = dict(g.get("target_config") or {})

            # —— 源配置补齐（source_id → 数据源；ID 缺失/失效时按映射源表反查）——
            ds = repository.get_datasource(g.get("source_id")) if g.get("source_id") else None
            if not ds:
                ds = datasource_for_mappings(g.get("mappings"), "SQL" if src_type == "SQL" else "")
                if ds:
                    logger.info("管道组 source_id=%s 无效，按映射源表反查到数据源 %s",
                                g.get("source_id"), ds.get("id"))
            dcfg = (ds.get("config") or {}) if ds else {}
            # FHIR 源配置兜底（与单管道同口径）：组未带 endpoint 且取不到数据源时，从**任一
            # FHIR 数据源**的运行契约取（历史上前端可能取不到字段 → FHIRSyncService 抓不到）。
            if not src_cfg.get("endpoint") and not ds:
                for _ds0 in repository.list_datasources():
                    if (_ds0.get("type") or "") != "FHIR":
                        continue
                    _rc0 = ((_ds0.get("runtime") or {}).get("connection")
                            or repository.datasource_runtime(_ds0)["connection"])
                    if _rc0.get("endpoint"):
                        src_cfg["endpoint"] = _rc0.get("endpoint")
                        src_cfg.setdefault("username", _rc0.get("username") or "superuser")
                        src_cfg.setdefault("password", _rc0.get("password") or "SYS")
                        logger.info("FHIR 源配置兜底：采用数据源 %s 的 endpoint", _ds0.get("id"))
                        break
            # 运行参数统一取归一化契约 connection（repository.datasource_runtime）；
            # 无数据源时留空字典，避免未绑定变量（曾致多管道生成直接 500）
            rconn = (((ds.get("runtime") or {}).get("connection")
                      or repository.datasource_runtime(ds)["connection"]) if ds else {})
            if ds:
                src_type = ds.get("type") or src_type
                src_cfg.setdefault("dsn", rconn.get("dsn") or dcfg.get("dsn") or "localTarget")
                src_cfg.setdefault("query", rconn.get("query") or dcfg.get("query") or "")
                src_cfg.setdefault("key_field", rconn.get("key_field") or dcfg.get("key_field") or "ID")
                src_cfg.setdefault("endpoint", rconn.get("endpoint") or "")
                src_cfg.setdefault("username", rconn.get("username") or "")
                src_cfg.setdefault("password", rconn.get("password") or "")
            if src_type == "SQL":
                # 多管道关键：源 BS 的 Query 必须以**本组映射的源表**为准。
                # 否则会沿用数据源的默认 query（可能是另一张表）—— 曾导致 SQL→SOAP 组误读
                # Diagnosis 表，把诊断 ID 当成患者号投递给 SOAP 目标。
                for _m in g.get("mappings") or []:
                    _tbl = str(_m.get("source") or "").split(".")[0].strip()
                    if _tbl and not _tbl.startswith(("DS", "M", "TG", "concat")):
                        src_cfg["query"] = f"SELECT * FROM SQLUser.{_tbl}"
                        break
                if (src_cfg.get("dsn") or "").startswith("jdbc:"):
                    src_cfg["dsn"] = "localTarget"
                # DSN：优先引用数据源已注册的 DSN；未注册则按 jdbc_url 幂等创建（同单管道）
                from backend.services import jdbc_dsn as _jdbc_dsn
                _registered = _jdbc_dsn.register_for_datasource(ds)
                if _registered:
                    src_cfg["dsn"] = _registered
                src_cfg["jdbc_url"] = (src_cfg.get("jdbc_url") or rconn.get("jdbc_url")
                                       or dcfg.get("jdbc_url") or "")
                _jdbc_dsn.ensure_for({
                    "dsn": src_cfg.get("dsn"), "jdbc_url": src_cfg["jdbc_url"],
                    "username": src_cfg.get("username") or rconn.get("username") or "superuser",
                    "password": src_cfg.get("password") or rconn.get("password") or "SYS"})
                if not src_cfg.get("query"):
                    src_cfg["query"] = "SELECT * FROM SQLUser.PatientSource"
                    for _m in g.get("mappings") or []:
                        _s = (_m.get("source") or "").split(".")[0] or _m.get("source")
                        if _s and not _s.startswith(("DS", "M", "TG", "concat")):
                            src_cfg["query"] = f"SELECT * FROM SQLUser.{_s}"
                            break
            if not src_type:
                src_type = "SQL" if src_cfg.get("dsn") else "FHIR"

            # —— 目标类型推断 + 目标配置补齐（target_id → 目标；ID 缺失/失效时按目标表反查）——
            if not tgt_type:
                for _m in g.get("mappings") or []:
                    if _m.get("target_type") == "SOAP":
                        tgt_type = "SOAP"
                        break
            if not tgt_type:
                tgt_type = "DB"
            tg = repository.get_target(g.get("target_id")) if g.get("target_id") else None
            if not tg:
                tg = target_for_mappings(g.get("mappings"), tgt_type)
                if tg:
                    logger.info("管道组 target_id=%s 无效，按映射目标表反查到目标 %s",
                                g.get("target_id"), tg.get("id"))
            if tg:
                tgt_type = tg.get("type") or tgt_type
                tconn = tg.get("connection") or {}
                if (tg.get("type") or "") == "SOAP":
                    tgt_cfg.setdefault("wsdl", tconn.get("wsdl") or "")
                    tgt_cfg.setdefault("service", tconn.get("service") or "default")
                    tgt_cfg.setdefault("packages", tconn.get("packages") or {})
                    tgt_cfg.setdefault("bo_class", tconn.get("bo_class") or tconn.get("boClass") or tg.get("bo_class") or "")
                    tgt_cfg.setdefault("endpoint", tconn.get("endpoint") or Config.MOCK_SOAP_URL)
                elif (tg.get("type") or "").upper() == "FHIR":
                    # FHIR 目标：并入 REST 端点（base_url → HTTPOp 的 HTTPServer/HTTPPort；
                    # 路径前缀/凭据由 save_fhir_runtime_config 写 ^demo.Config("fhir",*)）
                    merge_fhir_target_config(tgt_cfg, tg)
                elif (tg.get("type") or "").upper() == "DB":
                    # DB 目标：并入连接契约（jdbc_url → 命名空间 → DSN），演示默认目标库 = CLINIC
                    # （SQL 源 = USER）；并确保该 DSN 真实存在（与单管道路径同口径）
                    from backend.services import jdbc_dsn as _jdbc_dsn_t
                    trt = (tg.get("runtime") or {}).get("connection") or {}
                    tgt_cfg.setdefault("jdbc_url", trt.get("jdbc_url") or tconn.get("jdbc_url") or "")
                    tgt_cfg.setdefault("dsn", trt.get("dsn") or _jdbc_dsn_t.dsn_name_for_target(tg))
                    tgt_cfg.setdefault("driver_class", trt.get("driver_class") or tconn.get("driver_class") or "")
                    # 列清单事实（注册时按目标 JDBC 元数据取得）：SQLOp UPSERT 必须按**目标库**列生成
                    _tc, _sch = registered_table_columns(tg)
                    tgt_cfg.setdefault("table_columns", _tc)
                    tgt_cfg.setdefault("schema", _sch or "SQLUser")
                    _jdbc_dsn_t.register_for_target(tg)
            # SOAP 目标默认投递地址（未显式指定时指向 Python mock，与单管道路径一致）
            if tgt_type == "SOAP":
                tgt_cfg.setdefault("service", tgt_cfg.get("service") or "default")
                tgt_cfg.setdefault("endpoint", tgt_cfg.get("endpoint") or Config.MOCK_SOAP_URL)

            # —— mapping 校正 target_type 并写回 ——
            maps = []
            for _m in g.get("mappings") or []:
                if not isinstance(_m, dict) or not _m.get("id"):
                    continue
                _prev = str(_m.get("target_type") or "").upper()
                if _prev and _prev != tgt_type:
                    # 不再静默改写：映射声明的目标类型与本组目标类型不一致 → 显式告警，
                    # 便于定位「UI 分组把同名实体归错目标」这类缺陷（如 DB.Patient vs FHIR.Patient）。
                    logger.warning("映射 %s 声明 target_type=%s 与所属组 %s 不一致，按组类型改写",
                                   _m.get("id"), _prev, tgt_type)
                _m["target_type"] = tgt_type
                write_mapping_patch(_m)
                maps.append(_m)
            if not maps:
                raise ValueError("管道组缺少 mappings")

            all_mappings.extend(maps)
            if src_type == "FHIR":
                fhir_cfg.setdefault("endpoint", src_cfg.get("endpoint") or "")
                fhir_cfg.setdefault("username", src_cfg.get("username") or "")
                fhir_cfg.setdefault("password", src_cfg.get("password") or "")

            groups.append({
                "source_type": src_type,
                "source_config": src_cfg,
                "target_type": tgt_type,
                "target_config": tgt_cfg,
                "mappings": maps,
                "source_id": (ds.get("id") if ds else g.get("source_id")),
                "target_id": (tg.get("id") if tg else g.get("target_id")),
                # Agent B 设计输入：源/目标运行契约（归一 runtime，密码由 llm_client 脱敏）
                "_rt_src": repository.datasource_runtime(ds) if ds else None,
                "_rt_tgt": repository.target_runtime(tg) if tg else None,
            })

        # —— P0a 身份稳定：① 按 (source_id, target_id) 去重（同管道两次提交只生成一次）
        #                     ② 身份缺失**拒绝生成**（否则会退化成 PIPE_<skill> 幽灵实例 = "两套 SQL-SOAP"）
        groups, _dup_merged = dedup_groups_by_identity(groups)
        _no_ident = [g for g in groups if not all(group_identity(g))]
        if _no_ident:
            _msg = "；".join(
                "%s→%s：%s" % (g.get("source_type"), g.get("target_type"),
                               pipeline_instances.pipeline_id_fallback_note(
                                   g.get("source_id"), g.get("target_id")))
                for g in _no_ident[:3])
            logger.error("多管道生成中止（管道身份不完整，不登记幽灵实例）: %s", _msg)
            return {"result": "PIPELINE_IDENTITY_MISSING", "reason": "PIPELINE_IDENTITY_MISSING",
                    "message": _msg, "dup_merged": _dup_merged,
                    "validation": {"ok": False, "issues": [
                        {"severity": "error", "check": "pipeline_identity", "message": _msg}]}}

        # —— P1b：未提交的既有管道**按存储定义自动并入**（单一 Production 必须整份渲染；
        #   否则"只提交变更组"会让别的管道从 Production 消失）——
        _submitted_ids = {pipeline_instances.pipeline_id(g.get("source_id"), g.get("target_id"))
                          for g in groups}
        _joined = auto_join_existing_pipelines(groups, _submitted_ids)
        if _joined:
            groups += _joined

        # —— 合并拓扑 + 写路由表 ——
        # C1：逐组转换验证-修复（字段/结构校验，映射修正后写回）
        for _g in groups:
            if _g.get("_unchanged"):
                continue        # 增量：未变更组跳过 C1（否则会重写映射 → 签名漂移、破坏"未变更"判定）
            _g_maps = _g.get("mappings") or []
            if not _g_maps:
                continue
            try:
                _fix = transformation_validator.validate_and_fix_transformation(
                    _g_maps, assets=c1_assets(_g.get("source_id")),
                    target_models=c1_target_models(_g_maps))
                if _fix.get("status") == "ok" and _fix.get("mappings"):
                    for _m in _fix["mappings"]:
                        if not isinstance(_m, dict) or not _m.get("id"):
                            continue
                        _m["target_type"] = _g["target_type"]
                        write_mapping_patch(_m)
                    _g["mappings"] = _fix["mappings"]
                else:
                    logger.warning("多管道 C1 转换验证未完全通过: %s", _fix.get("message"))
            except Exception as _ce:  # noqa: BLE001
                logger.warning("多管道 C1 转换验证异常: %s", _ce)
        all_mappings = [m for _g in groups for m in (_g.get("mappings") or [])]

        # —— Agent B（LLM）：逐组设计管道拓扑（AI 决定各组件的构成与顺序；sql2fhir 组按 Skill 分发）——
        for _g in groups:
            try:
                # —— 增量（P1）：同一业务身份 + **输入未变** → 复用已存组件，跳过 Agent B（省 LLM 调用、
                #    避免重复生成导致组件/命名抖动）；组件名在 build_multi_pipeline_topology 里按同一规则
                #    重新确定性命名 → 与首次生成结果一致。
                _sig_in = inc_input_signature(_g)
                # ⚠ 必须**在此刻**固定签名（供登记使用）：Agent B 分支会 `pop("_rt_src"/"_rt_tgt")`，
                # 之后再用 `inc_input_signature(_g)` 会得到"空契约"签名 → 下轮比较永远不等
                # （实测：入库签名与比较签名不一致 → 永远判"变更"、反复重生成）。
                _g["_sig_in"] = _sig_in
                _unchanged, _irec = (False, None) if _force_regen else pipeline_instances.is_unchanged(
                    _g.get("source_id"), _g.get("target_id"), _sig_in)
                if _unchanged and (_irec or {}).get("ai_components"):
                    _frozen_ok, _why = stored_definition_complete(
                        frozen_defs_from_instance(_irec or {}), _g.get("target_type"))
                    if not _frozen_ok:
                        logger.warning("增量生成：管道 %s 的**冻结定义**不完整（%s）→ 不走复用，"
                                       "交回正常生成链路重建（自愈）", (_irec or {}).get("id"), _why)
                        _unchanged = False
                if _unchanged and (_irec or {}).get("ai_components"):
                    _g["_unchanged"] = True
                    _g["_sig_in"] = _sig_in
                    _g["ai_components"] = _irec.get("ai_components") or []
                    _g["design_skill"] = _irec.get("design_skill") or _g.get("design_skill")
                    _g["_sql2fhir"] = bool(_irec.get("is_sql2fhir"))
                    # 冻结定义：复用渲染时用它（参数完整 + 改名前名字 → 保真且不产生二次后缀）
                    _g["_frozen_components"] = frozen_defs_from_instance(_irec)
                    _g["_frozen_infra"] = [dict(c) for c in (_irec.get("ai_components") or [])
                                           if isinstance(c, dict) and is_infra_component(c)]
                    if _g["_sql2fhir"] and _irec.get("layout"):
                        _g["_layout"] = _irec.get("layout")
                    logger.info("增量生成：管道 %s（%s→%s）输入未变 → 复用 %d 个已存组件，跳过 Agent B",
                                _irec.get("id"), _g["source_type"], _g["target_type"],
                                len(_g["ai_components"]))
                    continue
                if _irec:
                    # 诊断（长期保留）：能一眼看出"为什么没判成未变更"（映射变了？契约变了？）
                    logger.info("增量生成：管道 %s 入参已变 → 按变更组重新生成"
                                "（库存签名=%s 现算签名=%s；映射=%s）",
                                _irec.get("id"), _irec.get("signature"), _sig_in,
                                [(m.get("id"), len(m.get("field_mappings") or []))
                                 for m in (_g.get("mappings") or [])])
                # 上下文精准化：只把本组映射涉及的表交给 Agent（否则会为数据源每张表生成源 BS）
                _g_tables = source_tables_from_mappings(_g.get("mappings") or [])
                _g_rt_src, _g_models = scope_source_context(
                    _g.pop("_rt_src", None), c1_assets(_g.get("source_id")), _g_tables)
                _p = llm_client.recommend_pipeline(
                    _g.get("mappings") or [],
                    source_type=_g["source_type"], target_type=_g["target_type"],
                    available_components=AVAILABLE_COMPONENTS,
                    source_models=_g_models,
                    transformation_plan={"mappings": _g.get("mappings") or []},
                    source_runtime=_g_rt_src,
                    target_runtime=_g.pop("_rt_tgt", None),
                    # 已登记管道事实（同一源/目标 → 更新既有管道而非新增；决策仍归 LLM）
                    existing_pipelines=pipeline_instances.existing_pipelines_brief(),
                    # Plan 模式：SQL→FHIR 时只让 Agent B 出拓扑（聚合 BP 由计划+逐方法生成）
                    bp_plan_mode=bp_plan_mode_for(_g["source_type"], _g["target_type"]))
                _comps = ((_p.get("pipeline") or {}).get("components") or [])
                if not _comps:
                    raise ValueError("Agent B 未返回任何组件")
                _g["design_skill"] = _p.get("design_skill") if isinstance(_p, dict) else None
                # sql2fhir-patient-tx 组：布局 executor 推导 → 写 config → Agent 生成 BP → 组件由 Skill 产出
                if (_g["source_type"] == "SQL" and _g["target_type"] == "FHIR"
                        and _g["design_skill"] == "sql2fhir-patient-tx"):
                    from backend.services import sql2fhir_executor as _sfx
                    maps_layout, meta = resolve_sql_source_tables(
                        _g.get("mappings") or [], _g.get("source_id"))
                    try:
                        layout = _sfx.derive_sql2fhir_layout(maps_layout, meta)
                    except Exception as _de:  # noqa: BLE001 - 包装为可定位的错误
                        raise ValueError(
                            "该组映射无法推导 sql2fhir 布局（组内源表: "
                            f"{[str(m.get('source')) for m in (_g.get('mappings') or [])]}）：{_de}"
                            f"{missing_patient_root_hint(_g.get('mappings'), _g.get('source_id'))}")
                    layout = _sfx.enrich_layout_with_mappings(layout, maps_layout)
                    if layout.get("unmapped_query_bos"):
                        raise ValueError(
                            "sql2fhir 布局不完整：子资源查询 BO 缺少映射 id "
                            f"{layout['unmapped_query_bos']}（子资源会组装成空资源 → FHIR 必填元素缺失"
                            "→ 整个 Bundle 事务回滚）")
                    save_sql2fhir_layout(layout)
                    _bp_ok, _bp_msg, _bp_meta = ensure_sql2fhir_bp_dispatch(
                        _p, _g.get("mappings") or [], "SQL", "FHIR",
                        AVAILABLE_COMPONENTS, None, None,
                        layout=layout, design_skill="sql2fhir-patient-tx",
                        facts={"source": "SQL", "target": "FHIR"})
                    _g["_bp_meta"] = _bp_meta
                    if not _bp_ok:
                        raise ValueError(f"Agent 生成 BP 失败（组 sql2fhir）: {_bp_msg}")
                    _g["ai_components"] = _sfx.build_sql2fhir_components(
                        layout, _g.get("source_config") or {}, _g.get("target_config") or {})
                    _g["_sql2fhir"] = True
                    _g["_layout"] = layout          # 供 C2 目标落地预期（布局声明的资源）
                    _g["_src_bn"] = f"SQLService_{layout['patient_table']}"
                    logger.info("多管道 sql2fhir 组：布局+Agent BP 就绪（%d 组件）",
                                len(_g["ai_components"]))
                else:
                    _g["ai_components"] = _comps
                    logger.info("多管道 Agent B 设计 %s→%s 完成（%d 组件）：%s",
                                _g["source_type"], _g["target_type"], len(_comps),
                                [c.get("type") for c in _comps])
            except Exception as _be:  # noqa: BLE001
                # 红线：LLM 失败**不静默回退规则**。仅当调用方显式 allow_rule_fallback=true 才允许
                # 规则兜底（无 LLM key 的演示环境），且必须可审计（响应 ai.rule_fallback）。
                if not _allow_rule_fallback:
                    raise ValueError(
                        f"Agent B 数据管道 AI 生成失败（组 {_g['source_type']}→{_g['target_type']}）：{_be}"
                        "（如需以类型注册表规则兜底，请显式传 allow_rule_fallback=true）"
                    ) from _be
                logger.warning("Agent B 调用失败，已显式允许规则兜底（组 %s→%s）: %s",
                               _g["source_type"], _g["target_type"], _be)
                _g["ai_components"] = None        # None → 拓扑构建走注册表规则路径
                _g["_rule_fallback"] = True

        # 管道类别（= 设计 Skill，来自 Agent B 决策）：渲染为 Ens 业务主机 Category，
        # 供管道分组 / 按管道启停 / LLM 上下文；同源同目标的两组同名源 BS 按此改名（消除名字漂移）
        for _g in groups:
            _g["_category"] = pipeline_instances.category_of(
                _g.get("design_skill"), _g["source_type"], _g["target_type"])

        # —— P3 免重启（增量）：**全部组都是"输入未变"**且其组件仍在 Production 在位 →
        # 直接返回"无变更"：不重渲染、不编译、不 Stop/Start（避免无谓重启与运行态被重置）。
        # 任一组件缺失 → 走完整重渲染自愈（安全兜底）。
        if groups and all(_g.get("_unchanged") for _g in groups):
            _prod_now = running_items()
            _gone: list[str] = []
            for _g in groups:
                _rec_u = pipeline_instances.find_instance(_g.get("source_id"), _g.get("target_id")) or {}
                # ⚠ 判据必须是**存储定义（ai_components）整体是否在位**，不能只看实例的
                #   `component_names` —— 后者在历史缺陷里本身就是残缺的（少目标 BO），
                #   只比它会把"缺件"状态**锁死**（实测 2026-09-20：SOAP 目标 BO 缺席却判"无需重渲染"）。
                for _c in (_rec_u.get("ai_components") or []):
                    if not isinstance(_c, dict) or is_infra_component(_c):
                        continue
                    _n = str(_c.get("name") or "")
                    if not _n:
                        continue
                    if _n in _prod_now or any(x.startswith(_n + "__") for x in _prod_now):
                        continue
                    _gone.append(_n)
            # ③ 组内启停一致性（2026-09-20 实测）：同一管道的组件必须**全启用或全停用**——
            #    出现"源 BS 停、BO 还在跑"这类半启用状态时，必须重渲染收敛（否则状态被锁死、
            #    BO/BP 白占许可，用户看到"目标 BO 为何没被禁用"）。
            _by_cat: dict[str, set] = {}
            for _n, _i in _prod_now.items():
                _c = str(_i.get("category") or "")
                if _c:
                    _by_cat.setdefault(_c, set()).add(int(_i.get("enabled") or 0))
            _inconsistent = sorted(c for c, vs in _by_cat.items() if len(vs) > 1)
            if _inconsistent:
                logger.warning("增量生成：组件启停不一致的管道 %s → 走完整重渲染收敛", _inconsistent)
                _gone.extend(_inconsistent)
            if _prod_now and not _gone:
                logger.info("增量生成：%d 条管道输入均未变且组件在位 → 跳过重渲染/重启（无变更）",
                            len(groups))
                _unchanged_recs = [
                    pipeline_instances.find_instance(_g.get("source_id"), _g.get("target_id")) or {}
                    for _g in groups]
                for _g in groups:      # 保留本组输入签名（供下次比对）
                    _g["_sig_in"] = _g.get("_sig_in") or inc_input_signature(_g)
                return {"result": "OK", "unchanged": True,
                        "production": "demo.DataflowProduction",
                        "render_skipped": True,
                        "validation": {"ok": True, "error_count": 0, "warning_count": 0,
                                       "issues": [], "results": {},
                                       "note": "增量生成：全部管道输入未变，组件已在位 → 未重渲染/重启"},
                        "license_budget": {},
                        "term_summary": {}, "term_todo": [],
                        "config_cleanup": {},
                        "pipelines": _unchanged_recs, "pipeline_error": "",
                        "dup_merged": _dup_merged,
                        "ai": {"driven": True, "groups": [
                            {"category": _g.get("_category"), "source_type": _g["source_type"],
                             "target_type": _g["target_type"], "design_skill": _g.get("design_skill"),
                             "unchanged": True,
                             "rule_fallback": bool(_g.get("_rule_fallback"))}
                            for _g in groups]}}

        topology = build_multi_pipeline_topology(groups)
        # sql2fhir 布局**按聚合 BP 实例**写入（一管道一实例；全局键作历史兼容兜底）：
        # 组件名在拓扑构建阶段才定（同名跨组会加 `__{类别}` 后缀），故必须在此之后写。
        # ⚠ 未变更（复用）的组：`_agg_bp_name` 不在内存里 → 从**最终拓扑**按
        #   (type=PatientTxProcess, category) 解析出渲染后的实例名（与首次生成同名）。
        for _g in groups:
            if _g.get("_sql2fhir") and _g.get("_layout"):
                _bp_nm = str(_g.get("_agg_bp_name") or "")
                if not _bp_nm:
                    _bp_nm = next((str(c.get("name")) for c in (topology.get("components") or [])
                                   if str(c.get("type")) == "PatientTxProcess"
                                   and str(c.get("category")) == str(_g.get("_category"))), "")
                if _bp_nm:
                    save_sql2fhir_layout(_g["_layout"], _bp_nm)
                else:
                    logger.warning("增量生成：sql2fhir 组 %s 未能解析聚合 BP 实例名 → 未写布局",
                                   _g.get("_category"))

        # 许可调度（社区版 KeyLicenseUnits=8，业务主机常驻各占 1 个）：按组顺序装箱，
        # 放不下的分组**组件照旧生成、初始停用**（不再直接失败）→ 用户在「数据管道」卡片一键切换
        # ⚠ 增量（P2）：**未被调度停用**的未变更（复用）管道保留其当前启用位（生成新管道不重置老管道）。
        #   ⚠ 但**许可调度是硬约束，P2 不得覆盖它**（实测缺陷 2026-09-20：调度判定 sql2soap 超容量→停用，
        #   P2 回填又把 `TransformProcess`/`SOAPOp_PatientService` 改回启用 → **半启用** + 许可超订
        #   （enabled=10 > units=8），用户看到"目标 BO 没被禁用"）。
        _prev_enabled = {n: int(i.get("enabled") or 0) for n, i in running_items().items()}
        _unchanged_cats = {str(_g.get("_category")) for _g in groups if _g.get("_unchanged")}
        # 实例状态为 suspended 的类别 = 用户此前的选择（未变更组保持停用；不再逐组件按历史快照回填）
        try:
            _susp_inst_cats = {str(r.get("category")) for r in pipeline_instances.list_instances()
                               if r.get("status") == "suspended" and r.get("category")}
        except Exception:  # noqa: BLE001
            _susp_inst_cats = set()
        _plan = license_plan(topology)
        _susp = mark_suspended_components(topology, _plan)
        _restored = converge_component_enabled(topology, _plan, _unchanged_cats, _susp_inst_cats)
        if _restored:
            logger.info("增量生成：按许可调度/运行态收敛组件启停: %s", _restored)
        if _susp:
            logger.warning("许可调度：以下组件生成后处于停用状态 %s", _susp)
        save_pipeline_topology(topology)
        save_fhir_runtime_config(
            topology,
            next((g.get("target_config") for g in groups if g["target_type"] == "FHIR"), None),
            # 事实注入：本管道的 FHIR 映射 + 源数据源（决定每列 system/coded，引擎不再猜）
            mappings=[m for _g in groups if _g["target_type"] == "FHIR"
                      for m in (_g.get("mappings") or [])],
            source_id=next((_g.get("source_id") for _g in groups
                            if _g["target_type"] == "FHIR"), None))
        for _g in groups:
            src_bn = _g.get("_src_bn") or ("SQLService" if _g["source_type"] == "SQL"
                                           else "FHIRService")
            _tt = _g["target_type"]
            _m0 = _g["mappings"][0]
            pipe = {"mapping": _m0["id"], "target_type": _tt}
            if _tt == "SOAP":
                pipe["service"] = (_g["target_config"] or {}).get("service") or "default"
            else:
                pipe["table"] = _m0.get("target_table") or ""
            # 派发目标（**显式主机名**）：组件按管道实例改名后，BP 按约定拼名会指向不存在的主机
            # （缺陷 A6）→ 把实际 Operation 主机名写进本 BP 自己的配置，BP 优先用它
            if _g.get("_bp_items"):
                pipe["items"] = dict(_g["_bp_items"])
            if _g.get("_bp_item"):
                pipe["item"] = _g["_bp_item"]
            # ① 权威：本管道**自己的 BP** 读自己的参数（一管道一 BP，互不干扰）
            #    demo.TransformProcess.OnRequest: $Get(^demo.Config("bp", ..%ConfigName))
            #    ⚠ 仅当本组**确有**转换 BP 主机时才登记：Skill 自带聚合 BP 的组（sql2fhir）
            #      里没有该主机 → 登记了也没有任何读者（死配置，见 prune_stale_bp_config）。
            if _g.get("_has_router"):
                bp_name = _g.get("_bp_name") or "TransformProcess"
                native.set(json.dumps(pipe, ensure_ascii=False), "^demo.Config", "bp", bp_name)
                # ② 源 BS → BP 的投递目标（供代码内显式投递的 BS 读取，如 demo.FHIRService）
                native.set(bp_name, "^demo.Config", "bp_target", src_bn)
            # ③ 本组**映射集**：FHIR 队列是全局表，源 BS 据此只消费本管道的行
            #    （否则多管道并存时互相抢行并标 processed → 别组拿不到数据、派发到不存在的 SQLOp_*）
            _bs_ids = [str(x.get("id")) for x in (_g.get("mappings") or []) if x.get("id")]
            native.set(json.dumps(_bs_ids, ensure_ascii=False), "^demo.Config", "bs_mappings", src_bn)
            # ③ 兼容：旧的「按源 BS 路由」表 + 全局单值键（历史管道 / 兜底路径）
            native.set(json.dumps(pipe, ensure_ascii=False), "^demo.Config", "pipe", src_bn)
            native.set(_m0["id"], "^demo.Config", "pipeline", "active_mapping")
            native.set(_tt, "^demo.Config", "pipeline", "target_type")

        logger.info("多管道拓扑: %s", [c.get("name") for c in topology["components"]])
        mappings_json = json.dumps(all_mappings, ensure_ascii=False)
        config_json = json.dumps(fhir_cfg, ensure_ascii=False) if fhir_cfg else ""
        _src_types = sorted({g["source_type"] for g in groups})
        _tgt_types = sorted({g["target_type"] for g in groups})
        # 许可调度把「放不下的分组」标为停用（组件已生成、未启动）→ 这些组的目标必然无数据，
        # 不能纳入目标落地判定（否则"已生成但停用"会被判为生成失败，实测 500）。
        _susp_cats = {str(c) for c in (_plan.get("suspended") or [])}
        _active_tgt_types = sorted({g["target_type"] for g in groups
                                    if str(g.get("_category") or "") not in _susp_cats})
        # C2 目标落地预期（布局驱动）：sql2fhir 组声明的资源每条至少 1 个——
        # 防"Patient 落地、Encounter/Condition/MedicationRequest 全 0"仍判通过（实测静默缺陷）
        _expect_targets: dict[str, int] = {}
        for _g in groups:
            if str(_g.get("_category") or "") in _susp_cats:
                logger.info("许可调度停用的组 %s → 本次不做目标落地预期（待一键切换启用）",
                            _g.get("_category"))
                continue
            for _r in (((_g.get("_layout") or {}).get("bundle") or {}).get("resource_order") or []):
                if str(_r):
                    _expect_targets.setdefault(str(_r), 1)

        # 许可预算：社区版 KeyLicenseUnits=8，每个 Ens 业务主机常驻占 1 个；让不属于本次
        # 管道的旧管道组件让出许可（等价于用户手工"生成新管道前先关闭旧管道组件"），
        # 否则 Production 启动后许可立即耗尽 → 后端连不上 IRIS → 生成/校验必然失败
        # 许可预算：让不属于本次管道的旧管道组件让出许可（等价于用户手工"生成新管道前先关闭
        # 旧管道组件"）；本次拓扑内部由 apply_license_budget(groups=...) 做容量调度
        # （放不下的分组已在上面标 enabled=false，不会占许可单元）
        license_budget = pipeline_validator.apply_license_budget(
            keep_categories=pipeline_categories(*[_g["_category"] for _g in groups]),
            groups=topology_groups(topology))

        # 前置连通门禁（按组构造源/目标 runtime，check_connection 现场探测）
        # ⚠ 必须优先用**归一的运行契约**（_rt_src/_rt_tgt，含 analyze 时探到的 health）：登记的
        #   FHIR endpoint 常是浏览器视角的 http://localhost:52773/...，backend 在独立容器里连不上，
        #   直接拿原始 source_config/target_config 现探会必然报 Connection refused 而拦下生成
        #   （2026-09-16 实测：三组生成被 source.FHIR 不可达拦掉）。契约缺失（数据源未登记/
        #   反查不到）时才退回原始配置现探。
        _cc_src = [{**g["_rt_src"], "role": "source"} for g in groups if g.get("_rt_src")]
        _cc_tgt = [{**g["_rt_tgt"], "role": "target"} for g in groups if g.get("_rt_tgt")]
        if not _cc_src:
            _cc_src = [{"kind": g["source_type"], "role": "source",
                        "connection": dict(g.get("source_config") or {})} for g in groups]
        if not _cc_tgt:
            _cc_tgt = [{"kind": g["target_type"], "role": "target",
                        "connection": dict(g.get("target_config") or {})} for g in groups]
        _conn_check = pipeline_validator.check_connection(sources=_cc_src, targets=_cc_tgt)
        if not _conn_check.get("ok"):
            _msgs = [i.get("message", "") for i in _conn_check.get("issues", [])][:3]
            raise ValueError("源/目标连通性检查未通过: " + "; ".join(_msgs))

        def _gen_and_validate():
            _r = iris_connector.class_method_value(
                "demo.PipelineGenerator", "Generate", mappings_json, config_json)
            _v = pipeline_validator.run_pipeline_validation(
                topology, source_types=_src_types, target_types=_tgt_types,
                expect_targets=_expect_targets or None,
                effect_target_types=_active_tgt_types)
            return _r, _v

        result, validation = _gen_and_validate()
        # 运行期错误回喂（有界 1 次）：FHIR 结构类 → 逐组 C1 修映射；BP 代码类 → method_updates 修复
        if result != "OK" or not validation.get("ok"):
            logger.warning("多管道验证未通过明细: %s",
                           json.dumps(validation.get("issues") or [], ensure_ascii=False)[:900])
            try:
                _rt = (validation.get("results") or {}).get("runtime") or {}
                _kinds = _rt.get("kinds") or []
                _err_text = "\n".join(i.get("message", "") for i in (_rt.get("issues") or []))
                if "fhir_schema" in _kinds:
                    logger.info("多管道运行期 FHIR 结构错误 → 逐组 C1 修复映射")
                    for _g in groups:
                        if _g.get("target_type") != "FHIR":
                            continue
                        _f2 = transformation_validator.validate_and_fix_transformation(
                            _g.get("mappings") or [], assets=c1_assets(_g.get("source_id")),
                            target_models=c1_target_models(_g.get("mappings") or []))
                        if _f2.get("status") == "ok" and _f2.get("mappings"):
                            for _m in _f2["mappings"]:
                                if isinstance(_m, dict) and _m.get("id"):
                                    write_mapping_patch(_m)
                            _g["mappings"] = _f2["mappings"]
                    all_mappings = [m for _g in groups for m in (_g.get("mappings") or [])]
                    mappings_json = json.dumps(all_mappings, ensure_ascii=False)
                    result, validation = _gen_and_validate()
                if "bp_code" in _kinds:
                    from backend.services import generated_bp as _gbp
                    _sf_group = next((g for g in groups
                                      if g["source_type"] == "SQL" and g["target_type"] == "FHIR"),
                                     None)
                    if _sf_group:
                        logger.info("多管道运行期 BP 代码错误 → method_updates 增量修复")
                        _reps = _gbp.repair_from_runtime_errors(
                            mappings=_sf_group.get("mappings") or [],
                            source_type="SQL", target_type="FHIR",
                            available_components=AVAILABLE_COMPONENTS,
                            errors_text=_err_text)
                        if _reps.get("ok"):
                            try:
                                iris_connector.class_method_value("Ens.Director", "StopProduction", 10)
                                iris_connector.class_method_value(
                                    "Ens.Director", "StartProduction", "demo.DataflowProduction")
                            except Exception as _se:  # noqa: BLE001
                                logger.warning("多管道修复后重启 Production 失败: %s", _se)
                            result, validation = _gen_and_validate()
                        else:
                            logger.warning("多管道 BP 方法修复未通过: %s", _reps.get("message"))
                            # 回滚到上一版通过验证的 BP（显式告警）→ 重启 Production 复验，
                            # 保证「新增管道」不会因本次 Agent BP 质量波动而把数据通路弄坏
                            _rb = _gbp.restore_last_good()
                            if _rb.startswith("restored"):
                                logger.warning("多管道 BP 已回滚 last_good（%s）→ 重启并复验", _rb)
                                try:
                                    iris_connector.class_method_value(
                                        "Ens.Director", "StopProduction", 10)
                                    iris_connector.class_method_value(
                                        "Ens.Director", "StartProduction", "demo.DataflowProduction")
                                except Exception as _se:  # noqa: BLE001
                                    logger.warning("回滚后重启 Production 失败: %s", _se)
                                result, validation = _gen_and_validate()
            except Exception as _re:  # noqa: BLE001 - 回喂失败不阻断主流程
                logger.warning("多管道运行期回喂失败（不影响主流程）: %s", _re)
        # 失败自动重建一次（Ens 重启/瞬态类问题）；仍失败则沉淀经验后返回错误
        if result != "OK" or not validation.get("ok"):
            logger.warning("多管道首次生成/验证未通过（%s），自动重建一次", result)
            result, validation = _gen_and_validate()
        if result != "OK" or not validation.get("ok"):
            try:
                _emsg = "；".join([i.get("message", "") for i in validation.get("issues", [])
                                   if i.get("severity") == "error"][:3])
                pipeline_validator.save_validation_issue(
                    pattern="multi-pipeline %s -> %s" % ("/".join(_src_types), "/".join(_tgt_types)),
                    resolution=f"多管道生成失败: {result} / {_emsg}", source="multi-generate")
            except Exception as _se:  # noqa: BLE001
                logger.warning("多管道经验沉淀失败: %s", _se)
        # 术语覆盖**复核**（只读盘点）：运行期由共享 BO 实时查术语服务器；缺映射进"待办清单"，
        # 只作验证告警（默认降级口径，不阻断、不写缓存）
        term_summary: dict = {}
        if result == "OK":
            try:
                term_summary = term_summary(all_mappings, topology, validation)
            except Exception as _tc:  # noqa: BLE001 - 复核异常也显式报告（不影响生成结果）
                logger.warning("多管道术语复核异常（不影响生成）: %s", _tc)
                term_summary = {"ok": False, "error": str(_tc),
                                "note": "术语复核异常；运行期由共享 BO 实时查询（缺映射会降级）"}

        # 配置收敛：生成成功 = Production 已**整份替换**，本次之外的 `bp` / `bp_target` 登记
        # 都指向不存在的组件（死配置：诊断噪音 + 悬空引用隐患）。保留本次的 BP / 源 BS、
        # `last_good` 存档与仍存在的组件（见 prune_stale_bp_config）。
        config_cleanup: dict = {}
        if result == "OK":
            try:
                config_cleanup = prune_stale_bp_config(
                    keep_bps={_bg["_bp_name"] for _bg in groups if _bg.get("_bp_name")},
                    keep_srcs={_bg["_src_bn"] for _bg in groups if _bg.get("_src_bn")})
            except Exception as _pce:  # noqa: BLE001 - 收敛失败不改变生成结果
                logger.warning("配置收敛异常（不影响生成）: %s", _pce)
                config_cleanup = {"error": str(_pce)}

        # 管道实体登记（受管理持久对象）：按 (source_id, target_id, design_skill) 逐组登记，
        # 同一身份重复生成 = 更新同一管道（不新增）。生成失败则登记为失败原因（不写成功记录）。
        instance_records: list[dict] = []
        instance_error = ""
        if result == "OK":
            try:
                prod_items = pipeline_validator.production_items()
                for _g in groups:
                    _cat = _g["_category"]
                    _own = set(_g.get("_own_names") or [])
                    # 组件归属：**优先按本组专属组件名**（同类别多组时按 category 取会拿到两条管道的并集，
                    # 一键切换会连带启停另一条管道）；无名单（历史路径）才退化为按 category 过滤。
                    _comps = ([c for c in (topology.get("components") or [])
                               if str(c.get("name")) in _own] if _own else
                              [c for c in topology.get("components") or []
                               if c.get("category") == _cat])
                    instance_records.append(pipeline_instances.upsert_from_generation(
                        source_id=_g.get("source_id"), target_id=_g.get("target_id"),
                        source_type=_g["source_type"], target_type=_g["target_type"],
                        design_skill=_g.get("design_skill"), category_hint=_cat,
                        mapping_ids=[m.get("id") for m in (_g.get("mappings") or []) if m.get("id")],
                        components=_comps or (_g.get("ai_components") or []),
                        # P0：存**未改名**的原始组件（含 infra）+ 是否 sql2fhir + 布局，
                        #     供下次增量生成复用（跳过 Agent B，不重渲不重启）
                        ai_components=_g.get("ai_components") or [],
                        is_sql2fhir=bool(_g.get("_sql2fhir")),
                        layout=(_g.get("_layout") if _g.get("_sql2fhir") else None),
                        source_config=_g.get("source_config") or None,
                        target_config=_g.get("target_config") or None,
                        signature=_g.get("_sig_in") or inc_input_signature(_g),
                        applied_signature=pipeline_instances.component_signature(
                            topology.get("components"),
                            mapping_ids=[m.get("id") for m in (_g.get("mappings") or [])
                                         if m.get("id")]),
                        routes={"source_bs": _g.get("_src_bn") or ""},
                        ai={"driven": True,
                            "supplemented": (_g.get("_ai_supplemented")
                                             or topology.get("ai_supplemented") or [])},
                        validation=validation, production_items=prod_items,
                        # 未变更（复用存储定义）→ 不计入"生成次数"（本次没重新生成）
                        reused=bool(_g.get("_unchanged"))))
                pipeline_instances.reconcile_states(production_items=prod_items)
            except Exception as _pie:  # noqa: BLE001 - 登记失败不改变"生成成功"事实，但必须显式暴露
                instance_error = str(_pie)
                logger.error("管道实体登记失败（生成已成功，实体未登记）: %s", _pie)

        return {"result": result, "production": "demo.DataflowProduction",
                "validation": validation,
                # BP 生成链（Plan → Execute）审计：bp_mode / 计划方法清单 / 单测汇总
                "bp_mode": [dict({"category": _g.get("_category")}, **(_g.get("_bp_meta") or {}))
                            for _g in groups if _g.get("_bp_meta")],
                # 许可预算：本次生成让旧管道组件让出的许可单元（社区版仅 8 个；避免"启动即超限"）
                "license_budget": license_budget,
                # 术语覆盖复核 + **待办清单**（运行期由共享 BO 实时查；缺映射默认降级、不阻断）
                "term_summary": term_summary,
                "term_todo": _term_todo,
                # 术语服务器目录/盘点摘要（唯一事实源：覆盖了多少码、缺什么、源库命名空间）
                "term_catalog": term_catalog_brief(_tgate.get("gate") or {}),
                # 配置收敛：本次生成清理掉的陈旧 `bp` / `bp_target` 登记（无对应组件）
                "config_cleanup": config_cleanup,
                # 管道实体（受管理对象）：本次生成新建/更新的管道
                "pipelines": instance_records,
                "pipeline_error": instance_error,
                # 增量（P0a）：同身份重复组被合并的审计（提交里若含重复管道 → 合并而非新增）
                "dup_merged": _dup_merged,
                # AI 驱动信息：每组管道均由 Agent B（LLM）设计拓扑
                "ai": {
                    # 红线审计：任一组走了规则兜底 → driven=False 且按组标注 rule_fallback
                    "driven": not any(_g.get("_rule_fallback") for _g in groups),
                    "rule_fallback": any(_g.get("_rule_fallback") for _g in groups),
                    "groups": [{
                        "source_type": _g["source_type"], "target_type": _g["target_type"],
                        "category": _g.get("_category"),
                        "design_skill": _g.get("design_skill"),
                        "rule_fallback": bool(_g.get("_rule_fallback")),
                        "components": [c.get("type") for c in (_g.get("ai_components") or [])],
                    } for _g in groups],
                }}
    finally:
        iris_connector.reset_connections()
