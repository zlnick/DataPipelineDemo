"""数据源管理 API。

流程：注册数据源 → 自动分析 Profile（解析 CapabilityStatement）→ 发现数据资产（FHIR 资源类型）。
"""

import logging

from flask import Blueprint, request

from backend.schemas.models import DataSourceIn
from backend.services import fhir_client, jdbc_client, profile_analyzer, repository
from backend.utils import error, success

logger = logging.getLogger(__name__)

datasources_bp = Blueprint("datasources", __name__, url_prefix="/api/datasources")


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
        return success({
            "analysis": {"source_type": "SQL",
                         "tables": [f"{t.get('schema', '')}.{t['table']}" for t in tables]},
            "assets": assets, "asset_count": len(assets),
        }, "SQL 数据源已就绪")
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

    return success({"analysis": analysis, "assets": assets, "asset_count": len(assets)}, "Profile 分析完成")


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
    return success(result, "连接成功")


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
    # 自动生成轮询 Query（EnsLib.SQL.Service.GenericService 的 Host 设置）
    first = saved[0]
    qtable = first["table"]
    query = f"SELECT * FROM {first['schema']}.{qtable}" if first["schema"] else f"SELECT * FROM {qtable}"
    key_field = (first["columns"][0]["name"] if first["columns"] else "")
    repository.update_datasource(ds_id, {
        "status": "analyzed",
        "tables": saved,
        "config": {**conn, "query": query, "key_field": key_field},
    })
    return success({"saved": saved, "count": len(saved)},
                   "数据表已保存（列结构已分析，轮询 Query 已自动生成）")


@datasources_bp.get("/<ds_id>/assets")
def list_assets(ds_id: str):
    """指定数据源的资产列表。"""
    return success({"items": repository.list_assets(ds_id)})
