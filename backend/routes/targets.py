"""转换目标（数据目标）API。

分步流程（JDBC 通用连接，不依赖 Native SDK）：
  ① 添加数据目标（JDBC 连接信息）→ ② 联通测试
  → ③ 选择 schema → ④ 列出表 → ⑤ 勾选目标表 → ⑥ 分析列结构

返回给 AI 推荐 / 管道投放的目标列表（GET /api/targets）为扁平化目标表列表。
"""

import datetime
import logging

from flask import Blueprint, request

from backend.config import Config, to_internal_url
from backend.services import (connection_profiler, fhir_client, fhir_target_model,
                              interface_analyzer, iris_connector,
                              jdbc_client, profile_analyzer, repository, wsdl_importer)
from backend.services.llm_client import AgentError
from backend.utils import error, success

logger = logging.getLogger(__name__)

targets_bp = Blueprint("targets", __name__, url_prefix="/api/targets")


def _apply_target_ai(target_id: str, tables: list[dict], target_type: str,
                     facts: dict | None = None) -> list[dict]:
    """接口分析 Agent：目标表/实体语义 + direction（LLM）并写回。失败抛 AgentError。"""
    items = interface_analyzer.analyze_target_interfaces(target_type, tables, facts)
    by_name = {}
    for it in items:
        if it.get("name"):
            by_name[str(it["name"]).lower()] = it
    for t in tables:
        nm = t.get("entity_name") or t.get("table") or ""
        it = by_name.get(str(nm).lower())
        if not it:
            continue
        patch = {
            "ai_analysis": True,
            "ai_semantics": it.get("semantics", ""),
            "direction": it.get("direction", ""),
        }
        if it.get("reason"):
            patch["ai_reason"] = it["reason"]
        repository.enrich_target_table(target_id, nm, patch)
    return items


def _apply_ai_contract(kind: str, rt: dict | None) -> dict | None:
    """接口分析 Agent：运行契约解读写 runtime.note.ai。失败抛 AgentError。"""
    if not rt:
        return rt
    note = interface_analyzer.interpret_contract(kind, rt)
    rt.setdefault("note", {})["ai"] = note
    return rt


def _apply_fhir_target_runtime(target_id: str, entities: list[dict]) -> dict:
    """（FHIR 目标）实体语义/direction（接口分析 Agent）+ 运行契约探查与 AI 解读。

    与 SOAP/DB 目标同口径：① 实体级 ai_semantics/direction 由 LLM 产出并写回；
    ② 目标级运行契约（metadata 能力 / 投递语义 / 健康）由确定性探查刷新，note.ai 由 LLM 解读。
    AI 失败抛 AgentError（调用方转 500，不静默留空）。
    """
    modeled = [e for e in entities if not e.get("open")]
    if modeled:
        _apply_target_ai(target_id, modeled, "FHIR",
                         {"协议": "FHIR R4 REST", "写入": "PUT/POST transaction Bundle",
                          "说明": "目标资源由平台 HTTP BO 以事务 Bundle 投递（write）"})
    rt = connection_profiler.profile_target(repository.get_target(target_id))
    _apply_ai_contract("target", rt)
    repository.update_target(target_id, {"runtime": rt})
    return rt


