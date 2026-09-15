"""数据源管理 API。

流程：注册数据源 → 自动分析 Profile（解析 CapabilityStatement）→ 发现数据资产（FHIR 资源类型）。
"""

import logging

from flask import Blueprint, request

from backend.schemas.models import DataSourceIn
from backend.services import (clinic_seed, connection_profiler, fhir_client,
                              interface_analyzer, jdbc_client, profile_analyzer, repository)
from backend.services.llm_client import AgentError
from backend.utils import error, success

logger = logging.getLogger(__name__)

datasources_bp = Blueprint("datasources", __name__, url_prefix="/api/datasources")


def _apply_ai_semantics(assets: list[dict], source_type: str,
                        facts: dict | None = None) -> list[dict]:
    """接口分析 Agent：资产语义/键建议（LLM）并写回资产。失败抛 AgentError（显式失败）。"""
    items = interface_analyzer.analyze_source_assets(source_type, assets, facts)
    by_name = {}
    for it in items:
        if it.get("name"):
            by_name[str(it["name"]).lower()] = it
    for a in assets:
        it = by_name.get(str(a.get("name", "")).lower())
        if not it:
            continue
        patch = {
            "ai_analysis": True,
            "ai_semantics": it.get("semantics", ""),
            "description": it.get("semantics") or a.get("description", ""),
        }
        if it.get("key_hint"):
            patch["key_hint"] = it["key_hint"]
        if it.get("comment"):
            patch["ai_comment"] = it["comment"]
        if it.get("field_terms") and isinstance(it["field_terms"], dict):
            patch["field_terms"] = it["field_terms"]
        repository.update_asset(a["id"], patch)
    return items


def _apply_ai_contract(kind: str, rt: dict | None) -> dict | None:
    """接口分析 Agent：运行契约解读写 runtime.note.ai。失败抛 AgentError。"""
    if not rt:
        return rt
    note = interface_analyzer.interpret_contract(kind, rt)
    rt.setdefault("note", {})["ai"] = note
    return rt


def _ai_semantics_summary(assets: list[dict]) -> list[dict]:
    """汇总已写回资产的 AI 语义（返回给前端展示/审计）。"""
    out = []
    for a in assets:
        sem = a.get("ai_semantics") or ""
        if not sem:
            continue
        out.append({
            "name": a.get("name"),
            "semantics": sem,
            "key_hint": a.get("key_hint") or "",
            "ai_analysis": True,
        })
    return out


@datasources_bp.post("")
def create_datasource():
    """注册数据源。

    请求体: {"name": "...", "type": "FHIR", "endpoint": "http://.../fhir/r4/", "auth": {...}}
          或 {"name": "...", "type": "SQL", "config": {"jdbc_url": "...", "driver_class": "...",
                "username": "...", "password": "..."}}
    """
    body = request.get_json(silent=True) or {}
    try:
        ds = DataSourceIn(**body)
    except Exception as exc:  # noqa: BLE001 - Pydantic 校验错误统一返回
        return error(f"参数校验失败: {exc}"), 400
    if ds.type == "SQL":
        cfg = ds.config or {}
        if not cfg.get("jdbc_url"):
            return error("SQL 数据源需要 JDBC 连接信息（jdbc_url）"), 400
    ds_id = repository.create_datasource(ds)
    return success({"id": ds_id, "datasource": repository.get_datasource(ds_id)}, "数据源注册成功")


@datasources_bp.get("")
def list_datasources():
    """数据源列表。"""
    return success({"items": repository.list_datasources()})


@datasources_bp.get("/<ds_id>")
def get_datasource(ds_id: str):
    """数据源详情。"""
    ds = repository.get_datasource(ds_id)
    if not ds:
        return error("数据源不存在"), 404
    return success(ds)


