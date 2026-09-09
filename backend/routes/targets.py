"""转换目标（数据目标）API。

分步流程（JDBC 通用连接，不依赖 Native SDK）：
  ① 添加数据目标（JDBC 连接信息）→ ② 联通测试
  → ③ 选择 schema → ④ 列出表 → ⑤ 勾选目标表 → ⑥ 分析列结构

返回给 AI 推荐 / 管道投放的目标列表（GET /api/targets）为扁平化目标表列表。
"""

import logging

from flask import Blueprint, request

from backend.config import Config
from backend.services import (connection_profiler, fhir_client, fhir_target_model,
                              interface_analyzer, iris_connector,
                              jdbc_client, repository, wsdl_importer)
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


@targets_bp.get("")
def list_targets():
    """目标表列表（扁平化：所有数据目标下已选定的表），供 AI 匹配与管道投放。"""
    return success({"items": _target_table_rows()})


@targets_bp.get("/manage")
def list_target_managers():
    """数据目标列表（管理视图：目标 + 连接状态 + 已选表数量）。"""
    items = []
    for tg in repository.list_targets():
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
        res_types = conn.get("resource_types") or fhir_target_model.DEFAULT_RESOURCE_TYPES
        entities = fhir_target_model.build_entities(res_types)
        conn = {**conn, "base_url": base, "username": user, "password": pwd,
                "entities": entities, "profile_base": fhir_target_model.US_CORE_BASE}
        target_id = repository.create_target(name, "FHIR", conn, tables=entities)
        repository.update_target(target_id, {"status": "analyzed"})
        repository.save_target_interface({
            "id": target_id, "target_id": target_id, "name": name, "type": "FHIR",
            "tables": entities,
            "config": {"base_url": base, "username": user,
                       "resource_types": list(res_types),
                       "profile_base": fhir_target_model.US_CORE_BASE},
        })
        # 可达性探活：拉取 CapabilityStatement（只读，不校验 profile）
        runtime = {}
        try:
            fhir_client.get_capability_statement(base + "/", user, pwd)
            runtime["health"] = "ok"
        except Exception as exc:  # noqa: BLE001 - 探活失败不阻断登记
            logger.warning("FHIR 目标探活失败: %s", exc)
            runtime["health"] = "unreachable"
        repository.update_target(target_id, {"runtime": runtime})
        return success({"id": target_id, "entities": entities,
                        "target": repository.get_target(target_id)},
                       "FHIR 目标添加成功（US Core 目标资源模型已生成）")

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
    try:
        rt = connection_profiler.profile_target(repository.get_target(target_id))
        repository.update_target(target_id, {"runtime": rt})
    except Exception as exc:  # noqa: BLE001
        logger.warning("DB 目标探查失败: %s", exc)
    return success({"id": target_id, "target": repository.get_target(target_id)}, "数据目标添加成功")


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



@targets_bp.post("/<target_id>/test")
def test_target(target_id: str):
    """联通测试（JDBC 连接 + SELECT 1）。"""
    tg = repository.get_target(target_id)
    if not tg:
        return error("数据目标不存在"), 404
    result = jdbc_client.test_connection(tg.get("connection") or {})
    repository.update_target(target_id, {"status": "connected" if result["ok"] else "error"})
    if not result["ok"]:
        return error(f"连接失败: {result['message']}"), 500
    return success(result, "连接成功")


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
        repository.add_target_table(target_id, schema, table, columns)
        saved.append({"schema": schema, "table": table, "columns": columns})

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
