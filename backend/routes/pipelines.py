"""数据管道 API：生成、触发、监控。"""

import json
import logging
import random
import re
import time
from datetime import datetime, timezone

from flask import Blueprint, request

from backend.config import Config, FHIRConfig
from backend.schemas.models import PipelineGenerateRequest
from backend.services import (iris_connector, llm_client, pipeline_instances,
                              pipeline_validator, repository,
                              transformation_validator, type_registry,
                              validate_agent, wsdl_importer)
# 「身份 / 签名 / 冻结定义」= **唯一实现**（backend/services/pipeline_identity.py，Step 1 重构）：
# 单管道与多管道两条入口共用同一份 → 结构上不再出现"两处各算一次"导致的口径漂移
# （S2 入库签名≠比较签名 / P0a 幽灵身份 等静默缺陷的根因）。
# ⚠ 以**私有别名**导入：保持既有调用点与离线回归（`PL._inc_input_signature` 等）不变。
from backend.services.pipeline_identity import (
    BO_PREFIX as _BO_PREFIX,
    dedup_groups_by_identity as _dedup_groups_by_identity,
    frozen_defs_from_instance as _frozen_defs_from_instance,
    group_identity as _group_identity,
    inc_input_signature as _inc_input_signature,
    running_items as _running_items,
    stored_definition_complete as _stored_definition_complete,
)
# 「上下文归一」= backend/services/pipeline_context.py（Step 2 重构）：源/目标配置重建、
# DSN/列清单/端点解析、C1 输入装配 —— 单/多管道两条路径共用同一份（私有别名保持调用点不变）。
from backend.services.pipeline_context import (
    registered_table_columns as _registered_table_columns,
    target_dsn as _target_dsn,
    get_table_columns as _get_table_columns,
    merge_fhir_target_config as _merge_fhir_target_config,
    fhir_http_host_port as _fhir_http_host_port,
    resolve_sql_source_tables as _resolve_sql_source_tables,
    sql_source_meta as _sql_source_meta,
    source_tables_from_mappings as _source_tables_from_mappings,
    scope_source_context as _scope_source_context,
    c1_assets as _c1_assets,
    c1_target_models as _c1_target_models,
    configs_for_identity as _configs_for_identity,
    datasource_for_mappings as _datasource_for_mappings,
    target_for_mappings as _target_for_mappings,
)

from backend.utils import error, success

logger = logging.getLogger(__name__)

pipelines_bp = Blueprint("pipelines", __name__, url_prefix="/api/pipelines")

# 模板库组件枚举（从类型注册表动态生成，供 Agent B 数据管道设计使用）
AVAILABLE_COMPONENTS = type_registry.get_available_components()



def _save_pipeline_topology(pipeline):
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