@datasources_bp.post("/<ds_id>/analyze")
def analyze_datasource(ds_id: str):
    """自动分析数据源：拉取 CapabilityStatement，识别 Profile 与资源类型，保存数据资产。

    返回: 分析结果（fhir_version / resource_types / supported_profiles 等）+ 资产列表。
    """
    ds = repository.get_datasource(ds_id)
    if not ds:
        return error("数据源不存在"), 404
    if ds.get("type") == "SQL":
        # SQL 源：无需 CapabilityStatement；校验已选数据表后标记就绪
        tables = ds.get("tables") or []
        if not tables:
            return error("请先选择数据表（联通测试 → 选择 schema → 选择表）"), 400
        assets = repository.list_assets(ds_id)
        repository.update_datasource(ds_id, {"status": "analyzed"})
        # —— 接口分析 Agent：SQL 表资产语义 + 运行契约解读（LLM，失败即显式失败）——
        try:
            if assets:
                _apply_ai_semantics(assets, "SQL",
                                    {"tables": tables, "source": "SQL database"})
            rt = connection_profiler.profile_source(repository.get_datasource(ds_id))
            _apply_ai_contract("source", rt)
            repository.update_datasource(ds_id, {"runtime": rt})
        except AgentError as exc:
            logger.error("SQL 源接口分析 Agent 失败: %s", exc)
            return error(f"SQL 源接口分析 Agent（AI）失败: {exc}"), 500
        return success({
            "analysis": {"source_type": "SQL",
                         "tables": [f"{t.get('schema', '')}.{t['table']}" for t in tables]},
            "assets": assets, "asset_count": len(assets),
            "ai": _ai_semantics_summary(assets),
        }, "SQL 数据源已就绪（AI 语义分析完成）")
    if ds.get("type") != "FHIR":
        return error(f"数据源类型 {ds.get('type')} 尚未实现"), 400

    try:
        endpoint = ds["endpoint"]
        auth = ds.get("auth") or {}
        cap = fhir_client.get_capability_statement(
            endpoint, auth.get("username", "superuser"), auth.get("password", "SYS"))
        analysis = profile_analyzer.analyze_capability(cap)
        assets = repository.save_assets(ds_id, analysis["resource_types"])
        for asset in assets:
            repository.save_source_asset({
                **asset,
                "fields": asset.get("fields", []),
                "description": f"FHIR 资产 {asset.get('name', '')}",
            })
        repository.update_datasource(ds_id, {"status": "analyzed", "profile": analysis})
    except Exception as exc:  # noqa: BLE001 - 需返回明确错误给前端
        logger.error("数据源分析失败: %s", exc)
        return error(f"Profile 分析失败: {exc}"), 500

    # —— 接口分析 Agent（LLM）：资产语义 + 运行契约解读（失败即显式失败，不静默规则）——
    try:
        if assets:
            _apply_ai_semantics(assets, "FHIR",
                                {"fhir_version": analysis.get("fhir_version", ""),
                                 "capability": analysis})
        rt = connection_profiler.profile_source(repository.get_datasource(ds_id))
        _apply_ai_contract("source", rt)
        repository.update_datasource(ds_id, {"runtime": rt})
    except AgentError as exc:
        logger.error("FHIR 源接口分析 Agent 失败: %s", exc)
        return error(f"接口分析 Agent（AI）语义/契约分析失败: {exc}"), 500
    except Exception as exc:  # noqa: BLE001 - 确定性探查失败可继续（语义已完成）
        logger.warning("FHIR 连接探查失败: %s", exc)

    return success({
        "analysis": analysis, "assets": assets, "asset_count": len(assets),
        "ai": _ai_semantics_summary(repository.list_assets(ds_id)),
    }, "Profile 分析完成（AI 语义分析已生成）")


@datasources_bp.post("/<ds_id>/test")
def test_datasource(ds_id: str):
    """（SQL 源）JDBC 联通测试。"""
    ds = repository.get_datasource(ds_id)
    if not ds:
        return error("数据源不存在"), 404
    if ds.get("type") != "SQL":
        return error("仅 SQL 数据源支持联通测试"), 400
    result = jdbc_client.test_connection(ds.get("config") or {})
    if not result["ok"]:
        return error(f"连接失败: {result['message']}"), 500
    repository.update_datasource(ds_id, {"status": "connected"})
    # 连接已验证 → 自动注册 DSN 并写回数据源（后续生成管道直接引用，无需人工建 DSN）
    from backend.services import jdbc_dsn
    result["dsn"] = jdbc_dsn.register_for_datasource(repository.get_datasource(ds_id))
    return success(result, "连接成功（已自动注册 DSN）")


@datasources_bp.get("/<ds_id>/schemas")
def list_source_schemas(ds_id: str):
    """（SQL 源）列出数据库 schema。"""
    ds = repository.get_datasource(ds_id)
    if not ds:
        return error("数据源不存在"), 404
    try:
        schemas = jdbc_client.list_schemas(ds.get("config") or {})
    except Exception as exc:  # noqa: BLE001
        return error(f"列出 schema 失败: {exc}"), 500
    return success({"items": schemas})


@datasources_bp.get("/<ds_id>/tables")
def list_source_tables(ds_id: str):
    """（SQL 源）列出指定 schema 的表。"""
    ds = repository.get_datasource(ds_id)
    if not ds:
        return error("数据源不存在"), 404
    schema = request.args.get("schema", "")
    if not schema:
        return error("缺少 schema 参数"), 400
    try:
        tables = jdbc_client.list_tables(ds.get("config") or {}, schema)
    except Exception as exc:  # noqa: BLE001
        return error(f"列出表失败: {exc}"), 500
    return success({"schema": schema, "items": tables})


