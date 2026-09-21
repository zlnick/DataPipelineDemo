"""数据管道 API：生成、触发、监控。"""

import json
import logging
import random
import time
from datetime import datetime, timezone

from flask import Blueprint, request

from backend.config import Config, FHIRConfig
from backend.schemas.models import PipelineGenerateRequest
# P0 清理（2026-09-21）：`transformation_validator` / `type_registry` / `validate_agent` / `wsdl_importer`
# 已随 Step 4 迁到 services/pipeline_generate.py，routes 内不再使用 → 移除导入（其余保持原样）。
from backend.services import (iris_connector, llm_client, pipeline_instances,
                              pipeline_validator, repository)
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

# 「生成编排 + 拓扑构建」= backend/services/pipeline_generate.py（Step 4 重构）：类型注册表
# 参数化 / sql2fhir BP 生成链 / 许可调度 / 术语盘点 / 运行期配置落地 / 死配置收敛；
# 原先这些逻辑与路由混在一个 2779 行文件里，现按职责分层（routes 只留路由 + 薄入口）。
from backend.services.pipeline_generate import (
    build_pipeline_topology, prune_stale_bp_config, build_multi_pipeline_topology,
    save_pipeline_topology as _save_pipeline_topology, save_fhir_runtime_config as _save_fhir_runtime_config, save_sql2fhir_layout as _save_sql2fhir_layout, bp_plan_mode_on as _bp_plan_mode_on,
    bp_plan_mode_for as _bp_plan_mode_for, ensure_sql2fhir_bp_by_plan as _ensure_sql2fhir_bp_by_plan, ensure_sql2fhir_bp_dispatch as _ensure_sql2fhir_bp_dispatch, ensure_sql2fhir_bp as _ensure_sql2fhir_bp,
    build_from_ai_components as _build_from_ai_components, dedupe_components as _dedupe_components, common_component as _common_component, setting_value as _setting_value,
    dispatch_targets as _dispatch_targets, assert_dispatch_targets as _assert_dispatch_targets, config_subs as _config_subs, unkeyed_operations as _unkeyed_operations,
    auto_join_existing_pipelines as _auto_join_existing_pipelines, write_mapping_patch as _write_mapping_patch, group_components as _group_components, needs_terminology as _needs_terminology,
    terminology_component as _terminology_component, is_infra_component as _is_infra_component, pipeline_categories as _pipeline_categories, topology_groups as _topology_groups,
    mark_suspended_components as _mark_suspended_components, converge_component_enabled as _converge_component_enabled, license_plan as _license_plan, term_catalog_brief as _term_catalog_brief,
    term_precheck as _term_precheck, term_summary as _term_summary, missing_patient_root_hint as _missing_patient_root_hint, reset_ens_messages_for_generate as _reset_ens_messages_for_generate,
    generate_multi_pipelines as _generate_multi_pipelines,
)

from backend.utils import error, success

logger = logging.getLogger(__name__)

pipelines_bp = Blueprint("pipelines", __name__, url_prefix="/api/pipelines")

# P0（2026-09-21）：本文件原先各自算一份组件枚举（与 services/pipeline_generate.AVAILABLE_COMPONENTS
# 重复、可能漂移）；实测 routes 内已无使用者、外部亦无引用 → 删除，统一由服务层持有唯一一份。


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


# P0（2026-09-21）已删除两个**全仓 0 引用**的死函数：`_source_bs_names`、
# `_all_pipeline_source_bs_names`（判码缓存刷新已改走共享 BO，不再需要按需暂停源 BS）。


@pipelines_bp.post("/generate")
def generate():
    """生成并启动数据管道。支持异构组合（source_type/target_type + target_config）与多管道（pipelines 列表）。"""
    body = request.get_json(silent=True) or {}
    # 多管道模式：body.pipelines = [组1, 组2, ...]，单 Production 内多套 BS/BP/BO
    pipelines_arg = body.get("pipelines")
    if pipelines_arg:
        try:
            multi = _generate_multi_pipelines(
                pipelines_arg, strict_terms=bool(body.get("strict_terms")),
                force=bool(body.get("force")),
                allow_rule_fallback=bool(body.get("allow_rule_fallback")))
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
    mappings_effective = [m.model_dump() for m in req.mappings]

    # —— 方案 A Step 3：**单管道 = 多管道的特例**（唯一实现）——
    # 本次提交 1 组还是 N 组，都走同一条 `_generate_multi_pipelines(groups)`：
    # 身份校验 / 同身份去重 / 既有管道自动并入 / C1 转换验证 / 术语门禁 / Agent B /
    # 增量复用（P1）与免重启（P3） / 许可调度 / 拓扑渲染 / 验证-修复闭环 / 实例登记
    # —— 全部只有一份实现。原先两条平行管线各自实现同一批语义，任何一处漂移都是
    # **静默缺陷**（S2 签名口径 / T1 单管道整份替换 / T2 复用渲染丢目标 BO / T3 存储定义污染
    # 的共同成因），此处从结构上消除：单管道走的一定是「组数 = 1」的同一条链路。
    _m2 = _generate_multi_pipelines([{
        "source_type": source_type,
        "source_id": source_id,
        "target_type": target_type,
        "target_id": target_id,
        "source_config": config or {},
        "target_config": target_config or {},
        "mappings": mappings_effective,
    }], strict_terms=bool(body.get("strict_terms")), force=bool(body.get("force")),
        allow_rule_fallback=bool(body.get("allow_rule_fallback")))
    if _m2.get("result") != "OK":
        if _m2.get("gate") is not None:                 # 术语严格模式（strict_terms）
            return error(_m2.get("message") or "术语预检未通过（严格模式）", data={
                "reason": _m2.get("reason") or "TERM_MAP_INCOMPLETE",
                "term_catalog": _m2.get("gate") or {}}), 400
        if _m2.get("reason") == "PIPELINE_IDENTITY_MISSING":
            return error(_m2.get("message") or "管道身份不完整", data={
                "reason": "PIPELINE_IDENTITY_MISSING"}), 400
        return error(f"管道生成失败: {_m2.get('result')}"), 500

    if _m2.get("unchanged"):
        return success(_m2, "该数据管道已存在且未变更（跳过重新生成）")
    return success(_m2, "数据管道已生成并启动")





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