def _save_fhir_runtime_config(topology: dict | None, target_config: dict | None = None,
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


def _save_sql2fhir_layout(layout: dict, bp_name: str | None = None) -> None:
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



def _bp_plan_mode_on() -> bool:
    """BP 是否走"非整类"路径（engine / plan）——此时 Agent B 只出拓扑。"""
    import os
    return os.getenv("BP_GEN_MODE", "engine").strip().lower() in ("engine", "plan")


def _bp_plan_mode_for(source_type: str, target_type: str) -> bool:
    """SQL→FHIR（= sql2fhir-patient-tx，唯一需要聚合 BP 的组合）**只让 Agent B 出拓扑**。

    BP 由平台渲染（engine 模式，调父类通用引擎）或计划链逐方法生成；其余组合的 BP 都由平台类
    （`demo.TransformProcess`）承担，不涉及整类生成，无需切换。
    """
    return (_bp_plan_mode_on()
            and str(source_type).upper() == "SQL" and str(target_type).upper() == "FHIR")


def _ensure_sql2fhir_bp_by_plan(*, layout: dict, mappings: list[dict], components: list[dict],
                                design_skill: str = "sql2fhir-patient-tx",
                                facts: dict | None = None) -> tuple[bool, str, dict]:
    """**Plan → Execute** 链：计划（小输出）→ 逐方法生成（每步编译/单测，可断点续跑）。

    返回 (ok, message, meta)；meta 含 bp_mode/plan_methods/unit 汇总，供响应回显（可审计）。
    失败**显式**返回 False（是否允许回退整类由调用方按开关决定，见 `_ensure_sql2fhir_bp_dispatch`）。
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


def _ensure_sql2fhir_bp_dispatch(p_result, mappings, source_type: str, target_type: str,
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
        ok, msg = _ensure_sql2fhir_bp(p_result, mappings, source_type, target_type,
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
    ok, msg, meta = _ensure_sql2fhir_bp_by_plan(
        layout=layout, mappings=mappings, components=components or [],
        design_skill=design_skill, facts=facts)
    if ok or not fallback_ok:
        return ok, msg, meta
    logger.warning("Plan 链失败 → 按 BP_ALLOW_FULL_FALLBACK=1 回退整类生成（显式标注）: %s",
                   str(msg)[:200])
    ok2, msg2 = _ensure_sql2fhir_bp(p_result, mappings, source_type, target_type,
                                    available, source_runtime, target_runtime)
    meta2 = dict(meta)
    meta2.update({"bp_mode": "full_fallback", "bp_mode_reason": str(msg)[:200]})
    return ok2, msg2, meta2


def _ensure_sql2fhir_bp(p_result, mappings, source_type: str, target_type: str,
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
        return _build_from_ai_components(
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
                cols = _get_table_columns(table, target_config)
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
                        _s("Adapter", "DSN", _target_dsn(target_config)),
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
            host, port = _fhir_http_host_port(base)
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
    if _needs_terminology(mappings):
        _term_comp = _terminology_component()
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


def _build_from_ai_components(mappings: list[dict], *, source_type: str, target_type: str,
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
            cols = _get_table_columns(tbl, cfg_tgt) if tbl else []
            if not cols:
                logger.warning("目标表 %s 列结构为空，跳过 SQLOp（Agent B 表选择不可用）", tbl)
                return None
            col_sql = ", ".join(cols)
            q_marks = ", ".join("?" for _ in cols)
            return {
                "type": "SQLOperation", "name": name or f"SQLOp_{tbl}",
                "className": tpl["className"], "comment": f"写入目标表 {tbl}",
                "settings": [
                    _s("Adapter", "DSN", _target_dsn(cfg_tgt)),
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
            host, port = _fhir_http_host_port(base)
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
    if _needs_terminology(mappings) and not any(
            c["type"] == "TerminologyOperation" for c in components):
        _term_comp = _terminology_component()
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


def _dedupe_components(components: list[dict]) -> list[dict]:
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


def _common_component(template: dict) -> dict:
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


def _setting_value(comp: dict, name: str) -> str:
    """取组件某个 setting 的值（任意 Target：Adapter/Host）。"""
    for st in (comp.get("settings") or []):
        if str(st.get("name")) == name:
            return str(st.get("value") or "")
    return ""


_INTO_RE = re.compile(r"\bINTO\s+([^\s(]+)", re.IGNORECASE)


def _dispatch_targets(comps: list[dict], rename: dict[str, str] | None = None,
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
                m = _INTO_RE.search(_setting_value(c, "Query"))
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


def _assert_dispatch_targets(groups: list[dict], components: list[dict]) -> None:
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


def _config_subs(native, *path: str) -> list[str]:
    """枚举 `^demo.Config(<path...>)` 的下一级下标（Native SDK nextSubscript，顺序确定）。

    `path` 为空时枚举一级下标；多级（如 `"sql2fhir","layout"`）用于实例级布局键。
    """
    prefix = '^demo.Config(%s)' % ",".join('"%s"' % p for p in path)
    subs, s = [], native.nextSubscript(False, prefix, "")
    while s:
        subs.append(str(s))
        s = native.nextSubscript(False, prefix, s)
    return subs


# `^demo.Config("bp")` 里**非管道参数**的键（Agent BP 源码存档，由 generated_bp.save_last_good
# 写入，用于回滚/审计）→ 收敛清理必须保护，不可当"死配置"删掉。
_BP_CONFIG_PROTECTED = ("last_good", "last_good_at")


def prune_stale_bp_config(keep_bps: set[str], keep_srcs: set[str],
                          dry_run: bool = False) -> dict:
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
        for key in _config_subs(native, "bp"):
            if key in protected or key in keep_bps or key in comps:
                continue
            if not dry_run:
                native.kill("^demo.Config", "bp", key)
            removed["bp"].append(key)
        for key in _config_subs(native, "bp_target"):
            val = str(native.get("^demo.Config", "bp_target", key) or "")
            if key in keep_srcs or (key in comps and val in comps):
                continue
            if not dry_run:
                native.kill("^demo.Config", "bp_target", key)
            removed["bp_target"].append({"key": key, "value": val})
        # 实例级布局 `^demo.Config("sql2fhir","layout",<聚合BP名>)`：读者是**该 BP 自己**
        # （运行期 `..%ConfigName` 查自己那份）→ 组件不在位 = 无读者，与 bp 同属死配置。
        # ⚠ 只清**带下标的实例键**，不动全局兜底键 `^demo.Config("sql2fhir","layout")`（历史兼容路径仍在读）。
        for key in _config_subs(native, "sql2fhir", "layout"):
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


def _unkeyed_operations(comps: list[dict]) -> list[str]:
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
            if _INTO_RE.search(_setting_value(c, "Query")):
                continue
            if "SELECT" in _setting_value(c, "Query").upper():
                continue                       # 查询 BO（SELECT）：不是路由 BP 的派发目标
            bad.append(old)
        elif ctype == "SOAPOperation" and not old.startswith("SOAPOp_"):
            bad.append(old)
        elif ctype not in ("SQLOperation", "SOAPOperation", "HTTPOperation", "TransformProcess",
                           "SQLService", "FHIRService", "PatientTxProcess", "JavaGateway"):
            if ctype.endswith("Operation"):
                bad.append(old)
    return bad


def _auto_join_existing_pipelines(submitted: list[dict],
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
        src_cfg, tgt_cfg = _configs_for_identity(rec.get("source_id"), rec.get("target_id"))
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
        g["_sig_in"] = _inc_input_signature(g)
        # 冻结定义（复用渲染用：参数完整 + 改名前名字）
        g["_frozen_components"] = _frozen_defs_from_instance(rec)
        g["_frozen_infra"] = [dict(c) for c in comps
                              if isinstance(c, dict) and _is_infra_component(c)]
        _ok_def, _why = _stored_definition_complete(
            _frozen_defs_from_instance(rec), rec.get("target_type"))
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






def _write_mapping_patch(m: dict) -> None:
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






def _group_components(g: dict) -> list[dict]:
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

    def _group_components(g: dict, bp_name: str) -> tuple[list[dict], bool, list[str]]:
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
            if not _is_infra_component(c):
                continue
            if not any(str(c.get("name")) == str(x.get("name")) for x in infra_seen):
                infra_seen.append(c)
        return [c for c in raw if not _is_infra_component(c)], needs_router, supplemented

    # 基础设施收集桶（见 _group_components 内注释）：跨管道共享、全局仅 1 个实例
    infra_seen: list[dict] = []

    categories = [str(g.get("_category") or pipeline_instances.category_of(
        g.get("design_skill"), g.get("source_type"), g.get("target_type")))
        for g in pipelines]
    # 每组一个专属 BP（按管道类别命名；同类别多组自动加序号）
    taken_bp: set[str] = set()
    bp_names = [_bp_name_for(cat, taken_bp) for cat in categories]
    group_parts = [_group_components(g, bp)
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
        _bp_items, _bp_item = _dispatch_targets(
            comps, rename, pipelines[gi].get("target_config"))
        # 本组走通用路由时，DB/SOAP 目标的**派发键必须可解析**（否则 BP 会按不存在的拼名派发）
        if group_parts[gi][1]:
            _bad = _unkeyed_operations(comps)
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
        # 「生成期悬空自检 `_assert_dispatch_targets`」与「布局 http_bo 兜底」的输入；
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
    components = _dedupe_components(components)
    # 基础设施（真正跨管道共享，各 1 个）：JavaGateway（JDBC 网关）；
    # 转换 BP 不再共享——每组在自己的拓扑里各有一个专属实例（见 _bp_name_for）。
    for t in type_registry.get_common_components():
        if t["type"] == "JavaGateway":
            comp = _common_component(t)
            comp["category"] = "shared"
            components.append(comp)
    # 共享术语 BO（术语转换能力的唯一出口）：本次任何一组含 term_map 决策就全局挂 1 个实例
    # （category=shared → 不参与许可调度、不被按类别让路停用；各管道的 BP 共用它取判码）。
    _all_mappings = [m for g in pipelines for m in (g.get("mappings") or [])]
    if _needs_terminology(_all_mappings):
        _term_comp = _terminology_component()
        if _term_comp:
            components.append(_term_comp)
            logger.info("本次含 term_map 决策 → 追加共享术语 BO %s", _term_comp.get("name"))
    # 组内拓扑自带的基础设施（如 FHIR 源的 FHIRSyncService 生产者）：**全局追加一次**，
    # 归 shared（不参与许可调度、不被按类别让路停用，修 J 语义）。
    for comp in infra_seen:
        comp["category"] = "shared"
        components.append(comp)
    components = _dedupe_components(components)
    if not need_router:
        logger.info("本次拓扑无组走通用路由 → 不生成转换 BP（无需该组件，省 1 个许可单元）")
    # 生成期自检：派发目标必须真实存在（改名后引用悬空 → 运行期红消息 + 目标零落地，静默）
    _assert_dispatch_targets(pipelines, components)
    return {"production": "demo.DataflowProduction",
            "categories": categories, "components": components}



def _active_items_from_topology(topology: dict) -> list[str]:
    """本次管道要启用的业务主机名（许可预算用；基础设施组件由 IRIS 侧保留）。"""
    names: list[str] = []
    for c in (topology or {}).get("components", []) or []:
        if _is_infra_component(c):
            continue
        n = str((c or {}).get("name") or "").strip()
        if n and n not in names:
            names.append(n)
    return names


def _needs_terminology(mappings: list[dict] | None) -> bool:
    """本次映射里是否存在术语转换决策（`transform=term_map:<skill>`）→ 是否要挂共享术语 BO。

    判据与 C1/运行期同源（都读 field_mappings[].transform），避免"AI 决策了术语转换、
    但运行期没有可用的术语能力"这种静默缺口。
    """
    for m in mappings or []:
        for fm in (m or {}).get("field_mappings") or []:
            if str((fm or {}).get("transform") or "").startswith("term_map:"):
                return True
    return False


def _terminology_component() -> dict | None:
    """共享术语 BO 组件（`demo.TerminologyOperation`，全局 1 实例，category 由 _stamp/追加处置 shared）。"""
    tpl = next((c for c in type_registry.get_common_components()
                if c.get("type") == "TerminologyOperation"), None)
    if not tpl:
        return None
    comp = _common_component(tpl)
    comp["category"] = "shared"
    return comp


def _is_infra_component(comp: dict) -> bool:
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


def _is_shared_component(comp: dict) -> bool:
    """共享组件判定（兼容旧名，等价 _is_infra_component）：category="shared" 的成员。"""
    return _is_infra_component(comp)


def _stamp_categories(topology: dict, category: str) -> dict:
    """给拓扑组件打「管道类别」：本管道组件 = 管道类别，基础设施组件 = "shared"。

    类别会渲染为 Ens 业务主机的 Category（PipelineGenerator.RenderComponent），
    是「管道分组 / 按管道启停 / LLM 上下文」的事实来源；转换 BP 属本管道（随管道启停）。
    """
    for comp in (topology or {}).get("components") or []:
        comp["category"] = "shared" if _is_shared_component(comp) else category
    return topology


def _pipeline_categories(*categories) -> list[str]:
    """本次生成的管道类别集合（许可预算 keep 名单；shared 由 IRIS 侧自动保留）。"""
    return [c for c in dict.fromkeys(str(x) for x in categories if x)]


def _topology_groups(topology: dict) -> list[dict]:
    """按组件 Category 聚合「管道分组」（许可调度粒度；基础设施 JavaGateway 不参与预算）。

    返回 [{"category", "items": [业务主机名, ...]}]，顺序 = 拓扑中组件首次出现顺序
    （即用户提交的分组顺序），保证调度结果确定（先进先服务）。
    """
    order: list[str] = []
    buckets: dict[str, list[str]] = {}
    for c in (topology or {}).get("components") or []:
        if _is_infra_component(c):
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


def _mark_suspended_components(topology: dict, plan: dict) -> list[str]:
    """把被许可调度停用的分组组件标 `enabled=False`（渲染为 Ens Item Enabled="false"）。

    「许可调度」的落地点：放不下的分组**组件照旧生成**（Production 里可查、可供验证），
    只是初始为停用状态（不占许可单元），用户在 UI 一键切换即可启用。返回被标记的组件名。
    """
    cats = {str(c) for c in (plan.get("suspended") or [])}
    marked: list[str] = []
    for c in (topology or {}).get("components") or []:
        if _is_infra_component(c):
            continue
        if str((c or {}).get("category") or "") in cats:
            c["enabled"] = False
            nm = str((c or {}).get("name") or "")
            if nm:
                marked.append(nm)
    return marked


def _converge_component_enabled(topology: dict, plan: dict, unchanged_cats: set,
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



def _license_plan(topology: dict) -> dict:
    """本拓扑的许可容量调度计划（超容量的分组停用而非失败，见 schedule_groups）。"""
    return pipeline_validator.schedule_groups(_topology_groups(topology))


def _source_bs_names(topology: dict) -> list[str]:
    """拓扑中的源业务主机名（SQLService / FHIRService / FHIRSyncService）。"""
    names: list[str] = []
    for c in (topology or {}).get("components") or []:
        if str((c or {}).get("type") or "") in ("SQLService", "FHIRService"):
            n = str((c or {}).get("name") or "").strip()
            if n and n not in names:
                names.append(n)
    return names


def _all_pipeline_source_bs_names() -> list[str]:
    """全部已登记管道的源 BS 名（判码缓存刷新遇许可不足时可按需暂停腾单元）。"""
    names: list[str] = []
    try:
        for rec in pipeline_instances.list_instances():
            routes = rec.get("routes") or {}
            cand = list(routes.get("source_bs_names") or [])
            if routes.get("source_bs"):
                cand.append(routes["source_bs"])
            for n in cand:
                if n and n not in names:
                    names.append(n)
    except Exception as exc:  # noqa: BLE001 - 诊断性读取失败不影响生成
        logger.warning("读取已登记管道源 BS 列表失败: %s", exc)
    return names


def _term_catalog_brief(gate: dict) -> dict:
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


def _term_precheck(mappings: list[dict] | None, *, pause_items: list[str] | None = None,
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


def _term_summary(mappings: list[dict] | None, topology: dict | None,
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
                "gate": _term_catalog_brief(gate), "skipped": gate.get("skipped") or "",
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
            "gate": _term_catalog_brief(gate),
            "note": ("术语转换运行期由**共享 BO** demo.TerminologyOperation 实时查术语服务器："
                     "active→追加目标体系 coding（双 coding）；negative→不追加（服务器判定无匹配）；"
                     "missing/error→**默认降级**（保留源编码 + meta.tag=unmapped，不静默）；"
                     "todo=服务器尚无该转换的源编码（补录：tools/term_map_build.py，补录后无需重生成）")}


def _missing_patient_root_hint(mappings: list[dict] | None, group_source_id: str | None,
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


def _reset_ens_messages_for_generate() -> None:
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


def _generate_multi_pipelines(pipelines: list[dict]) -> dict:
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
    _strict = bool((request.get_json(silent=True) or {}).get("strict_terms"))
    # 增量生成逃生开关：force=true 时**不**复用存量（强制全量重生成，用于手工改动组件后复位）
    _force_regen = bool((request.get_json(silent=True) or {}).get("force"))
    _tgate = _term_precheck([m for g in pipelines for m in (g.get("mappings") or [])],
                            strict=_strict)
    _term_todo = list(_tgate.get("todo") or [])
    if not _tgate.get("ok"):
        logger.error("多管道生成被术语**严格**预检中止: %s", _tgate.get("message"))
        _brief = _term_catalog_brief(_tgate.get("gate") or {})
        return {"result": "TERM_MAP_INCOMPLETE", "reason": "TERM_MAP_INCOMPLETE",
                "message": _tgate.get("message") or "", "gate": _brief,
                "validation": {"ok": False, "issues": [
                    {"severity": "error", "check": "term_map_precheck",
                     "message": _tgate.get("message") or "术语预检未通过（严格模式）"}]}}

    # 生成前先停 Production 并清消息历史（否则历史 Error 消息会让 smoke 校验误判本次失败）
    _reset_ens_messages_for_generate()

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
                ds = _datasource_for_mappings(g.get("mappings"), "SQL" if src_type == "SQL" else "")
                if ds:
                    logger.info("管道组 source_id=%s 无效，按映射源表反查到数据源 %s",
                                g.get("source_id"), ds.get("id"))
            dcfg = (ds.get("config") or {}) if ds else {}
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
                tg = _target_for_mappings(g.get("mappings"), tgt_type)
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
                    # 路径前缀/凭据由 _save_fhir_runtime_config 写 ^demo.Config("fhir",*)）
                    _merge_fhir_target_config(tgt_cfg, tg)
                elif (tg.get("type") or "").upper() == "DB":
                    # DB 目标：并入连接契约（jdbc_url → 命名空间 → DSN），演示默认目标库 = CLINIC
                    # （SQL 源 = USER）；并确保该 DSN 真实存在（与单管道路径同口径）
                    from backend.services import jdbc_dsn as _jdbc_dsn_t
                    trt = (tg.get("runtime") or {}).get("connection") or {}
                    tgt_cfg.setdefault("jdbc_url", trt.get("jdbc_url") or tconn.get("jdbc_url") or "")
                    tgt_cfg.setdefault("dsn", trt.get("dsn") or _jdbc_dsn_t.dsn_name_for_target(tg))
                    tgt_cfg.setdefault("driver_class", trt.get("driver_class") or tconn.get("driver_class") or "")
                    # 列清单事实（注册时按目标 JDBC 元数据取得）：SQLOp UPSERT 必须按**目标库**列生成
                    _tc, _sch = _registered_table_columns(tg)
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
                _write_mapping_patch(_m)
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
        groups, _dup_merged = _dedup_groups_by_identity(groups)
        _no_ident = [g for g in groups if not all(_group_identity(g))]
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
        _joined = _auto_join_existing_pipelines(groups, _submitted_ids)
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
                    _g_maps, assets=_c1_assets(_g.get("source_id")),
                    target_models=_c1_target_models(_g_maps))
                if _fix.get("status") == "ok" and _fix.get("mappings"):
                    for _m in _fix["mappings"]:
                        if not isinstance(_m, dict) or not _m.get("id"):
                            continue
                        _m["target_type"] = _g["target_type"]
                        _write_mapping_patch(_m)
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
                _sig_in = _inc_input_signature(_g)
                # ⚠ 必须**在此刻**固定签名（供登记使用）：Agent B 分支会 `pop("_rt_src"/"_rt_tgt")`，
                # 之后再用 `_inc_input_signature(_g)` 会得到"空契约"签名 → 下轮比较永远不等
                # （实测：入库签名与比较签名不一致 → 永远判"变更"、反复重生成）。
                _g["_sig_in"] = _sig_in
                _unchanged, _irec = (False, None) if _force_regen else pipeline_instances.is_unchanged(
                    _g.get("source_id"), _g.get("target_id"), _sig_in)
                if _unchanged and (_irec or {}).get("ai_components"):
                    _frozen_ok, _why = _stored_definition_complete(
                        _frozen_defs_from_instance(_irec or {}), _g.get("target_type"))
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
                    _g["_frozen_components"] = _frozen_defs_from_instance(_irec)
                    _g["_frozen_infra"] = [dict(c) for c in (_irec.get("ai_components") or [])
                                           if isinstance(c, dict) and _is_infra_component(c)]
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
                _g_tables = _source_tables_from_mappings(_g.get("mappings") or [])
                _g_rt_src, _g_models = _scope_source_context(
                    _g.pop("_rt_src", None), _c1_assets(_g.get("source_id")), _g_tables)
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
                    bp_plan_mode=_bp_plan_mode_for(_g["source_type"], _g["target_type"]))
                _comps = ((_p.get("pipeline") or {}).get("components") or [])
                if not _comps:
                    raise ValueError("Agent B 未返回任何组件")
                _g["design_skill"] = _p.get("design_skill") if isinstance(_p, dict) else None
                # sql2fhir-patient-tx 组：布局 executor 推导 → 写 config → Agent 生成 BP → 组件由 Skill 产出
                if (_g["source_type"] == "SQL" and _g["target_type"] == "FHIR"
                        and _g["design_skill"] == "sql2fhir-patient-tx"):
                    from backend.services import sql2fhir_executor as _sfx
                    maps_layout, meta = _resolve_sql_source_tables(
                        _g.get("mappings") or [], _g.get("source_id"))
                    try:
                        layout = _sfx.derive_sql2fhir_layout(maps_layout, meta)
                    except Exception as _de:  # noqa: BLE001 - 包装为可定位的错误
                        raise ValueError(
                            "该组映射无法推导 sql2fhir 布局（组内源表: "
                            f"{[str(m.get('source')) for m in (_g.get('mappings') or [])]}）：{_de}"
                            f"{_missing_patient_root_hint(_g.get('mappings'), _g.get('source_id'))}")
                    layout = _sfx.enrich_layout_with_mappings(layout, maps_layout)
                    if layout.get("unmapped_query_bos"):
                        raise ValueError(
                            "sql2fhir 布局不完整：子资源查询 BO 缺少映射 id "
                            f"{layout['unmapped_query_bos']}（子资源会组装成空资源 → FHIR 必填元素缺失"
                            "→ 整个 Bundle 事务回滚）")
                    _save_sql2fhir_layout(layout)
                    _bp_ok, _bp_msg, _bp_meta = _ensure_sql2fhir_bp_dispatch(
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
            except Exception as _be:  # noqa: BLE001 - Agent B 失败即报错，不静默回退规则
                raise ValueError(
                    f"Agent B 数据管道 AI 生成失败（组 {_g['source_type']}→{_g['target_type']}）：{_be}"
                ) from _be

        # 管道类别（= 设计 Skill，来自 Agent B 决策）：渲染为 Ens 业务主机 Category，
        # 供管道分组 / 按管道启停 / LLM 上下文；同源同目标的两组同名源 BS 按此改名（消除名字漂移）
        for _g in groups:
            _g["_category"] = pipeline_instances.category_of(
                _g.get("design_skill"), _g["source_type"], _g["target_type"])

        # —— P3 免重启（增量）：**全部组都是"输入未变"**且其组件仍在 Production 在位 →
        # 直接返回"无变更"：不重渲染、不编译、不 Stop/Start（避免无谓重启与运行态被重置）。
        # 任一组件缺失 → 走完整重渲染自愈（安全兜底）。
        if groups and all(_g.get("_unchanged") for _g in groups):
            _prod_now = _running_items()
            _gone: list[str] = []
            for _g in groups:
                _rec_u = pipeline_instances.find_instance(_g.get("source_id"), _g.get("target_id")) or {}
                # ⚠ 判据必须是**存储定义（ai_components）整体是否在位**，不能只看实例的
                #   `component_names` —— 后者在历史缺陷里本身就是残缺的（少目标 BO），
                #   只比它会把"缺件"状态**锁死**（实测 2026-09-20：SOAP 目标 BO 缺席却判"无需重渲染"）。
                for _c in (_rec_u.get("ai_components") or []):
                    if not isinstance(_c, dict) or _is_infra_component(_c):
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
                    _g["_sig_in"] = _g.get("_sig_in") or _inc_input_signature(_g)
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
                             "unchanged": True} for _g in groups]}}

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
                    _save_sql2fhir_layout(_g["_layout"], _bp_nm)
                else:
                    logger.warning("增量生成：sql2fhir 组 %s 未能解析聚合 BP 实例名 → 未写布局",
                                   _g.get("_category"))

        # 许可调度（社区版 KeyLicenseUnits=8，业务主机常驻各占 1 个）：按组顺序装箱，
        # 放不下的分组**组件照旧生成、初始停用**（不再直接失败）→ 用户在「数据管道」卡片一键切换
        # ⚠ 增量（P2）：**未被调度停用**的未变更（复用）管道保留其当前启用位（生成新管道不重置老管道）。
        #   ⚠ 但**许可调度是硬约束，P2 不得覆盖它**（实测缺陷 2026-09-20：调度判定 sql2soap 超容量→停用，
        #   P2 回填又把 `TransformProcess`/`SOAPOp_PatientService` 改回启用 → **半启用** + 许可超订
        #   （enabled=10 > units=8），用户看到"目标 BO 没被禁用"）。
        _prev_enabled = {n: int(i.get("enabled") or 0) for n, i in _running_items().items()}
        _unchanged_cats = {str(_g.get("_category")) for _g in groups if _g.get("_unchanged")}
        # 实例状态为 suspended 的类别 = 用户此前的选择（未变更组保持停用；不再逐组件按历史快照回填）
        try:
            _susp_inst_cats = {str(r.get("category")) for r in pipeline_instances.list_instances()
                               if r.get("status") == "suspended" and r.get("category")}
        except Exception:  # noqa: BLE001
            _susp_inst_cats = set()
        _plan = _license_plan(topology)
        _susp = _mark_suspended_components(topology, _plan)
        _restored = _converge_component_enabled(topology, _plan, _unchanged_cats, _susp_inst_cats)
        if _restored:
            logger.info("增量生成：按许可调度/运行态收敛组件启停: %s", _restored)
        if _susp:
            logger.warning("许可调度：以下组件生成后处于停用状态 %s", _susp)
        _save_pipeline_topology(topology)
        _save_fhir_runtime_config(
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
            keep_categories=_pipeline_categories(*[_g["_category"] for _g in groups]),
            groups=_topology_groups(topology))

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
                            _g.get("mappings") or [], assets=_c1_assets(_g.get("source_id")),
                            target_models=_c1_target_models(_g.get("mappings") or []))
                        if _f2.get("status") == "ok" and _f2.get("mappings"):
                            for _m in _f2["mappings"]:
                                if isinstance(_m, dict) and _m.get("id"):
                                    _write_mapping_patch(_m)
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
                term_summary = _term_summary(all_mappings, topology, validation)
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
                        signature=_g.get("_sig_in") or _inc_input_signature(_g),
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
                "term_catalog": _term_catalog_brief(_tgate.get("gate") or {}),
                # 配置收敛：本次生成清理掉的陈旧 `bp` / `bp_target` 登记（无对应组件）
                "config_cleanup": config_cleanup,
                # 管道实体（受管理对象）：本次生成新建/更新的管道
                "pipelines": instance_records,
                "pipeline_error": instance_error,
                # 增量（P0a）：同身份重复组被合并的审计（提交里若含重复管道 → 合并而非新增）
                "dup_merged": _dup_merged,
                # AI 驱动信息：每组管道均由 Agent B（LLM）设计拓扑
                "ai": {
                    "driven": True,
                    "groups": [{
                        "source_type": _g["source_type"], "target_type": _g["target_type"],
                        "category": _g.get("_category"),
                        "design_skill": _g.get("design_skill"),
                        "components": [c.get("type") for c in (_g.get("ai_components") or [])],
                    } for _g in groups],
                }}
    finally:
        iris_connector.reset_connections()


@pipelines_bp.post("/generate")
def generate():
    """生成并启动数据管道。支持异构组合（source_type/target_type + target_config）与多管道（pipelines 列表）。"""
    body = request.get_json(silent=True) or {}
    # 多管道模式：body.pipelines = [组1, 组2, ...]，单 Production 内多套 BS/BP/BO
    pipelines_arg = body.get("pipelines")
    if pipelines_arg:
        try:
            multi = _generate_multi_pipelines(pipelines_arg)
        except Exception as exc:  # noqa: BLE001
            logger.error("多管道生成失败: %s", exc)
            return error(f"多管道生成失败: {exc}"), 500
        if multi.get("result") != "OK":
            # 术语门禁中止：返回 400 + 面向用户的明确说明（缺哪些码、怎么补录），而不是笼统 500
            if multi.get("gate") is not None:
                return error(multi.get("message") or "术语预检未通过", data={
                    "reason": multi.get("reason") or "TERM_MAP_INCOMPLETE",
                    "term_catalog": multi.get("gate") or {}}), 400
            # 管道身份不完整（缺源数据源/目标）：同样 400 + 可照做的说明（P0a：不登记幽灵实例）
            if multi.get("reason") == "PIPELINE_IDENTITY_MISSING":
                return error(multi.get("message") or "管道身份不完整", data={
                    "reason": "PIPELINE_IDENTITY_MISSING",
                    "dup_merged": multi.get("dup_merged") or []}), 400
            return error(f"多管道生成失败: {multi.get('result')}"), 500
        v = multi.get("validation") or {}
        if not v.get("ok"):
            msgs = [i.get("message") for i in v.get("issues", [])
                    if i.get("severity") == "error"][:3]
            return error(f"多管道验证未通过: {'; '.join(msgs) or '未知'}"), 500
        if multi.get("unchanged"):
            # 增量：全部提交的管道都已存在且未变更 → 不重渲染/不重启（P3）
            return success(multi, "所有数据管道均已存在且未变更（未重新生成）")
        return success(multi, "多管道已生成并启动")
    mappings = body.get("mappings") or []
    plan = body.get("transformation_plan") or {}
    if not mappings:
        mappings = plan.get("mappings") or []
    config = body.get("config")
    source_type = body.get("source_type") or ""
    target_type = body.get("target_type") or ""
    source_config = body.get("source_config") or {}
    target_config = body.get("target_config") or {}
    source_models = body.get("source_models") or plan.get("source_models") or []
    target_models = body.get("target_models") or plan.get("target_models") or []

    # 1. 自动推导/提取 target_type, target_id, target_config
    target_id = body.get("target_id")
    if not target_id and target_models:
        target_id = target_models[0].get("target_id") or target_models[0].get("id")

    for m in mappings:
        if isinstance(m, dict):
            if m.get("target_type") == "SOAP":
                target_type = "SOAP"
                break
            tbl = m.get("target_table") or ""
            for tg in repository.list_targets():
                if tg.get("type") == "SOAP":
                    for tb in (tg.get("tables") or []):
                        if tb.get("table") == tbl or tb.get("entity_name") == tbl or tb.get("name") == tbl:
                            target_type = "SOAP"
                            if not target_id:
                                target_id = tg.get("id")
                            break

    if not target_type and target_models:
        if any(m.get("type") == "SOAP" for m in target_models if isinstance(m, dict)):
            target_type = "SOAP"

    if target_type == "SOAP" and not target_id:
        for tg in repository.list_targets():
            if tg.get("type") == "SOAP":
                target_id = tg.get("id")
                break

    if not target_type:
        target_type = "DB"

    if target_id:
        tg = repository.get_target(target_id)
        if tg:
            target_type = tg.get("type") or target_type
            # 目标运行参数统一取归一化契约 connection（repository.target_runtime）
            rconn = (tg.get("runtime") or {}).get("connection") \
                or repository.target_runtime(tg)["connection"]
            if (tg.get("type") or "").upper() == "DB":
                # DB 目标：并入连接契约（jdbc_url → 命名空间 → DSN），供 SQLOp 引用。
                # 演示默认目标库 = **CLINIC**（SQL 源 = USER）；不并入就会退化成 localTarget/USER：
                # 目标登记的命名空间被忽略、数据静默写进 USER（见 services/jdbc_dsn.py 说明）。
                from backend.services import jdbc_dsn as _jdbc_dsn_t
                _tc, _sch = _registered_table_columns(tg)
                target_config = {
                    **target_config,
                    "jdbc_url": target_config.get("jdbc_url") or rconn.get("jdbc_url", ""),
                    "dsn": target_config.get("dsn") or rconn.get("dsn") \
                        or _jdbc_dsn_t.dsn_name_for_target(tg),
                    "driver_class": target_config.get("driver_class") or rconn.get("driver_class", ""),
                    # 列清单事实（注册时按目标 JDBC 元数据取得）：SQLOp UPSERT 必须按**目标库**列生成
                    "table_columns": target_config.get("table_columns") or _tc,
                    "schema": target_config.get("schema") or _sch or "SQLUser",
                }
                _jdbc_dsn_t.ensure_jdbc_dsn(
                    target_config.get("dsn"), target_config.get("jdbc_url"),
                    rconn.get("username") or "superuser", rconn.get("password") or "SYS")
            elif (tg.get("type") or "").upper() == "FHIR":
                # FHIR 目标：并入 REST 端点（base_url → HTTPOp HTTPServer/HTTPPort）
                target_config = _merge_fhir_target_config({**target_config}, tg)
            else:
                target_config = {
                    "wsdl": target_config.get("wsdl") or rconn.get("wsdl", ""),
                    "service": target_config.get("service") or rconn.get("service", "default"),
                    "packages": target_config.get("packages") or rconn.get("packages") or {},
                    "bo_class": target_config.get("bo_class") or rconn.get("bo_class") or tg.get("bo_class", ""),
                    # 远端 SOAP 地址：目标登记时可指定 endpoint；默认指向 Python mock 演示第三方系统
                    "endpoint": target_config.get("endpoint") or rconn.get("endpoint") or Config.MOCK_SOAP_URL,
                }

    if target_type == "SOAP" and not target_config.get("bo_class"):
        for tg in repository.list_targets():
            if tg.get("type") == "SOAP":
                rconn = (tg.get("runtime") or {}).get("connection") \
                    or repository.target_runtime(tg)["connection"]
                bo_c = rconn.get("bo_class") or tg.get("bo_class") or ""
                if bo_c:
                    target_config["bo_class"] = bo_c
                    if not target_config.get("wsdl"):
                        target_config["wsdl"] = rconn.get("wsdl", "")
                    if not target_config.get("service"):
                        target_config["service"] = rconn.get("service", "default")
                    break

    # 2. 自动推导/提取 source_type, source_id, source_config
    source_id = body.get("source_id")
    if not source_id and source_models:
        source_id = source_models[0].get("source_id") or source_models[0].get("id")

    # 提取全部涉及的源资产标识（映射/模型）
    mapped_sources = []
    for m in mappings:
        if isinstance(m, dict):
            if m.get("source"):
                mapped_sources.append(m["source"])
            for sa in (m.get("source_assets") or []):
                mapped_sources.append(sa)
            if m.get("source_type") == "SQL" or m.get("asset_type") == "SQL_TABLE":
                source_type = "SQL"
    for sm in source_models:
        if isinstance(sm, dict):
            if sm.get("name"):
                mapped_sources.append(sm["name"])
            if sm.get("id"):
                mapped_sources.append(sm["id"])
            struct = sm.get("structure") or {}
            if struct.get("table"):
                mapped_sources.append(struct.get("table"))
                if struct.get("schema"):
                    mapped_sources.append(f"{struct['schema']}.{struct['table']}")
            if sm.get("type") == "SQL_TABLE" or sm.get("asset_type") == "SQL_TABLE":
                source_type = "SQL"

    all_datasources = repository.list_datasources()
    all_assets = repository.list_assets()

    # 从映射名/资产匹配判断 SQL 数据源
    for src_name in mapped_sources:
        for ds in all_datasources:
            if ds.get("type") == "SQL":
                for a in all_assets:
                    if a.get("source_id") == ds.get("id"):
                        if a.get("name") == src_name or a.get("id") == src_name or a.get("name") in src_name:
                            source_type = "SQL"
                            if not source_id:
                                source_id = ds.get("id")
                            break

    if source_type == "SQL" and not source_id:
        for ds in all_datasources:
            if ds.get("type") == "SQL":
                source_id = ds.get("id")
                break

    if not source_type and all_datasources:
        for ds in all_datasources:
            if ds.get("type") == "SQL":
                source_type = "SQL"
                source_id = ds.get("id")
                break

    if not source_type:
        source_type = "FHIR"

    ds_obj = repository.get_datasource(source_id) if source_id else None
    if ds_obj:
        source_type = ds_obj.get("type") or source_type
        cfg = ds_obj.get("config") or {}
        rconn = (ds_obj.get("runtime") or {}).get("connection") \
            or repository.datasource_runtime(ds_obj)["connection"]
        source_config = {
            "dsn": source_config.get("dsn") or rconn.get("dsn") or "localTarget",
            "query": source_config.get("query") or rconn.get("query") or "",
            "key_field": source_config.get("key_field") or rconn.get("key_field", "ID"),
        }
        # DSN：优先引用数据源已注册的 DSN（连通测试/选表时写回 runtime）；
        # 未注册则按 jdbc_url 幂等创建（避免生成出 'USER' 之类不存在的 DSN）
        from backend.services import jdbc_dsn as _jdbc_dsn
        _registered = _jdbc_dsn.register_for_datasource(ds_obj)
        if _registered:
            source_config["dsn"] = _registered
        source_config["jdbc_url"] = (source_config.get("jdbc_url") or rconn.get("jdbc_url")
                                     or cfg.get("jdbc_url") or "")
        _jdbc_dsn.ensure_for({
            "dsn": source_config.get("dsn"), "jdbc_url": source_config["jdbc_url"],
            "username": rconn.get("username") or cfg.get("username") or "superuser",
            "password": rconn.get("password") or cfg.get("password") or "SYS"})
        # FHIR 源 endpoint/auth 也归一进 source_config（供 generate 后段/验证读取）
        if source_type == "FHIR":
            source_config["endpoint"] = source_config.get("endpoint") or rconn.get("endpoint") or ""
            source_config["username"] = source_config.get("username") or rconn.get("username") or ""
            source_config["password"] = source_config.get("password") or rconn.get("password") or ""

    if source_type == "SQL":
        if not source_config.get("dsn") or source_config.get("dsn").startswith("jdbc:"):
            source_config["dsn"] = "localTarget"

        user_explicit_query = (body.get("source_config") or {}).get("query")
        user_explicit_key = (body.get("source_config") or {}).get("key_field")

        query_built = ""
        key_field_built = ""

        # 精确依据 mapped_sources 匹配 DataAsset 或 DataSource tables
        for ms in mapped_sources:
            # 1. 匹配 DataAsset
            for a in all_assets:
                if a.get("id") == ms or a.get("name") == ms:
                    struct = a.get("structure") or {}
                    sch = struct.get("schema") or "SQLUser"
                    tbl = struct.get("table") or a.get("name")
                    if tbl:
                        query_built = f"SELECT * FROM {sch}.{tbl}" if sch else f"SELECT * FROM {tbl}"
                        cols = struct.get("columns") or a.get("fields") or []
                        if cols:
                            c0 = cols[0]
                            key_field_built = c0.get("name") if isinstance(c0, dict) else str(c0)
                        break
            if query_built:
                break

            # 2. 匹配 DataSource 表
            for ds in all_datasources:
                if ds.get("type") == "SQL":
                    for tb in (ds.get("tables") or []):
                        tbl_name = tb.get("table") or ""
                        schema_tbl = f"{tb.get('schema', '')}.{tbl_name}"
                        if ms == tbl_name or ms == schema_tbl or ms.endswith(f".{tbl_name}") or tbl_name.endswith(f".{ms}"):
                            sch = tb.get("schema") or "SQLUser"
                            query_built = f"SELECT * FROM {sch}.{tbl_name}" if sch else f"SELECT * FROM {tbl_name}"
                            cols = tb.get("columns") or []
                            if cols:
                                c0 = cols[0]
                                key_field_built = c0.get("name") if isinstance(c0, dict) else str(c0)
                            break
                    if query_built:
                        break

        if not query_built and mapped_sources:
            for ms in mapped_sources:
                if not ms.startswith("DS") and not ms.startswith("M") and not ms.startswith("TG"):
                    schema_prefix = "SQLUser." if "." not in ms else ""
                    query_built = f"SELECT * FROM {schema_prefix}{ms}"
                    break

        if user_explicit_query:
            source_config["query"] = user_explicit_query
        elif query_built:
            source_config["query"] = query_built
        elif ds_obj and (ds_obj.get("config") or {}).get("query"):
            source_config["query"] = ds_obj["config"]["query"]
        else:
            source_config["query"] = "SELECT * FROM SQLUser.PatientSource"

        if user_explicit_key:
            source_config["key_field"] = user_explicit_key
        elif key_field_built:
            source_config["key_field"] = key_field_built
        elif ds_obj and (ds_obj.get("config") or {}).get("key_field"):
            source_config["key_field"] = ds_obj["config"]["key_field"]
        else:
            source_config["key_field"] = "ID"
    if not mappings:
        mappings = repository.list_mappings()
    if not mappings:
        return error("缺少 mappings"), 400

    try:
        req = PipelineGenerateRequest(**{**body, "mappings": mappings})
    except Exception as exc:
        return error(f"参数校验失败: {exc}"), 400
    repository.save_mappings([m.model_dump() for m in req.mappings])

    # Agent C1：转换验证-修复（生成前，先保证转换关系合法：列存在/结构完整）
    mappings_effective = [m.model_dump() for m in req.mappings]
    trans_fix = transformation_validator.validate_and_fix_transformation(
        mappings_effective, assets=_c1_assets(source_id),
        target_models=_c1_target_models(mappings_effective))
    # 采纳 C1 的修复进展：即使未完全解决也不丢弃已修正的映射（避免"全有或全无"把
    # 引用标注 transform=reference / 必填常量等修复回退，导致 FHIR 事务校验整体被拒）
    if trans_fix.get("mappings"):
        mappings_effective = trans_fix["mappings"]
    if trans_fix["status"] != "ok":
        logger.warning("转换验证-修复未完全解决（已采纳部分修复）: %s", trans_fix["message"])
    repository.save_mappings(mappings_effective)

    # —— 术语映射**盘点**（术语服务器 = 术语转换的唯一事实源；**默认放行**）——
    # 位置：C1 修完映射之后、Agent B（LLM 管道设计）之前。
    # 2026-09-18 口径：缺映射不再中止（缺什么进"待办清单"，运行期由共享 BO 降级处理）；
    # 仅 `strict_terms=true` 时按 TERM_MAP_INCOMPLETE 中止（合规场景）。
    _strict = bool(body.get("strict_terms"))
    _tgate = _term_precheck(mappings_effective, strict=_strict)
    if not _tgate.get("ok"):
        logger.error("单管道生成被术语**严格**预检中止: %s", _tgate.get("message"))
        return error(_tgate.get("message") or "术语预检未通过（严格模式）", data={
            "reason": "TERM_MAP_INCOMPLETE",
            "term_catalog": _term_catalog_brief(_tgate.get("gate") or {})}), 400
    _term_catalog = _term_catalog_brief(_tgate.get("gate") or {})
    _term_todo = list(_tgate.get("todo") or [])
    if _tgate.get("message"):
        logger.warning("术语待办（默认放行，运行期降级）: %s", _tgate.get("message"))

    mappings_json = json.dumps(mappings_effective, ensure_ascii=False)
    # FHIR 源配置兜底：统一从数据源运行契约读取（datasource_runtime 归一 endpoint/auth），
    # 保证 FHIRSyncService 能抓取（前端可能取不到字段的历史问题由此根治）
    if not (config or {}).get("endpoint"):
        for _ds in repository.list_datasources():
            if _ds.get("type") == "FHIR":
                _rc = (_ds.get("runtime") or {}).get("connection") \
                    or repository.datasource_runtime(_ds)["connection"]
                _ep = _rc.get("endpoint") or ""
                if _ep:
                    config = {
                        **(config or {}),
                        "endpoint": _ep,
                        "username": (config or {}).get("username") or _rc.get("username") or "superuser",
                        "password": (config or {}).get("password") or _rc.get("password") or "SYS",
                    }
                    break
    config_json = json.dumps(config, ensure_ascii=False) if config else ""

    # Agent B（LLM）：设计数据管道拓扑——AI 决定组件构成与顺序，注册表只补参数。
    # LLM 不可用时不再静默走规则；如确需规则兜底（如无 LLM key 的演示环境）须显式 allow_rule_fallback。
    allow_rule_fallback = bool(body.get("allow_rule_fallback"))

    # —— 增量（P0a/P1/P3，与多管道同口径）——
    # ① 身份必须完整：缺源数据源/目标 → **拒绝生成**（否则会登记成 PIPE_<skill> 幽灵实例）
    if not (str(source_id or "").strip() and str(target_id or "").strip()):
        _msg0 = pipeline_instances.pipeline_id_fallback_note(source_id, target_id)
        logger.error("单管道生成中止（管道身份不完整）: %s", _msg0)
        return error(_msg0, data={"reason": "PIPELINE_IDENTITY_MISSING"}), 400
    # ⓪ 保护：**存在其它有效管道**时改走多管道路径（P1b 会把它们按存储定义自动并入）。
    #   否则"单管道 = 整份替换 Production"会把现存的别的管道清掉 —— 实测缺陷（2026-09-20）：
    #   用户先建了 SQL→SOAP，再生成 SQL→FHIR 时前端只提交 1 组 → 走单管道路径 →
    #   SQL-SOAP 的 3 个组件整组消失、其实例变 superseded（用户视角："管道消失了"）。
    try:
        _others = [r for r in pipeline_instances.list_instances()
                   if r.get("status") != "superseded"
                   and str(r.get("id")) != pipeline_instances.pipeline_id(source_id, target_id)
                   and (r.get("ai_components") or [])]
    except Exception:  # noqa: BLE001 - 读不到既有管道时按"没有"处理（退化为原单管道语义）
        _others = []
    if _others:
        logger.info("单管道请求：检测到其它 %d 条有效管道 %s → 转多管道路径（自动并入，不整份替换）",
                    len(_others), [str(r.get("id")) for r in _others])
        _multi2 = _generate_multi_pipelines([{
            "source_type": source_type, "source_id": source_id,
            "target_type": target_type, "target_id": target_id,
            "mappings": mappings_effective}])
        if _multi2.get("result") != "OK":
            if _multi2.get("gate") is not None:
                return error(_multi2.get("message") or "术语预检未通过", data={
                    "reason": _multi2.get("reason") or "TERM_MAP_INCOMPLETE",
                    "term_catalog": _multi2.get("gate") or {}}), 400
            if _multi2.get("reason") == "PIPELINE_IDENTITY_MISSING":
                return error(_multi2.get("message") or "管道身份不完整", data={
                    "reason": "PIPELINE_IDENTITY_MISSING"}), 400
            return error(f"管道生成失败: {_multi2.get('result')}"), 500
        if _multi2.get("unchanged"):
            return success(_multi2, "所有数据管道均已存在且未变更（未重新生成）")
        return success(_multi2, "多管道已生成并启动")

    # ② 入参未变 且 组件仍在 Production 在位 → 跳过重渲染/重启（无变更；force=true 时不跳过）
    # ⚠ 签名口径必须与多管道路径**完全一致**（`_inc_input_signature`，含运行契约 extra）——
    #   否则两条路径算出的签名不同：单管道重放永远判"变更"→ 重渲染 → 把别的管道整份换掉
    #   （实测缺陷）。
    _force_regen1 = bool(body.get("force"))
    _sig_in1 = _inc_input_signature({
        "source_id": source_id, "target_id": target_id,
        "source_type": source_type, "target_type": target_type,
        "mappings": mappings_effective,
        "_rt_src": repository.datasource_runtime(ds_obj) if ds_obj else None,
        "_rt_tgt": repository.target_runtime(locals().get("tg")) if locals().get("tg") else None})
    _unch1, _rec1 = (False, None) if _force_regen1 else pipeline_instances.is_unchanged(
        source_id, target_id, _sig_in1)
    if _unch1:
        _prod1 = _running_items()
        _miss1 = [str(n) for n in ((_rec1 or {}).get("component_names") or [])
                  if str(n) not in _prod1]
        if _prod1 and not _miss1:
            logger.info("增量生成（单管道）：%s 输入未变且组件在位 → 跳过重渲染/重启（无变更）",
                        (_rec1 or {}).get("id"))
            rec1 = dict(_rec1 or {})
            rec1["signature"] = _sig_in1
            return success({
                "result": "OK", "unchanged": True, "render_skipped": True,
                "production": "demo.DataflowProduction",
                "validation": {"ok": True, "error_count": 0, "warning_count": 0, "issues": [],
                               "note": "增量生成：输入未变、组件已在位 → 未重渲染/重启"},
                "pipelines": [rec1],
            }, "该数据管道已存在且未变更（跳过重新生成）")
        logger.warning("增量生成（单管道）：组件缺失 %s → 走完整重渲染自愈", _miss1[:5])

    ai_components: list[dict] | None = None
    p_result: dict | None = None
    design_skill = None
    try:
        # 上下文精准化：源模型/运行契约收敛到本组映射涉及的表（组件构成仍由 Agent 决策）
        _src_tables = _source_tables_from_mappings(mappings_effective)
        _rt_src_scoped, _src_models_scoped = _scope_source_context(
            (ds_obj or {}).get("runtime") if ds_obj else None, source_models, _src_tables)
        p_result = llm_client.recommend_pipeline(
            mappings_effective,
            source_type=source_type, target_type=target_type,
            available_components=AVAILABLE_COMPONENTS,
            source_models=_src_models_scoped,
            target_models=target_models,
            transformation_plan=plan or {"mappings": mappings_effective},
            # 源/目标运行契约（归一 runtime）注入，驱动 AI 基于轮询/投递能力决策（密码由 llm_client 脱敏）
            source_runtime=_rt_src_scoped,
            target_runtime=(tg.get("runtime") if locals().get("tg") else None),
            # 已登记管道事实（同一 (源,目标) → 更新既有管道而非新增；决策仍归 LLM）
            existing_pipelines=pipeline_instances.existing_pipelines_brief(),
            # Plan 模式：SQL→FHIR 时只让 Agent B 出拓扑（聚合 BP 由计划+逐方法生成）
            bp_plan_mode=_bp_plan_mode_for(source_type, target_type))
        pipeline = p_result.get("pipeline")
        design_skill = p_result.get("design_skill") if isinstance(p_result, dict) else None
        if pipeline and pipeline.get("components"):
            ai_components = pipeline["components"]
            logger.info("Agent B 数据管道设计 Agent 选 Skill=%s（AI 决策 %d 组件）: %s",
                        design_skill, len(ai_components),
                        [c.get("type") for c in ai_components])
        else:
            raise llm_client.AgentError("Agent B 未返回任何组件")
    except Exception as exc:
        if not allow_rule_fallback:
            logger.error("Agent B 数据管道 AI 生成失败（不静默回退规则）: %s", exc)
            return error(f"Agent B 数据管道 AI 生成失败: {exc}"
                         "（如需以类型注册表规则兜底，请显式传 allow_rule_fallback=true）"), 500
        logger.warning("Agent B 调用失败，已显式允许规则兜底: %s", exc)

    # 校正 mapping.target_type：前端确认/plan 常缺省存成 "DB"（MappingItem 默认值），
    # 会导致 TransformProcess 对 SOAP 目标走错 DB 路由。统一按推导的最终 target_type 回写
    # （SOAP 目标→SOAPOp，DB 目标→SQLOp），并持久化到 ^demo.Mapping。
    for _m in mappings_effective or []:
        if isinstance(_m, dict) and _m.get("id"):
            _m["target_type"] = target_type
            _write_mapping_patch(_m)

    # —— sql2fhir-patient-tx 分发（Skill 布局 executor + Agent 生成 BP，无平台预置 BP）——
    # 触发条件：目标=FHIR、Agent B 已选 design_skill=sql2fhir-patient-tx（AI 决策），
    # 且源为 SQL 并映射覆盖患者主表。布局与 BP 由 Skill/Agent 链路完成，平台只做参数化与准入。
    # C2 目标落地预期（布局驱动，sql2fhir 分支内按布局声明赋值）：{FHIR 资源: 至少条数}
    _expect_targets: dict[str, int] = {}
    _bp_mode_meta: dict = {}          # Plan 链 meta（bp_mode/方法/单测汇总），响应回显（可审计）
    sql2fhir_flow = False
    if (target_type == "FHIR" and design_skill == "sql2fhir-patient-tx"
            and ai_components is not None):
        from backend.services import sql2fhir_executor as _sfx
        try:
            maps_layout, meta = _resolve_sql_source_tables(mappings_effective, source_id)
            layout = _sfx.derive_sql2fhir_layout(maps_layout, meta)
            layout = _sfx.enrich_layout_with_mappings(layout, maps_layout)
            if layout.get("unmapped_query_bos"):
                return error("sql2fhir 布局不完整：子资源查询 BO 缺少映射 id "
                             f"{layout['unmapped_query_bos']}（子资源会组装成空资源 → "
                             "FHIR 必填元素缺失 → 整个 Bundle 事务回滚）"), 500
        except Exception as _lexc:  # noqa: BLE001
            logger.error("sql2fhir 布局推导失败（Skill executor）: %s", _lexc, exc_info=True)
            return error("sql2fhir 布局推导失败（Skill executor）: %s%s" % (
                _lexc, _missing_patient_root_hint(mappings_effective, source_id))), 500
        _save_sql2fhir_layout(layout)
        # 目标落地预期 = 布局声明的资源（每条至少 1 个）：子资源全 0 时不能判"通过"
        _expect_targets = {str(r): 1
                           for r in (layout.get("bundle", {}).get("resource_order") or []) if str(r)}
        _tg_rt = (locals().get("tg") or {}).get("runtime") if locals().get("tg") else None
        _bp_ok, _bp_msg, _bp_meta = _ensure_sql2fhir_bp_dispatch(
            p_result, mappings_effective, source_type, target_type,
            AVAILABLE_COMPONENTS,
            (ds_obj or {}).get("runtime") if ds_obj else None, _tg_rt,
            layout=layout, design_skill="sql2fhir-patient-tx",
            facts={"source": source_type, "target": target_type})
        _bp_mode_meta = _bp_meta
        if not _bp_ok:
            logger.error("Agent 生成 BP 失败（sql2fhir-patient-tx）: %s", _bp_msg)
            return error(f"Agent 生成 BP 失败（sql2fhir-patient-tx）: {_bp_msg}"), 500
        ai_components = _sfx.build_sql2fhir_components(layout, source_config, target_config)
        sql2fhir_flow = True
        logger.info("sql2fhir-patient-tx 布局+Agent BP 就绪（%d 组件）", len(ai_components))

    # 依据 Agent B（LLM）组件构建拓扑；ai_components=None（规则兜底）时才用纯注册表规则
    if sql2fhir_flow:
        # sql2fhir 拓扑由 Skill executor 按布局生成（含 Agent 编译的 BP），不再走注册表参数化重建
        topology = {"production": "demo.DataflowProduction", "components": ai_components,
                    "design_skill": "sql2fhir-patient-tx", "ai_supplemented": []}
    else:
        topology = build_pipeline_topology(
            mappings_effective,
            source_type=source_type, target_type=target_type,
            suggested_types=None,
            source_config=source_config, target_config=target_config,
            ai_components=ai_components)
    # 管道类别（= Agent B 选定的设计 Skill）：渲染为 Ens 业务主机 Category（分组/启停/LLM 上下文的事实）
    _category = pipeline_instances.category_of(
        topology.get("design_skill") or design_skill, source_type, target_type)
    _stamp_categories(topology, _category)
    # 本拓扑是否真的生成转换 BP（TransformProcess 主机）：Skill 自带聚合 BP 的组（sql2fhir）没有
    # 它 → `bp` / `bp_target` 一律不登记（否则留下无读者的死配置，口径与多管道一致）
    _has_router = any(str(c.get("type")) == "TransformProcess"
                      for c in (topology.get("components") or []))
    # 生成期自检 + 布局派发名对齐（单管道不改名，但口径与多管道一致：BP 读显式派发名）
    _sp_items, _sp_item = _dispatch_targets(
        topology.get("components") or [], None, target_config)
    _assert_dispatch_targets(
        [{"_category": _category, "_bp_items": _sp_items, "_bp_item": _sp_item}],
        topology.get("components") or [])
    if sql2fhir_flow and locals().get("layout") and isinstance(layout, dict):
        layout["http_bo"] = _sp_item or layout.get("http_bo") or ""
    # 许可调度：放不下的管道分组标 enabled=false（组件照旧生成、初始停用，UI 一键切换）
    _plan = _license_plan(topology)
    _susp = _mark_suspended_components(topology, _plan)
    if _susp:
        logger.warning("许可调度：以下组件生成后处于停用状态 %s", _susp)
    # 本组被调度停用（组件已生成、未启动）→ 目标必然无数据：取消落地预期并跳过落地判定，
    # 否则"已生成但停用"会被判为生成失败（实测：多/单管道第二条被调度停用 → 500 假失败）
    _susp_cats = {str(c) for c in (_plan.get("suspended") or [])}
    _self_suspended = str(_category) in _susp_cats
    if _self_suspended:
        logger.warning("许可调度：本管道（%s）生成后处于停用状态 → 本次不做目标落地判定",
                       _category)
        _expect_targets = {}
    _effect_target_types = [] if _self_suspended else [target_type]
    _save_pipeline_topology(topology)
    # sql2fhir 布局**按聚合 BP 实例**写入（多管道隔离；全局键已在上文写过作历史兼容）——
    # 单管道时 BP 名即 `SqlFhirPatientTxProcess`，与多管道同口径，便于两口径混用。
    _agg_bp = next((str(_c.get("name")) for _c in (topology.get("components") or [])
                    if str(_c.get("type")) == "PatientTxProcess" and _c.get("name")), "")
    if _agg_bp and locals().get("layout"):
        _save_sql2fhir_layout(layout, _agg_bp)
    _save_fhir_runtime_config(topology, target_config,
                              mappings=mappings_effective, source_id=source_id)
    # 以及管道目标类型（TransformProcess 路由权威依据，防止 mapping.target_type 缺省 DB 误路由）
    # 转换 BP 的**自身配置**（单管道只有一条 → BP 名固定 TransformProcess）：
    # demo.TransformProcess.OnRequest 首选 ^demo.Config("bp", ..%ConfigName)，
    # 不再依赖全局单值键（后者在多管道下会被后来者覆盖）；同时写：
    #  - bp_target[源BS] = BP 名（供代码内显式投递的源 BS 读取，如 demo.FHIRService）
    #  - pipe[源BS] / pipeline.* （兼容历史路径与兜底）
    if mappings_effective:
        import iris
        conn = iris_connector.get_connection()
        try:
            native = iris.createIRIS(conn)
            _m0 = mappings_effective[0]
            _pipe = {"mapping": _m0.get("id", ""), "target_type": target_type}
            if target_type == "SOAP":
                _pipe["service"] = (target_config or {}).get("service") or "default"
            else:
                _pipe["table"] = _m0.get("target_table") or ""
            # 派发目标（显式主机名，口径与多管道一致）：单管道虽不改名，仍写入以便
            # 与多管道共用同一 BP 读法（生成期自检也会校验其存在性）
            _sp_items, _sp_item = _dispatch_targets(
                topology.get("components") or [], None, target_config)
            if _sp_items:
                _pipe["items"] = _sp_items
            if _sp_item:
                _pipe["item"] = _sp_item
            _pipe_json = json.dumps(_pipe, ensure_ascii=False)
            # ① 权威：本 BP 自己的参数（BP 内用 ..%ConfigName 读）
            #    ⚠ 仅当本拓扑**确有** TransformProcess 主机时才登记（口径同多管道）
            if _has_router:
                native.set(_pipe_json, "^demo.Config", "bp", "TransformProcess")
            _ids_json = json.dumps([str(x.get("id")) for x in mappings_effective if x.get("id")],
                                   ensure_ascii=False)
            for _bn in _source_bs_names(topology):
                # ② 源 BS → BP 投递表（仅本拓扑有转换 BP 时才有意义）
                if _has_router:
                    native.set("TransformProcess", "^demo.Config", "bp_target", _bn)
                native.set(_pipe_json, "^demo.Config", "pipe", _bn)
                # 本管道映射集：源 BS 据此只消费本管道在 FHIR 队列里的行（多管道不互相抢行）
                native.set(_ids_json, "^demo.Config", "bs_mappings", _bn)
            native.set(_m0.get("id", ""),
                       "^demo.Config", "pipeline", "active_mapping")
            native.set(target_type,
                       "^demo.Config", "pipeline", "target_type")
        finally:
            iris_connector.reset_connections()
    logger.info("已构建完整管道拓扑: %s", [c.get("type") for c in topology["components"]])

    # 许可预算：让不属于本次管道的旧管道业务主机让出许可单元（社区版仅 8 个）；
    # 本次拓扑内部由 apply_license_budget(groups=...) 做容量调度——放不下的分组在上面已标
    # enabled=false（生成但停用），因此不再出现"超容量即失败"，用户可在 UI 一键切换
    license_budget = pipeline_validator.apply_license_budget(
        keep_categories=_pipeline_categories(_category),
        groups=_topology_groups(topology))

    # 生成前连通性检查（源/目标运行契约门禁：参数/可达性错误在生成前拦截）
    try:
        _conn_rt_src = [ds_obj.get("runtime")] if ds_obj else []
        _conn_rt_tgt = [tg.get("runtime")] if (tg := locals().get("tg")) else []
        _conn_check = pipeline_validator.check_connection(sources=_conn_rt_src, targets=_conn_rt_tgt)
        if not _conn_check.get("ok"):
            _msg = "; ".join(i.get("message", "") for i in _conn_check.get("issues", [])[:3])
            return error(f"源/目标连通性检查未通过: {_msg}"), 500
    except Exception as _exc:  # noqa: BLE001 - 连通检查失败不阻断生成主流程
        logger.warning("连通性检查异常（跳过）: %s", _exc)

    try:
        result = iris_connector.class_method_value(
            "demo.PipelineGenerator", "Generate", mappings_json, config_json)
    except Exception as exc:
        logger.error("生成管道失败: %s", exc)
        return error(f"生成管道失败: {exc}"), 500

    # Agent C2：管道验证（拓扑/编译/启动/消息流转 + 目标落地效果 + 运行期错误分类）
    validation = pipeline_validator.run_pipeline_validation(
        topology, source_type, target_type, expect_targets=_expect_targets or None,
        effect_target_types=_effect_target_types)

    # 运行期错误回喂（有界 1 次）：FHIR 结构类错误 → C1 修映射后重生成；
    # BP 代码类错误 → 记录 method_updates 修复建议（自动 patch 由 Skill 通道执行）。
    if result != "OK" or not validation.get("ok"):
        try:
            _rt = (validation.get("results") or {}).get("runtime") or {}
            _kinds = _rt.get("kinds") or []
            if "fhir_schema" in _kinds:
                logger.info("运行期 FHIR 结构错误 → 回喂 C1 修复映射后重生成")
                _fix2 = transformation_validator.validate_and_fix_transformation(
                    mappings_effective, assets=_c1_assets(source_id),
                    target_models=_c1_target_models(mappings_effective))
                if _fix2.get("status") == "ok" and _fix2.get("mappings"):
                    for _m in _fix2["mappings"]:
                        if isinstance(_m, dict) and _m.get("id"):
                            _write_mapping_patch(_m)
                    mappings_effective = _fix2["mappings"]
                    result = iris_connector.class_method_value(
                        "demo.PipelineGenerator", "Generate",
                        json.dumps(mappings_effective, ensure_ascii=False), config_json)
                    validation = pipeline_validator.run_pipeline_validation(
                        topology, source_type, target_type,
                        expect_targets=_expect_targets or None,
                        effect_target_types=_effect_target_types)
                    logger.info("运行期回喂重生成结果: result=%s 验证ok=%s",
                                result, validation.get("ok"))
            elif "bp_code" in _kinds:
                from backend.services import generated_bp as _gbp
                logger.info("运行期 BP 代码错误 → method_updates 增量修复")
                _reps = _gbp.repair_from_runtime_errors(
                    mappings=mappings_effective, source_type=source_type,
                    target_type=target_type, available_components=AVAILABLE_COMPONENTS,
                    errors_text="\n".join(i.get("message", "") for i in (_rt.get("issues") or [])),
                    source_runtime=(ds_obj or {}).get("runtime") if ds_obj else None,
                    target_runtime=(locals().get("tg") or {}).get("runtime"))
                if _reps.get("ok"):
                    logger.info("BP 方法修复完成: %s", _reps.get("applied"))
                    try:
                        iris_connector.class_method_value("Ens.Director", "StopProduction", 10)
                        iris_connector.class_method_value("Ens.Director", "StartProduction",
                                                          "demo.DataflowProduction")
                    except Exception as _se:  # noqa: BLE001
                        logger.warning("修复后重启 Production 失败（不阻断）: %s", _se)
                    result = iris_connector.class_method_value(
                        "demo.PipelineGenerator", "Generate",
                        json.dumps(mappings_effective, ensure_ascii=False), config_json)
                    validation = pipeline_validator.run_pipeline_validation(
                        topology, source_type, target_type,
                        expect_targets=_expect_targets or None,
                        effect_target_types=_effect_target_types)
                else:
                    logger.warning("BP 方法修复未通过: %s", _reps.get("message"))
        except Exception as _re:  # noqa: BLE001 - 回喂失败不阻断主流程
            logger.warning("运行期错误回喂失败（不影响主流程）: %s", _re)

    # 管道验证-修复闭环：生成失败或验证不通过时，由 Agent C2 判断并分层修复
    fix_result: dict | None = None
    if result != "OK" or not validation["ok"]:
        logger.warning("生成/管道验证未通过（生成=%s 验证错误=%d），进入管道验证-修复闭环",
                       result, validation["error_count"])

        def _generate_with_topology(topo, mp):
            """按给定拓扑 + 映射重新生成（供管道验证-修复 Agent 的 generate_fn）。"""
            _save_pipeline_topology(topo)
            mp_json = json.dumps(mp, ensure_ascii=False)
            return iris_connector.class_method_value(
                "demo.PipelineGenerator", "Generate", mp_json, config_json)

        fix_result = validate_agent.validate_and_fix_pipeline(
            mappings_effective, topology,
            generate_fn=_generate_with_topology,
            source_type=source_type, target_type=target_type)
        if fix_result["status"] != "ok":
            return error(
                f"生成管道失败，验证-修复闭环未解决: {result}（{fix_result['message']}）"), 500
        result = "OK"
        validation = fix_result["report"]
        topology = fix_result["topology"]
        # C2 修复可能改写了拓扑（规则补齐/重建）→ 重新打类别，保证 Category 与最终拓扑一致
        _stamp_categories(topology, _category)
        logger.info("管道验证-修复闭环完成: 状态=%s 轮次=%d",
                    fix_result["status"], len(fix_result["rounds"]))

    # 管道实体登记（受管理持久对象）：同一 (source_id, target_id, design_skill) 更新，不新增
    pipeline_rec: dict | None = None
    pipeline_error = ""
    if result == "OK":
        try:
            _src_bs = _source_bs_names(topology)
            pipeline_rec = pipeline_instances.upsert_from_generation(
                source_id=source_id, target_id=target_id,
                source_type=source_type, target_type=target_type,
                design_skill=topology.get("design_skill") or design_skill,
                category_hint=_category,
                mapping_ids=[m.get("id") for m in mappings_effective if m.get("id")],
                components=topology.get("components"),
                # P0：存**未改名**原始组件（含 infra）+ 是否 sql2fhir + 布局 + 输入签名，
                #     供下次增量生成复用（跳过 Agent B、不重渲不重启）
                ai_components=ai_components or [],
                is_sql2fhir=bool(locals().get("sql2fhir_flow")),
                layout=(locals().get("layout") if locals().get("sql2fhir_flow") else None),
                signature=_sig_in1,
                applied_signature=pipeline_instances.component_signature(
                    (topology or {}).get("components"),
                    mapping_ids=[m.get("id") for m in mappings_effective if m.get("id")]),
                routes={"source_bs": _src_bs[0] if _src_bs else "",
                        "source_bs_names": _src_bs},
                ai={"driven": ai_components is not None,
                    "rule_fallback": ai_components is None,
                    "supplemented": (topology or {}).get("ai_supplemented", [])},
                validation=validation)
            pipeline_instances.reconcile_states()
        except Exception as _pie:  # noqa: BLE001 - 登记失败不改变"生成成功"事实，但必须显式暴露
            pipeline_error = str(_pie)
            logger.error("管道实体登记失败（生成已成功，实体未登记）: %s", _pie)

    # 术语覆盖**复核**（只读盘点）：运行期由共享 BO 实时查；缺映射进"待办清单"（只告警不阻断）
    term_summary: dict = {}
    if result == "OK":
        try:
            term_summary = _term_summary(mappings_effective, topology, validation)
        except Exception as _tc:  # noqa: BLE001 - 复核异常也显式报告（不影响生成结果）
            logger.warning("生成后术语复核异常（不影响生成）: %s", _tc)
            term_summary = {"ok": False, "error": str(_tc),
                            "note": "术语复核异常；运行期由共享 BO 实时查询（缺映射会降级）"}

    # 配置收敛（口径同多管道）：清理指向不存在组件的 `bp` / `bp_target` 陈旧登记
    config_cleanup: dict = {}
    if result == "OK":
        try:
            config_cleanup = prune_stale_bp_config(
                keep_bps={"TransformProcess"} if _has_router else set(),
                keep_srcs=set(_source_bs_names(topology)) if _has_router else set())
        except Exception as _pce:  # noqa: BLE001 - 收敛失败不改变生成结果
            logger.warning("配置收敛异常（不影响生成）: %s", _pce)
            config_cleanup = {"error": str(_pce)}

    return success({
        "result": result,
        "production": "demo.DataflowProduction",
        "validation": validation,
        # BP 生成链（Plan → Execute）审计：bp_mode / 计划方法清单 / 每方法状态 / 单测汇总
        "bp_mode": _bp_mode_meta,
        # 许可预算：本次生成让旧管道组件让出的许可单元（社区版仅 8 个许可单元）
        "license_budget": license_budget,
        # 术语覆盖复核 + **待办清单**（运行期由共享 BO 实时查；缺映射默认降级、不阻断）
        "term_summary": term_summary,
        "term_todo": _term_todo,
        # 术语服务器目录/盘点摘要（唯一事实源：覆盖了多少码、缺什么、源库命名空间）
        "term_catalog": _term_catalog,
        # 配置收敛：本次生成清理掉的陈旧 `bp` / `bp_target` 登记（无对应组件）
        "config_cleanup": config_cleanup,
        # AI 驱动信息：Agent B（LLM）决定组件构成；supplemented 为注册表保底补齐（校验性，非替代）
        "ai": {
            "driven": ai_components is not None,
            "rule_fallback": ai_components is None,
            "components": [c.get("type") for c in (ai_components or [])],
            "supplemented": (topology or {}).get("ai_supplemented", []),
            # 红线审计：C2 修复中规则是否改写了 AI 决策的组件构成（True 则结果非纯 AI）
            "c2_rule_rebuilt": bool((fix_result or {}).get("rule_applied")),
        },
        # 管道实体（受管理对象）：本次生成新建/更新的管道；同身份重复生成只更新不新增
        "pipeline": pipeline_rec,
        "pipelines": [pipeline_rec] if pipeline_rec else [],
        "pipeline_error": pipeline_error,
    }, "数据管道已生成并启动")



@pipelines_bp.post("/run")
def run():
    """触发一次转换（FHIRService 定时拉取中，本接口确认状态）。"""
    result = iris_connector.class_method_value("demo.PipelineQuery", "TriggerFetch")
    return success({"result": result})


@pipelines_bp.get("/status")
def status():
    """管道运行状态。"""
    running = iris_connector.class_method_value("demo.PipelineQuery", "ProductionStatus")
    return success({"running": bool(running), "production": "demo.DataflowProduction"})


@pipelines_bp.get("/items")
def items():
    """Production 组件清单 + 许可容量 + 按管道类别分组（供「组件启停」面板与许可预算可视化）。

    背景：IRIS 社区版 `KeyLicenseUnits = 8`，每个 Ens 业务主机（BS/BP/BO）常驻占 1 个
    许可单元。组件的 Category 即所属数据管道类别（sql2fhir-patient-tx / sql2soap / shared…），
    categories 字段给出每个类别的组件数与已启用数，用于判断哪条管道正在占用许可。
    """
    rows = pipeline_validator.production_items()
    buckets: dict[str, dict] = {}
    for r in rows:
        cat = str(r.get("category") or "")
        if not cat:
            continue
        grp = buckets.setdefault(cat, {"category": cat, "items": [], "enabled_count": 0})
        grp["items"].append(r.get("name"))
        if int(r.get("enabled") or 0) == 1:
            grp["enabled_count"] += 1
    return success({
        "items": rows,
        "units": pipeline_validator.license_units(),
        "enabled_count": len([r for r in rows if int(r.get("enabled") or 0) == 1]),
        "categories": sorted(buckets.values(), key=lambda g: g["category"]),
    })


@pipelines_bp.post("/items/toggle")
def toggle_items():
    """启用/禁用 Production 组件（等价于手工切换管道占用许可）。

    body: {"names": ["SQLService_Patient"], "enabled": true}
      或 {"name": "SQLService_Patient", "enabled": false}
    注意：启用组件会常驻占用 1 个许可单元；启用过多会导致后端无法连接 IRIS（接口 500）。
    生成流程会在启动前自动做许可预算（让非本次管道组件让路）。
    """
    body = request.get_json(silent=True) or {}
    names = body.get("names") or ([body["name"]] if body.get("name") else [])
    if not names:
        return error("请指定要启停的组件名（name 或 names）"), 400
    enabled = bool(body.get("enabled"))
    res = pipeline_validator.set_items_enabled(names, enabled)
    if not res.get("ok"):
        return error(f"组件启停失败: {res.get('message') or res.get('result')}"), 500
    return success(res, "组件已启用" if enabled else "组件已禁用")


@pipelines_bp.get("/instances")
def pipeline_instances_list():
    """数据管道清单（受管理持久实体）+ 按管道类别分组 + 许可占用。

    管道实体 = 「源 + 目标 + 设计 Skill」的稳定对象：组件 Category 即管道类别。
    status 按 Production 实际 Enabled 回写（active/suspended），不是生成时的历史快照。
    """
    prod_items = pipeline_validator.production_items()
    recs = pipeline_instances.reconcile_states(production_items=prod_items)
    return success({
        "items": recs,
        "groups": pipeline_instances.group_by_category(recs, prod_items),
        "units": pipeline_validator.license_units(),
        "enabled_count": len([r for r in prod_items if int(r.get("enabled") or 0) == 1]),
        "active_count": len([r for r in recs if r.get("status") == "active"]),
    })


@pipelines_bp.get("/instances/<pid>")
def pipeline_instance_detail(pid):
    """单条数据管道详情（含组件清单、路由、AI 决策与最近验证结果）。"""
    rec = pipeline_instances.get_instance(pid)
    if not rec:
        return error(f"管道实例不存在: {pid}"), 404
    return success(rec)


@pipelines_bp.delete("/instances/<pid>")
def pipeline_instance_delete(pid):
    """删除管道实体记录（仅删实体，不改动 Production 组件与其启停状态）。"""
    if not pipeline_instances.get_instance(pid):
        return error(f"管道实例不存在: {pid}"), 404
    pipeline_instances.delete_instance(pid)
    return success({"id": pid}, "管道实体已删除（Production 组件未改动）")


@pipelines_bp.post("/instances/<pid>/enable")
def pipeline_instance_enable(pid):
    """启用一条数据管道（启用其业务主机组件，占用许可单元）。

    注意许可上限：启用后若「已启用组件数 + 1（后端连接）」超过 license_units()，
    后端连接会被 IRIS 拒绝（接口 500）——此时请先停掉另一条管道。
    """
    res = pipeline_instances.set_enabled(pid, True)
    if not res.get("ok"):
        return error(f"启用管道失败: {res.get('message')}"), 500
    return success(res, "管道已启用")


@pipelines_bp.post("/instances/<pid>/disable")
def pipeline_instance_disable(pid):
    """停用一条数据管道（停其业务主机组件，释放许可单元；无其它活动管道时连共享组件一起停）。"""
    res = pipeline_instances.set_enabled(pid, False)
    if not res.get("ok"):
        return error(f"停用管道失败: {res.get('message')}"), 500
    return success(res, "管道已停用（许可单元已释放）")


@pipelines_bp.post("/instances/sync")
def pipeline_instance_sync():
    """从 Production（组件 Category）+ 路由表回填管道实体（历史环境迁移/修复）。

    适用：升级前生成过的 Production 只有组件没有管道实体；或手工改过组件类别需要归位。
    """
    res = pipeline_instances.sync_from_production()
    return success({"created": res.get("created"), "updated": res.get("updated"),
                    "categories": res.get("categories"),
                    "instances": res.get("instances")}, "管道实体已同步")


@pipelines_bp.get("/logs")
def logs():
    """消息流转日志（Ens.MessageHeader 真实消息历史）。"""
    count = request.args.get("count", 50, type=int)
    data = iris_connector.class_method_value("demo.PipelineQuery", "GetLogs", count)
    try:
        items = json.loads(data or "[]")
    except json.JSONDecodeError:
        items = []
    return success({"items": items})


@pipelines_bp.post("/validation-issues")
def save_validation_issue_route():
    """沉淀一条验证-修复经验到 ^demo.ValidationIssue（外部工具/审计用）。

    请求体: {"pattern": "...", "resolution": "...", "source": "..."}
    """
    body = request.get_json(silent=True) or {}
    pattern = (body.get("pattern") or "").strip()
    resolution = (body.get("resolution") or "").strip()
    if not pattern or not resolution:
        return error("缺少 pattern/resolution"), 400
    result = pipeline_validator.save_validation_issue(
        pattern, resolution, body.get("source") or "api")
    if not result.get("ok"):
        return error(result.get("message") or "沉淀失败"), 500
    return success({"pattern": pattern[:60]}, "经验已沉淀")


@pipelines_bp.post("/validation-issues/polish")
def polish_validation_issues_route():
    """LLM 研读润色 + 去重验证经验（^demo.ValidationIssue → 结构化知识）。

    供宿主导出到 Obsidian 知识库使用；LLM 失败返回明确错误（不静默回退原文）。
    """
    body = request.get_json(silent=True) or {}
    limit = min(max(int(body.get("limit") or 500), 1), 1000)
    items = pipeline_validator.load_validation_issues(limit)
    try:
        polished = llm_client.polish_validation_issues(items)
    except llm_client.AgentError as exc:
        return error(f"知识润色 Agent（LLM）失败: {exc}"), 500
    except Exception as exc:  # noqa: BLE001
        logger.error("知识润色失败: %s", exc)
        return error(f"知识润色失败: {exc}"), 500
    return success({"items": polished, "count": len(polished),
                    "raw_count": len(items)}, "知识润色完成")


@pipelines_bp.get("/validation-issues")
def list_validation_issues():
    """验证-修复 Agent 积累的经验（^demo.ValidationIssue）。

    供审计/导出知识库用（外部脚本可据此转为 IRIS-Dev-Vault 的 04-Pitfalls 笔记）。
    """
    limit = request.args.get("limit", 100, type=int)
    limit = min(max(limit, 1), 500)
    items = pipeline_validator.load_validation_issues(limit)
    return success({"items": items, "count": len(items)})


def _mock_patients(count: int) -> list[dict]:
    """生成 count 条模拟 Patient（lastUpdated > 增量游标，触发增量同步）。"""
    families = ["张", "李", "王", "刘", "陈", "杨", "赵", "黄", "周", "吴"]
    givens = ["伟", "芳", "娜", "敏", "静", "磊", "军", "洋", "勇", "艳", "杰", "娟", "涛", "明", "超"]
    cities = ["北京", "上海", "广州", "深圳", "成都", "杭州"]
    now_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    patients = []
    stamp = int(time.time() * 1000) % 100000
    for i in range(count):
        pid = f"M{stamp}{i}"
        city = random.choice(cities)
        patients.append({
            "resourceType": "Patient",
            "id": pid,
            "meta": {"lastUpdated": now_ts},
            "name": [{"family": random.choice(families), "given": [random.choice(givens)]}],
            "gender": "male" if i % 2 == 0 else "female",
            "birthDate": f"{random.randint(1970, 2000)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}",
            "telecom": [{"system": "phone", "value": f"13{random.randint(0, 9)}{random.randint(10000000, 99999999)}"}],
            "address": [{"line": [f"{city}演示街道{random.randint(1, 99)}号"], "city": city}],
        })
    return patients


@pipelines_bp.post("/generate-mock")
def generate_mock():
    """演示：按需生成模拟 Patient 数据写入 FHIR（lastUpdated > 增量游标）。"""
    count = request.args.get("count", 3, type=int)
    count = min(max(count, 1), 10)
    patients = _mock_patients(count)
    try:
        from backend.services import fhir_client
        result = fhir_client.submit_bundle(
            FHIRConfig.BASE_URL, FHIRConfig.USERNAME, FHIRConfig.PASSWORD, patients)
    except Exception as exc:
        logger.error("生成模拟数据失败: %s", exc)
        return error(f"生成模拟数据失败: {exc}"), 500
    ok = sum(1 for e in result.get("entry", [])
             if e.get("response", {}).get("status", "").startswith("2"))
    ids = [p["id"] for p in patients]
    return success({"count": ok, "ids": ids},
                   f"已生成 {ok} 条模拟数据，增量同步将自动抓取并转换")


@pipelines_bp.get("/mappings")
def pipeline_mappings():
    """已生效的转换关系（Production 正在执行的）。"""
    data = iris_connector.class_method_value("demo.PipelineQuery", "GetMappings")
    try:
        items = json.loads(data or "[]")
    except json.JSONDecodeError:
        items = []
    return success({"items": items})


def _viewable_tables() -> list[str]:
    """当前可查看数据表（动态，由演示过程登记的数据源/目标决定，非写死）：

    - 已登记目标表/实体（_target_table_rows：DB 目标表 + SOAP 目标实体）
    - SQL 数据源下已选定的源资产表（便于查看源数据）
    仅保留 SQLUser schema 中物理存在的表（过滤 SOAP 非落库实体如 AddPatientResult）。
    按演示进度变化：未登记的源/目标不会出现在列表。
    """
    from backend.routes.targets import _target_table_rows

    tables = {tb.get("table") or "" for tb in _target_table_rows()}
    ds_map = {d.get("id"): d for d in repository.list_datasources()}
    for a in repository.list_assets():
        ds = ds_map.get(a.get("source_id"))
        if ds and ds.get("type") == "SQL":
            nm = a.get("name") or (a.get("structure") or {}).get("table") or ""
            if nm:
                tables.add(str(nm))
    rows = iris_connector.query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='SQLUser'")
    exist = {r[0] for r in rows}
    return sorted(t for t in tables if t and t in exist)


@pipelines_bp.get("/view-tables")
def view_tables():
    """可查看数据表列表（动态，见 _viewable_tables），供前端目标数据下拉使用。"""
    items = [{"table": t} for t in _viewable_tables()]
    return success({"items": items})


@pipelines_bp.get("/target-data")
def target_data():
    """目标表落库结果。"""
    table = request.args.get("table", "Patient")
    limit = request.args.get("limit", 50, type=int)
    from backend.routes.targets import _target_table_rows
    allowed = set(_viewable_tables())
    if table not in allowed:
        return error(f"目标表不存在或未注册: {table}"), 404
    data = iris_connector.class_method_value("demo.PipelineQuery", "GetTargetData", table, limit)
    try:
        items = json.loads(data or "[]")
    except json.JSONDecodeError:
        items = []
    return success({"items": items, "count": len(items)})