@datasources_bp.post("/<ds_id>/tables")
def select_source_tables(ds_id: str):
    """（SQL 源）勾选数据表 → 分析列结构 → 存资产 + 自动生成轮询 Query。"""
    ds = repository.get_datasource(ds_id)
    if not ds:
        return error("数据源不存在"), 404
    body = request.get_json(silent=True) or {}
    selected = body.get("tables") or []
    if not selected:
        return error("请选择数据表"), 400

    conn = ds.get("config") or {}
    saved = []
    for item in selected:
        schema = item.get("schema", "")
        table = item.get("table", "")
        if not table:
            continue
        try:
            columns = jdbc_client.list_columns(conn, schema, table)
        except Exception as exc:  # noqa: BLE001
            return error(f"分析表 {table} 列失败: {exc}"), 500
        saved.append({"schema": schema, "table": table, "columns": columns})

    if not saved:
        return error("未选择有效数据表"), 400
    assets = repository.save_sql_table_assets(ds_id, saved)
    for asset in assets:
        repository.save_source_asset({
            **asset,
            "fields": asset.get("fields", []),
            "description": f"SQL 资产 {asset.get('name', '')}",
        })
    # —— 接口分析 Agent：表/列语义 + 轮询键建议（LLM；失败即显式失败，不静默规则）——
    ai_items: list[dict] = []
    try:
        ai_items = _apply_ai_semantics(assets, "SQL",
                                       {"tables": saved, "source": "SQL database"})
    except AgentError as exc:
        logger.error("SQL 表资产接口分析 Agent 失败: %s", exc)
        return error(f"SQL 表资产接口分析 Agent（AI）失败: {exc}"), 500

    # 自动生成轮询 Query（EnsLib.SQL.Service.GenericService 的 Host 设置）
    first = saved[0]
    qtable = first["table"]
    query = f"SELECT * FROM {first['schema']}.{qtable}" if first["schema"] else f"SELECT * FROM {qtable}"
    key_field = (first["columns"][0]["name"] if first["columns"] else "")
    # 轮询键采纳 AI 建议（仅当建议列真实存在于轮询表时替换启发式首列）
    ai_cols = {str(c["name"]).lower() for c in (first.get("columns") or [])}
    for it in ai_items:
        if str(it.get("name", "")).lower() != str(qtable).lower():
            continue
        hint = str(it.get("key_hint") or "").strip()
        if hint and hint.lower() in ai_cols:
            key_field = hint
            logger.info("轮询键采用接口分析 Agent 建议: %s.%s = %s", first["schema"], qtable, hint)
        break
    repository.update_datasource(ds_id, {
        "status": "analyzed",
        "tables": saved,
        "config": {**conn, "query": query, "key_field": key_field},
    })
    # 连接探查 + 契约解读（刷新运行契约）
    try:
        rt = connection_profiler.profile_source(repository.get_datasource(ds_id))
        _apply_ai_contract("source", rt)
        repository.update_datasource(ds_id, {"runtime": rt})
    except AgentError as exc:
        logger.error("SQL 源契约解读 Agent 失败: %s", exc)
        return error(f"SQL 源契约解读 Agent（AI）失败: {exc}"), 500
    except Exception as exc:  # noqa: BLE001
        logger.warning("SQL 连接探查失败: %s", exc)
    # 表已纳入数据源 → 自动注册/复用 DSN 并写回（生成管道时自动引用）
    from backend.services import jdbc_dsn
    dsn_name = jdbc_dsn.register_for_datasource(repository.get_datasource(ds_id))
    return success({
        "saved": saved, "count": len(saved),
        "ai": _ai_semantics_summary(repository.list_assets(ds_id)),
        "dsn": dsn_name,
    }, "数据表已保存（AI 语义分析、轮询键建议与 DSN 注册完成）")


@datasources_bp.post("/<ds_id>/seed")
def seed_datasource(ds_id: str):
    """为 SQL 演示源（CLINIC）生成 n 位患者模拟数据（诊断/药嘱取自中文术语集）。

    幂等：先清四表再写入；返回各表条数。术语连接与 CLINIC 库由 clinic_seed 服务按
    Docker 网络服务名配置（iris-terminology / iris）。
    """
    ds = repository.get_datasource(ds_id)
    if not ds:
        return error("数据源不存在"), 404
    if ds.get("type") != "SQL":
        return error("仅 SQL 数据源支持生成 CLINIC 演示数据"), 400
    body = request.get_json(silent=True) or {}
    try:
        n = int(body.get("patients", 10) or 10)
    except (TypeError, ValueError):
        n = 10
    try:
        stat = clinic_seed.generate_clinic_seed(n=n)
    except Exception as exc:  # noqa: BLE001 - 生成失败返回明确错误
        logger.error("生成 CLINIC 演示数据失败: %s", exc)
        return error(f"生成失败: {exc}"), 500
    return success(stat, "CLINIC 演示数据生成完成（诊断/药品取自中文术语集）")


@datasources_bp.get("/<ds_id>/assets")
def list_assets(ds_id: str):
    """指定数据源的资产列表。"""
    return success({"items": repository.list_assets(ds_id)})