def _discover_fhir_entities(base: str, user: str, pwd: str,
                            requested: list[str] | None) -> tuple[list[dict], dict]:
    """FHIR 目标候选实体发现：服务器 CapabilityStatement 支持全集 + 平台 US Core 字段模型。

    候选资源类型由服务器能力驱动（不再写死四类/十一类）：
    - requested 为 None 时默认取服务器 metadata 支持的全部资源类型（能力全集）；
    - 平台已建模的类型（US_CORE_RESOURCE_MODELS）带 profile + 字段结构，
      未建模但服务器支持的类型生成「开放候选」（open=True，无列结构）；
    - 开放候选的映射字段由 Agent（LLM）在 AI 智能匹配时依 FHIR R4/US Core 规范自定；
    - 服务器不支持的类型自动剔除；显式指定但全不支持时诚实报错。

    返回 (entities, runtime)。参数错误 / 能力不支持时抛 ValueError（调用方转 4xx）。
    """
    runtime: dict = {}
    cap = None
    try:
        # 回环地址归一：用户从浏览器视角登记的是 http://localhost:52773/...，
        # backend 在独立容器里连不上 → metadata 探测必然失败并退化为"已建模 11 类"候选。
        # 仅**探测**用内部地址；对外保存的 base_url 仍是用户登记值。
        probe_base = to_internal_url(base)
        cap = fhir_client.get_capability_statement(probe_base + "/", user, pwd)
        runtime["health"] = {"ok": True, "detail": f"FHIR metadata 可达（{probe_base}）",
                             "checked_at": datetime.datetime.now().isoformat()}
    except Exception as exc:  # noqa: BLE001 - 探活失败不阻断登记
        logger.warning("FHIR 目标探活失败: %s", exc)
        runtime["health"] = {"ok": False, "detail": str(exc)[:200],
                             "checked_at": datetime.datetime.now().isoformat()}
    if cap is not None:
        try:
            analysis = profile_analyzer.analyze_capability(cap)
            runtime["capabilities"] = {
                "fhir_version": analysis.get("fhir_version", ""),
                "resource_count": analysis.get("resource_count", 0),
                "resource_types": analysis.get("resource_types", []),
                "supported_profiles": analysis.get("supported_profiles", []),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("FHIR CapabilityStatement 分析失败: %s", exc)

    supported_list = list((runtime.get("capabilities") or {}).get("resource_types") or [])
    supported = set(supported_list)
    if requested is not None:
        if not isinstance(requested, list) or not requested:
            raise ValueError("FHIR 目标 resource_types 必须是资源类型数组")
        candidates = list(requested)
    elif supported_list:
        # 默认候选 = 服务器 CapabilityStatement 支持的全部资源类型（能力全集，不写死）：
        # 已建模类型带字段结构，未建模类型按开放候选交由 Agent 依 FHIR 规范自定字段。
        candidates = list(supported_list)
    else:
        # metadata 不可达（探活失败但登记放行）：退回已建模全集并明确 basis 标注
        candidates = list(fhir_target_model.DEFAULT_RESOURCE_TYPES)
    if supported:
        candidates = [rt for rt in candidates if rt in supported]
        if requested and not candidates:
            raise ValueError(f"请求的资源类型 {requested} 均未被服务器支持")
        if not candidates:
            # 能力可达但候选全为空：诚实失败，不静默降级
            raise ValueError("服务器 CapabilityStatement 未声明可映射的资源类型")
    entities = fhir_target_model.build_entities(candidates)
    if not entities:
        raise ValueError("未生成任何 FHIR 目标资源实体（US Core 模型缺失）")
    modeled_n = sum(1 for e in entities if not e.get("open"))
    runtime["candidates"] = {
        "count": len(entities),
        "modeled_count": modeled_n,
        "open_count": len(entities) - modeled_n,
        "resource_types": [e["table"] for e in entities],
        "basis": "capability_full" if (supported and requested is None)
                 else ("capability_filtered" if supported else "model_default"),
    }
    return entities, runtime

# 允许的目标类型（DB: JDBC 表；SOAP: WSDL 导入型 BO；FHIR: US Core 声明式写入内置 FHIR BO）
TARGET_TYPES = ["DB", "SOAP", "FHIR"]


def _target_table_rows() -> list[dict]:
    """汇总所有数据目标下已选定的目标表（扁平化，供 AI 推荐/管道投放）。

    - DB 目标：每个选定表一个条目（含列结构）；
    - SOAP 目标：每个分析出的 WSDL 实体一个条目（含实体名、属性结构列）。
    """
    items = []
    for tg in repository.list_targets():
        if tg.get("type") == "SOAP":
            conn = tg.get("connection") or {}
            service = conn.get("service") or "default"
            entities = tg.get("tables") or conn.get("entities") or []
            if not entities:
                entities = [{
                    "entity_name": service,
                    "table": service,
                    "schema": "SOAP",
                    "columns": [],
                    "fields": []
                }]
            for ent in entities:
                cols = ent.get("columns", [])
                col_names = [c["name"] if isinstance(c, dict) else c for c in cols]
                items.append({
                    "target_id": tg.get("id"),
                    "target_name": tg.get("name"),
                    "table": ent.get("entity_name") or ent.get("table") or service,
                    "schema": "SOAP",
                    "columns": col_names,
                    "fields": ent.get("fields") or col_names,
                    "type": "SOAP",
                    "bo_class": conn.get("bo_class", ""),
                    "enabled": True,
                    "count": 0,
                    "status": tg.get("status", "analyzed"),
                    # 接口分析 Agent（AI）语义产物
                    "ai_semantics": ent.get("ai_semantics") or "",
                    "direction": ent.get("direction") or "",
                    "ai_reason": ent.get("ai_reason") or "",
                })
            continue
        if tg.get("type") == "FHIR":
            # FHIR 目标：目标实体 = US Core 资源（无 SQL count）
            for ent in tg.get("tables", []) or []:
                cols = ent.get("columns", [])
                col_names = [c["name"] if isinstance(c, dict) else c for c in cols]
                items.append({
                    "target_id": tg.get("id"),
                    "target_name": tg.get("name"),
                    "table": ent.get("entity_name") or ent.get("table") or "FHIR",
                    "schema": "FHIR",
                    "profile": ent.get("profile") or "",
                    "columns": col_names,
                    "fields": ent.get("fields") or col_names,
                    "type": "FHIR",
                    "enabled": True,
                    "count": 0,
                    "status": tg.get("status", "analyzed"),
                    # 开放候选标记：平台未建模但服务器支持的资源类型（字段由 LLM 依 FHIR 规范自定）
                    "open": bool(ent.get("open")),
                    "modeled": bool(ent.get("modeled", not ent.get("open"))),
                    "ai_semantics": ent.get("ai_semantics") or "",
                    "direction": ent.get("direction") or "",
                    "ai_reason": ent.get("ai_reason") or "",
                })
            continue
        for tb in tg.get("tables", []):
            count = 0
            try:
                count = iris_connector.query(
                    f"SELECT COUNT(*) FROM {tb['table']}")[0][0]
            except Exception:  # noqa: BLE001 - 行数查询失败不影响目标可用
                count = 0
            items.append({
                "target_id": tg.get("id"),
                "target_name": tg.get("name"),
                "table": tb["table"],
                "schema": tb.get("schema", ""),
                "columns": [c["name"] for c in tb.get("columns", [])],
                "type": tg.get("type", "DB"),
                "enabled": True,
                "count": count,
                "status": tg.get("status", "analyzed"),
                # 接口分析 Agent（AI）语义产物
                "ai_semantics": tb.get("ai_semantics") or "",
                "direction": tb.get("direction") or "",
                "ai_reason": tb.get("ai_reason") or "",
            })
    return items


@targets_bp.get("/fhir-constraints")
def fhir_constraints():
    """已建模 FHIR 资源的结构约束（选型/映射上下文与前端展示共用）。

    query: resource=Patient（可省略；省略则返回全部已建模资源的约束）
    """
    rt = (request.args.get("resource") or "").strip()
    if rt:
        cons = fhir_target_model.column_constraints(rt)
        if not cons:
            return error(f"未建模资源（无列结构约束）: {rt}"), 404
        return success({"resource": rt, "constraints": cons})
    return success({"resources": {k: fhir_target_model.column_constraints(k)
                                  for k in fhir_target_model.DEFAULT_RESOURCE_TYPES}})


@targets_bp.get("")
def list_targets():
    """目标表列表（扁平化：所有数据目标下已选定的表），供 AI 匹配与管道投放。"""
    return success({"items": _target_table_rows()})


@targets_bp.get("/manage")
def list_target_managers():
    """数据目标列表（管理视图：目标 + 连接状态 + 已选表数量 + 精简运行契约）。"""
    items = []
    for tg in repository.list_targets():
        rt = tg.get("runtime") or {}
        # runtime 精简展示（健康/候选/能力/delivery），不回传 connection（含密码）与 assets 明细
        caps = dict(rt.get("capabilities") or {})
        caps.pop("resource_types", None)
        display_rt = {
            "health": rt.get("health") or {},
            "candidates": rt.get("candidates") or {},
            "delivery": rt.get("delivery") or {},
            "capabilities": caps,
        }
        items.append({
            "id": tg.get("id"),
            "name": tg.get("name"),
            "type": tg.get("type"),
            "status": tg.get("status"),
            "tables": tg.get("tables", []),
            "created_at": tg.get("created_at"),
            # 连接信息脱敏（不回传密码）
            "connection": {k: v for k, v in tg.get("connection", {}).items()
                           if k != "password"},
            "runtime": display_rt,
        })
    return success({"items": items})


@targets_bp.post("")
def create_target():
    """添加数据目标。

    - DB: 请求体 {"name", "type":"DB", "connection":{"jdbc_url", "driver_class", "username", "password"}}
    - SOAP: 请求体 {"name", "type":"SOAP", "connection":{"wsdl", "service", "packages"}}
      创建时立即调 %SOAP.WSDL.Reader 导入生成 BO（验证 WSDL 有效）。
    """
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    target_type = body.get("type") or "DB"
    conn = body.get("connection") or {}
    if not name:
        return error("请填写目标名称"), 400
    if target_type not in TARGET_TYPES:
        return error(f"目标类型 {target_type} 尚未实现"), 400

    if target_type == "SOAP":
        if not conn.get("wsdl"):
            return error("SOAP 目标需要提供 WSDL 地址"), 400
        imp = wsdl_importer.import_soap_operation(
            conn.get("wsdl"), conn.get("service") or "default", conn.get("packages"))
        if not imp["ok"]:
            return error(f"WSDL 导入失败: {imp['message']}"), 400
        entities = imp.get("entities") or []
        # SOAP 目标支持显式指定远端地址 endpoint（默认指向 Python mock 演示第三方系统）
        conn = {
            **conn,
            "bo_class": imp["boClass"],
            "entities": entities,
            "endpoint": conn.get("endpoint") or Config.MOCK_SOAP_URL,
        }
        target_id = repository.create_target(name, "SOAP", conn, tables=entities)
        repository.update_target(target_id, {"status": "analyzed"})
        repository.save_target_interface({
            "id": target_id,
            "target_id": target_id,
            "name": name,
            "type": "SOAP",
            "tables": entities,
            "config": {
                "service": conn.get("service") or "default",
                "wsdl": conn.get("wsdl"),
                "bo_class": imp["boClass"],
                "endpoint": conn.get("endpoint") or Config.MOCK_SOAP_URL,
            },
        })
        # —— 接口分析 Agent（LLM）：SOAP 实体语义 + 写读方向判定（失败即显式失败）——
        try:
            _apply_target_ai(target_id, entities, "SOAP",
                             {"service": conn.get("service") or "default",
                              "bo_class": imp["boClass"]})
        except AgentError as exc:
            logger.error("SOAP 实体接口分析 Agent 失败: %s", exc)
            return error(f"SOAP 实体接口分析 Agent（AI）失败: {exc}"), 500
        # 连接探查：操作语义判定（写入/查询）+ endpoint 可达性 → 刷新运行契约
        try:
            rt = connection_profiler.profile_target(repository.get_target(target_id))
            _apply_ai_contract("target", rt)
            repository.update_target(target_id, {"runtime": rt})
        except AgentError as exc:
            logger.error("SOAP 目标契约解读 Agent 失败: %s", exc)
            return error(f"SOAP 目标契约解读 Agent（AI）失败: {exc}"), 500
        except Exception as exc:  # noqa: BLE001
            logger.warning("SOAP 目标探查失败: %s", exc)
        return success({
            "id": target_id, "bo_class": imp["boClass"], "entities": entities,
            "target": repository.get_target(target_id),
        }, "SOAP 目标添加成功（WSDL 已导入生成 BO，AI 实体分析完成）")

    if target_type == "FHIR":
        base = (conn.get("base_url") or "").strip().rstrip("/")
        if not base:
            return error("FHIR 目标需要 base_url（FHIR endpoint 根，如 "
                         "http://dataflow-iris:52773/csp/healthshare/fhirserver/fhir/r4）"), 400
        user = conn.get("username") or "superuser"
        pwd = conn.get("password") or "SYS"
        requested = conn.get("resource_types")
        try:
            entities, runtime = _discover_fhir_entities(base, user, pwd, requested)
        except ValueError as exc:
            return error(str(exc)), 400

        conn = {**conn, "base_url": base, "username": user, "password": pwd,
                "entities": entities, "profile_base": fhir_target_model.US_CORE_BASE}
        target_id = repository.create_target(name, "FHIR", conn, tables=entities)
        repository.update_target(target_id, {"status": "analyzed"})
        repository.save_target_interface({
            "id": target_id, "target_id": target_id, "name": name, "type": "FHIR",
            "tables": entities,
            "config": {"base_url": base, "username": user,
                       "resource_types": [e["table"] for e in entities],
                       "profile_base": fhir_target_model.US_CORE_BASE},
        })
        repository.update_target(target_id, {"runtime": runtime})
        # —— 接口分析 Agent（实体语义/direction）+ 契约探查与解读（AI）：失败即显式失败 ——
        try:
            runtime = _apply_fhir_target_runtime(target_id, entities)
        except AgentError as exc:
            logger.error("FHIR 目标接口分析/契约解读 Agent 失败: %s", exc)
            return error(f"FHIR 目标接口分析 Agent（AI）失败: {exc}"), 500
        except Exception as exc:  # noqa: BLE001 - 契约探查失败不阻断登记（AI 已成功）
            logger.warning("FHIR 目标契约探查失败: %s", exc)
        return success({
            "id": target_id, "entities": entities,
            "resource_types": [e["table"] for e in entities],
            "runtime": runtime,
            "target": repository.get_target(target_id),
        }, f"FHIR 目标添加成功（候选 US Core 资源 {len(entities)} 类，"
            f"由 Agent 在智能匹配时决定具体映射）")

    if not conn.get("jdbc_url"):
        return error("缺少 JDBC 连接信息（jdbc_url）"), 400
    target_id = repository.create_target(name, target_type, conn)
    repository.save_target_interface({
        "id": target_id,
        "target_id": target_id,
        "name": name,
        "type": target_type,
        "connection": conn,
    })
    # DSN 归一（演示默认 DB 目标库 = **CLINIC**）：按 jdbc_url 的命名空间注册 DSN 并写回 runtime，
    # 生成管道时 DB 目标 SQLOp 直接引用它 —— 不再硬编码 localTarget/USER（否则"目标登记 CLINIC、
    # 数据却写进 USER"的静默错位，详见 services/jdbc_dsn.py 模块说明）。
    from backend.services import jdbc_dsn
    dsn_name = jdbc_dsn.register_for_target(repository.get_target(target_id))
    try:
        rt = connection_profiler.profile_target(repository.get_target(target_id))
        repository.update_target(target_id, {"runtime": rt})
    except Exception as exc:  # noqa: BLE001
        logger.warning("DB 目标探查失败: %s", exc)
    logger.info("DB 目标 %s 登记完成（DSN=%s）", target_id, dsn_name)
    return success({"id": target_id, "dsn": dsn_name,
                    "target": repository.get_target(target_id)}, "数据目标添加成功")


@targets_bp.post("/<target_id>/import")
def import_target(target_id: str):
    """（SOAP 目标）重新导入 WSDL，更新 BO 类并重新分析实体与属性。"""
    tg = repository.get_target(target_id)
    if not tg:
        return error("数据目标不存在"), 404
    if tg.get("type") != "SOAP":
        return error("仅 SOAP 目标支持 WSDL 导入"), 400
    conn = tg.get("connection") or {}
    imp = wsdl_importer.import_soap_operation(
        conn.get("wsdl", ""), conn.get("service") or "default", conn.get("packages"))
    if not imp["ok"]:
        return error(f"WSDL 导入失败: {imp['message']}"), 500
    entities = imp.get("entities") or []
    conn = {**conn, "bo_class": imp["boClass"], "entities": entities}
    repository.update_target(target_id, {"connection": conn, "tables": entities, "status": "analyzed"})
    repository.save_target_interface({
        "id": target_id,
        "target_id": target_id,
        "name": tg.get("name"),
        "type": "SOAP",
        "tables": entities,
        "config": {
            "service": conn.get("service") or "default",
            "wsdl": conn.get("wsdl"),
            "bo_class": imp["boClass"],
            "endpoint": conn.get("endpoint") or Config.MOCK_SOAP_URL,
        },
    })
    # —— 接口分析 Agent（LLM）：重新导入后刷新实体语义/写读方向 ——
    try:
        _apply_target_ai(target_id, entities, "SOAP",
                         {"service": conn.get("service") or "default",
                          "bo_class": imp["boClass"]})
    except AgentError as exc:
        logger.error("SOAP 实体接口分析 Agent 失败: %s", exc)
        return error(f"SOAP 实体接口分析 Agent（AI）失败: {exc}"), 500
    try:
        rt = connection_profiler.profile_target(repository.get_target(target_id))
        _apply_ai_contract("target", rt)
        repository.update_target(target_id, {"runtime": rt})
    except AgentError as exc:
        logger.error("SOAP 目标契约解读 Agent 失败: %s", exc)
        return error(f"SOAP 目标契约解读 Agent（AI）失败: {exc}"), 500
    except Exception as exc:  # noqa: BLE001
        logger.warning("SOAP 目标探查失败: %s", exc)
    return success({"bo_class": imp["boClass"], "entities": entities},
                   "WSDL 重新导入与实体分析成功（AI 语义已刷新）")



@targets_bp.post("/<target_id>/refresh-resources")
def refresh_fhir_resources(target_id: str):
    """（FHIR 目标）刷新候选资源类型：按最新 US Core 模型 ∩ 服务器能力重新发现。

    服务器新增资源类型 / 平台扩充模型后无需删除重建——刷新候选集即可，
    具体「源资产 → 哪类 FHIR 资源」仍由 Agent（LLM）在智能匹配时决定。
    """
    tg = repository.get_target(target_id)
    if not tg:
        return error("数据目标不存在"), 404
    if tg.get("type") != "FHIR":
        return error("仅 FHIR 目标支持刷新资源类型"), 400
    conn = tg.get("connection") or {}
    base = (conn.get("base_url") or "").strip().rstrip("/")
    if not base:
        return error("FHIR 目标缺少 base_url"), 400
    user = conn.get("username") or "superuser"
    pwd = conn.get("password") or "SYS"
    try:
        entities, runtime = _discover_fhir_entities(base, user, pwd, None)
    except ValueError as exc:
        return error(str(exc)), 400
    conn = {**conn, "entities": entities, "profile_base": fhir_target_model.US_CORE_BASE}
    repository.update_target(target_id, {"connection": conn, "tables": entities,
                                         "status": "analyzed", "runtime": runtime})
    repository.save_target_interface({
        "id": target_id, "target_id": target_id, "name": tg.get("name"), "type": "FHIR",
        "tables": entities,
        "config": {"base_url": base, "username": user,
                   "resource_types": [e["table"] for e in entities],
                   "profile_base": fhir_target_model.US_CORE_BASE},
    })
    # 刷新时同步刷新实体语义/direction 与运行契约（AI 失败即显式失败）
    try:
        runtime = _apply_fhir_target_runtime(target_id, entities)
    except AgentError as exc:
        logger.error("FHIR 目标接口分析/契约解读 Agent 失败: %s", exc)
        return error(f"FHIR 目标接口分析 Agent（AI）失败: {exc}"), 500
    except Exception as exc:  # noqa: BLE001 - 探查失败不阻断刷新（AI 已成功），但保留已发现的 runtime
        logger.warning("FHIR 目标契约探查失败: %s", exc)
    return success({"id": target_id, "entities": entities,
                    "resource_types": [e["table"] for e in entities],
                    "runtime": runtime,
                    "target": repository.get_target(target_id)},
                   f"FHIR 目标候选资源已刷新（{len(entities)} 类，最新 US Core 模型 ∩ 服务器能力）")


@targets_bp.post("/<target_id>/test")
def test_target(target_id: str):
    """联通测试（按目标类型分流）。

    - DB：JDBC 连接 + `SELECT 1`（jdbc_client.test_connection）
    - SOAP / FHIR：走 `connection_profiler.profile_target` 的运行契约探查
      （SOAP = endpoint HTTP 可达性；FHIR = metadata 能力 + 投递语义）

    实测缺陷（N5）：原先所有目标都走 JDBC 分支，对 FHIR/SOAP 目标必然返回
    「连接失败: 缺少 jdbc_url」并把目标状态写成 `error`，把一个健康目标标红。
    """
    tg = repository.get_target(target_id)
    if not tg:
        return error("数据目标不存在"), 404
    ttype = (tg.get("type") or "DB").upper()
    if ttype in ("SOAP", "FHIR"):
        try:
            rt = connection_profiler.profile_target(tg)
        except Exception as exc:  # noqa: BLE001 - 探查异常按连接失败返回
            logger.error("目标运行契约探查失败 %s: %s", target_id, exc)
            repository.update_target(target_id, {"status": "error"})
            return error(f"连接失败: {exc}"), 500
        health = rt.get("health") or {}
        ok = bool(health.get("ok"))
        repository.update_target(target_id, {"status": "connected" if ok else "error"})
        if not ok:
            return error(f"连接失败: {health.get('detail') or '端点不可达'}"), 500
        return success({"ok": True, "type": ttype, "health": health,
                        "delivery": rt.get("delivery") or {},
                        "capabilities": rt.get("capabilities") or {},
                        "candidates": rt.get("candidates") or {}},
                       f"{ttype} 目标连通（{health.get('detail') or 'OK'}）")
    result = jdbc_client.test_connection(tg.get("connection") or {})
    if result["ok"]:
        # 连通即顺带把 DSN 归一/注册（与数据源侧「连通测试即注册 DSN」同一时机），
        # 保证生成 SQLOp 时引用的是**目标命名空间**对应的 DSN（如 CLINIC），而不是 localTarget/USER
        from backend.services import jdbc_dsn
        result["dsn"] = jdbc_dsn.register_for_target(repository.get_target(target_id))
    repository.update_target(target_id, {"status": "connected" if result["ok"] else "error"})
    if not result["ok"]:
        return error(f"连接失败: {result['message']}"), 500
    return success(result, f"连接成功（DSN={result.get('dsn') or 'localTarget'}）")


@targets_bp.get("/<target_id>/schemas")
def list_schemas(target_id: str):
    """列出该数据库的 schema（JDBC）。"""
    tg = repository.get_target(target_id)
    if not tg:
        return error("数据目标不存在"), 404
    try:
        schemas = jdbc_client.list_schemas(tg.get("connection") or {})
    except Exception as exc:  # noqa: BLE001
        logger.error("列出 schema 失败: %s", exc)
        return error(f"列出 schema 失败: {exc}"), 500
    return success({"items": schemas})


@targets_bp.get("/<target_id>/tables")
def list_tables(target_id: str):
    """列出指定 schema 的表（JDBC）。

    查询参数: schema（必填，schema 名）。
    """
    tg = repository.get_target(target_id)
    if not tg:
        return error("数据目标不存在"), 404
    schema = request.args.get("schema", "")
    if not schema:
        return error("缺少 schema 参数"), 400
    try:
        tables = jdbc_client.list_tables(tg.get("connection") or {}, schema)
    except Exception as exc:  # noqa: BLE001
        logger.error("列出表失败: %s", exc)
        return error(f"列出表失败: {exc}"), 500
    return success({"schema": schema, "items": tables})


@targets_bp.post("/<target_id>/tables")
def select_tables(target_id: str):
    """保存用户勾选的目标表并分析列结构（JDBC）。

    请求体: {"tables": [{"schema": "SQLUser", "table": "Patient"}, ...]}
    """
    tg = repository.get_target(target_id)
    if not tg:
        return error("数据目标不存在"), 404
    body = request.get_json(silent=True) or {}
    selected = body.get("tables") or []
    if not selected:
        return error("请选择目标表"), 400

    conn = tg.get("connection") or {}
    saved = []
    for item in selected:
        schema = item.get("schema", "")
        table = item.get("table", "")
        if not table:
            continue
        try:
            columns = jdbc_client.list_columns(conn, schema, table)
        except Exception as exc:  # noqa: BLE001
            logger.error("分析列失败 %s.%s: %s", schema, table, exc)
            return error(f"分析表 {table} 列失败: {exc}"), 500
        # 主键列事实（JDBC 元数据）：IRIS 的 INSERT/UPSERT 要求主键非空，
        # 该事实供 Agent A（生成映射）与 C1（验证修复）使用；探查失败不阻断登记。
        try:
            key_columns = jdbc_client.list_primary_keys(conn, schema, table)
        except Exception as exc:  # noqa: BLE001
            logger.warning("分析主键失败（%s.%s，按未知处理）: %s", schema, table, exc)
            key_columns = []
        repository.add_target_table(target_id, schema, table, columns,
                                    key_columns=key_columns)
        saved.append({"schema": schema, "table": table, "columns": columns,
                      "key_columns": key_columns})

    repository.update_target(target_id, {"status": "analyzed"})
    # —— 接口分析 Agent（LLM）：DB 目标表语义 + 写读方向判定（失败即显式失败）——
    try:
        _apply_target_ai(target_id, saved, "DB", {"kind": "database table target"})
    except AgentError as exc:
        logger.error("DB 目标表接口分析 Agent 失败: %s", exc)
        return error(f"DB 目标表接口分析 Agent（AI）失败: {exc}"), 500
    try:
        rt = connection_profiler.profile_target(repository.get_target(target_id))
        _apply_ai_contract("target", rt)
        repository.update_target(target_id, {"runtime": rt})
    except AgentError as exc:
        logger.error("DB 目标契约解读 Agent 失败: %s", exc)
        return error(f"DB 目标契约解读 Agent（AI）失败: {exc}"), 500
    except Exception as exc:  # noqa: BLE001
        logger.warning("DB 目标探查失败: %s", exc)
    return success({"saved": saved, "count": len(saved)},
                   "目标表已保存并完成列分析（AI 语义分析完成）")


@targets_bp.get("/<target_id>/selected")
def list_selected_tables(target_id: str):
    """已选定的目标表（含列结构）。"""
    tg = repository.get_target(target_id)
    if not tg:
        return error("数据目标不存在"), 404
    return success({"items": tg.get("tables", [])})


@targets_bp.get("/<table>/data")
def table_data(table: str):
    """目标表数据（示例中目标表位于 IRIS USER namespace，即模拟远端库）。

    查询参数: limit（默认 50）。
    """
    # 仅允许已登记的目标表（动态，由演示过程添加的目标决定）
    allowed = {tb["table"] for tb in _target_table_rows()}
    if table not in allowed:
        return error(f"目标表不存在或未注册: {table}"), 404
    limit = request.args.get("limit", 50, type=int)
    limit = min(max(limit, 1), 200)
    rows = iris_connector.query(f"SELECT * FROM {table} LIMIT {limit}")
    cols = [r[0] for r in iris_connector.query(
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_schema='SQLUser' AND table_name='{table}' ORDER BY ordinal_position")]
    items = [dict(zip(cols, row)) for row in rows]
    return success({"items": items, "count": len(items)})
