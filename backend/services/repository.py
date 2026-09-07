"""IRIS global 存储封装（数据源 / 数据资产 / 转换关系）。

使用 Native SDK 的 global 操作读写 IRIS global（与 ObjectScript 的 ^demo.* 共享）：
  ^demo.DataSource(id)  = 数据源 JSON
  ^demo.DataAsset(id)   = 数据资产 JSON
  ^demo.Mapping(id)     = 转换关系 JSON
"""

import json
import logging
import time
from datetime import datetime

from backend.schemas.models import DataSourceIn, SourceAssetModel, TargetInterfaceModel, TransformationPlan

logger = logging.getLogger(__name__)


def _get_native():
    """获取 Native SDK 对象与连接（调用方负责 close）。"""
    import iris

    from backend.services import iris_connector

    conn = iris_connector.get_connection()
    return iris.createIRIS(conn), conn


def set_json(global_name: str, key: str, data: dict) -> None:
    """写入一个 JSON 记录到 global。

    注意：Native SDK 的 set 签名是 set(value, globalName, subscripts...)。
    """
    native, conn = _get_native()
    try:
        native.set(json.dumps(data, ensure_ascii=False), global_name, key)
    finally:
        conn.close()


def get_json(global_name: str, key: str) -> dict | None:
    """读取一个 JSON 记录。"""
    native, conn = _get_native()
    try:
        raw = native.get(global_name, key)
        return json.loads(raw) if raw else None
    finally:
        conn.close()


def list_json(global_name: str) -> list[dict]:
    """遍历 global，按 key 序返回所有记录。

    使用 Native SDK 的 iterator()：迭代 (key, value) 对。
    """
    native, conn = _get_native()
    try:
        items: list[dict] = []
        for _key, raw in native.iterator(global_name):
            if raw:
                try:
                    items.append(json.loads(raw))
                except json.JSONDecodeError:
                    logger.warning("global %s(%s) 非 JSON，跳过", global_name, _key)
        return items
    finally:
        conn.close()


def _gen_id(prefix: str) -> str:
    """生成唯一 ID（时间戳后 5 位，足够演示用）。"""
    return f"{prefix}{int(time.time() * 1000) % 100000:05d}"


# ---------------- 数据源 ----------------

def create_datasource(data: DataSourceIn) -> str:
    """注册数据源并写入 global。"""
    ds_id = _gen_id("DS")
    record = {
        "id": ds_id,
        "name": data.name,
        "type": data.type,
        "endpoint": data.endpoint.rstrip("/") + "/" if data.endpoint else "",
        "auth": data.auth.model_dump(),
        "config": data.config or {},
        "status": "registered",
        "created_at": datetime.now().isoformat(),
    }
    set_json("^demo.DataSource", ds_id, record)
    logger.info("数据源注册成功: %s (%s, %s)", ds_id, data.name, data.type)
    return ds_id


def datasource_runtime(ds: dict) -> dict:
    """数据源运行契约（Connection Contract）——参数归一单一取值。

    有显式 runtime.connection 用之；否则从旧字段惰性构造（FHIR endpoint 在记录顶层/auth，
    SQL 在 config），保证前端 / generate / AI 上下文读到一致的 connection 结构。
    真实能力/增量键探测由 connection_profiler（P1）补全到 runtime。
    """
    kind = (ds.get("type") or "").upper()
    rt = ds.get("runtime") or {}
    # SQL 源的 connection 是「向导可变配置」（query/key_field/dsn 可能被更新），
    # 始终以 config 现算，不复用 runtime 缓存的旧值（曾缓存 key_field=ID 导致 SQLService 报错）
    if rt.get("connection") and kind != "SQL":
        return rt

    cfg = ds.get("config") or {}
    assets = []
    poll: dict = {}
    caps: dict = {}
    conn: dict = {}
    if kind == "FHIR":
        # FHIR：endpoint/auth 在记录顶层（历史上也有 config 版本，兼容读）
        conn = {
            "endpoint": ds.get("endpoint") or cfg.get("endpoint") or "",
            "username": (ds.get("auth") or {}).get("username") or cfg.get("username") or "superuser",
            "password": (ds.get("auth") or {}).get("password") or cfg.get("password") or "SYS",
        }
        caps = {"auth": "basic", "incremental_search": "_lastUpdated"}
        poll = {"mechanism": "cursor"}
        assets = [
            {"name": a.get("name"), "type": a.get("type") or "FHIR",
             "fields": a.get("fields") or []}
            for a in list_assets(ds.get("id") or "") if a.get("name")]
    elif kind == "SQL":
        if cfg.get("jdbc_url"):
            conn = {
                "jdbc_url": cfg["jdbc_url"],
                "driver_class": cfg.get("driver_class") or "",
                "username": cfg.get("username") or "",
                "password": cfg.get("password") or "",
                "query": cfg.get("query") or "",
                "key_field": cfg.get("key_field") or "ID",
            }
            caps = {"dialect": "jdbc"}
        else:
            conn = {
                "dsn": cfg.get("dsn") or "localTarget",
                "query": cfg.get("query") or "",
                "key_field": cfg.get("key_field") or "ID",
            }
            caps = {"dialect": "IRIS"}
        poll = {"mechanism": "key_field", "key_column": conn.get("key_field") or "ID"}
        assets = [
            {"name": t.get("table"), "type": "SQL_TABLE", "schema": t.get("schema"),
             "table": t.get("table"),
             "fields": [c["name"] if isinstance(c, dict) else str(c) for c in (t.get("columns") or [])]}
            for t in (ds.get("tables") or []) if t.get("table")]
    return {
        "kind": kind, "role": "source",
        "connection": conn, "capabilities": caps,
        "assets": assets, "poll": poll, "delivery": {}, "health": {},
    }


def target_runtime(tg: dict) -> dict:
    """数据目标运行契约（Connection Contract）——参数归一单一取值。

    统一 connection 结构：DB 目标 {jdbc_url|dsn,...}；SOAP 目标 {wsdl/service/packages/endpoint/bo_class}。
    目标写入方式（delivery）由 connection_profiler（P1）进一步补全。
    """
    kind = (tg.get("type") or "").upper()
    rt = tg.get("runtime") or {}
    if rt.get("connection"):
        return rt

    conn0 = tg.get("connection") or {}
    assets = []
    delivery: dict = {}
    if kind == "SOAP":
        # SOAP：connection 与 TargetInterface.config 可能字段不全（endpoint 只在一处）——归一补齐
        svc = conn0.get("service") or "default"
        conn = {
            "wsdl": conn0.get("wsdl") or "",
            "service": svc,
            "packages": conn0.get("packages") or {},
            "endpoint": conn0.get("endpoint") or "",
            "bo_class": conn0.get("bo_class") or conn0.get("boClass") or tg.get("bo_class") or "",
        }
        delivery = {"mechanism": "soap_operation", "target": svc}
        assets = [
            {"entity_name": (tb.get("entity_name") or tb.get("table")), "table": (tb.get("table")),
             "schema": tb.get("schema") or "SOAP",
             "fields": tb.get("fields") or [c.get("name") if isinstance(c, dict) else str(c)
                                            for c in (tb.get("columns") or [])]}
            for tb in (tg.get("tables") or [])]
    else:  # DB
        conn = {
            "jdbc_url": conn0.get("jdbc_url") or "",
            "dsn": conn0.get("dsn") or "localTarget",
            "driver_class": conn0.get("driver_class") or "",
            "username": conn0.get("username") or "",
            "password": conn0.get("password") or "",
        }
        delivery = {"mechanism": "upsert"}
        assets = [
            {"table": tb.get("table"), "schema": tb.get("schema") or "SQLUser",
             "columns": tb.get("columns") or []}
            for tb in (tg.get("tables") or []) if tb.get("table")]
    return {
        "kind": kind, "role": "target",
        "connection": conn, "capabilities": {},
        "assets": assets, "poll": {}, "delivery": delivery, "health": {},
    }


def get_datasource(ds_id: str) -> dict | None:
    """按 ID 获取数据源（含归一化 runtime 契约）。"""
    ds = get_json("^demo.DataSource", ds_id)
    if ds:
        ds["runtime"] = datasource_runtime(ds)
    return ds


def list_datasources() -> list[dict]:
    """返回全部数据源（每项含归一化 runtime 契约）。"""
    items = list_json("^demo.DataSource")
    for ds in items:
        ds["runtime"] = datasource_runtime(ds)
    return items


def update_datasource(ds_id: str, patch: dict) -> None:
    """更新数据源字段。"""
    ds = get_datasource(ds_id)
    if not ds:
        return
    ds.update(patch)
    set_json("^demo.DataSource", ds_id, ds)


def update_asset(asset_id: str, patch: dict) -> None:
    """更新发现资产（^demo.DataAsset）并尽量同步分层模型资产（^demo.SourceAsset）。

    用于写入接口分析 Agent 的语义结论（description/key_hint/comment 等）。
    """
    rec = get_json("^demo.DataAsset", asset_id)
    if rec:
        rec.update(patch)
        set_json("^demo.DataAsset", asset_id, rec)
    rec2 = get_json("^demo.SourceAsset", asset_id)
    if rec2:
        rec2.update(patch)
        set_json("^demo.SourceAsset", asset_id, rec2)


def enrich_target_table(target_id: str, table_name: str, patch: dict) -> None:
    """给数据目标内指定表/实体记录补 AI 分析字段（description/direction…，不影响列）。"""
    tg = get_json("^demo.Target", target_id)
    if not tg:
        return
    for t in tg.get("tables", []):
        if (t.get("table") or t.get("entity_name")) == table_name:
            t.update(patch)
    set_json("^demo.Target", target_id, tg)


# ---------------- 数据资产 ----------------

def save_assets(ds_id: str, resource_types: list[str]) -> list[dict]:
    """保存数据源发现的资产（FHIR 资源类型），返回资产列表。"""
    ds = get_datasource(ds_id)
    assets = []
    for idx, rtype in enumerate(resource_types):
        asset_id = f"{ds_id}_A{idx + 1:03d}"
        record = {
            "id": asset_id,
            "source_id": ds_id,
            "name": rtype,
            "type": "FHIR_RESOURCE",
            "structure": {"resource_type": rtype},
            "source_name": ds.get("name", "") if ds else "",
        }
        set_json("^demo.DataAsset", asset_id, record)
        assets.append(record)
    return assets


def save_sql_table_assets(ds_id: str, tables: list[dict]) -> list[dict]:
    """保存 SQL 数据源选定的表资产（name=表名，fields=列名）。

    参数:
        ds_id: 数据源 ID。
        tables: [{"schema": "...", "table": "...", "columns": [{"name", "type", "size"}]}]
    """
    ds = get_datasource(ds_id)
    assets = []
    for idx, tb in enumerate(tables):
        asset_id = f"{ds_id}_T{idx + 1:03d}"
        record = {
            "id": asset_id,
            "source_id": ds_id,
            "name": tb.get("table", ""),
            "type": "SQL_TABLE",
            "fields": [c["name"] for c in tb.get("columns", [])],
            "structure": {
                "schema": tb.get("schema", ""),
                "table": tb.get("table", ""),
                "columns": tb.get("columns", []),
            },
            "source_name": ds.get("name", "") if ds else "",
        }
        set_json("^demo.DataAsset", asset_id, record)
        assets.append(record)
    return assets


def list_assets(ds_id: str | None = None) -> list[dict]:
    """返回资产列表（可按数据源过滤）。"""
    assets = list_json("^demo.DataAsset")
    if ds_id:
        assets = [a for a in assets if a.get("source_id") == ds_id]
    return assets


def get_asset(asset_id: str) -> dict | None:
    """按 ID 获取数据资产。"""
    return get_json("^demo.DataAsset", asset_id)


# ---------------- 转换关系 ----------------

def save_mappings(mappings: list[dict]) -> int:
    """保存转换关系列表，返回保存数量。"""
    # 兜底：同一批次内 id 重复会导致后保存覆盖先保存（^demo.Mapping 以 id 为键），
    # 自动加后缀唯一化，避免 AI 确认多条转换关系时丢失。
    used: set[str] = set()
    for m in mappings:
        mid = m.get("id") or _gen_id("M")
        if mid in used:
            mid = f"{mid}_{len(used)}"
        used.add(mid)
        m["id"] = mid
        m.setdefault("status", "confirmed")
        set_json("^demo.Mapping", mid, m)
    return len(mappings)


def list_mappings() -> list[dict]:
    """返回全部转换关系。"""
    return list_json("^demo.Mapping")


# ---------------- 转换目标（数据目标 / 目标表） ----------------

def create_target(name: str, target_type: str, connection: dict, tables: list[dict] | None = None) -> str:
    """注册一个数据目标（如数据库），返回目标 ID。

    参数:
        name: 目标名称。
        target_type: 目标类型（DB/SOAP）。
        connection: 连接配置信息。
        tables: 选定或分析出的实体/表结构列表。
    """
    target_id = _gen_id("TG")
    record = {
        "id": target_id,
        "name": name,
        "type": target_type,
        "connection": connection,
        "status": "registered",
        "tables": tables or [],
        "created_at": datetime.now().isoformat(),
    }
    set_json("^demo.Target", target_id, record)
    logger.info("数据目标注册成功: %s (%s)", target_id, name)
    return target_id


def get_target(target_id: str) -> dict | None:
    """按 ID 获取数据目标（含归一化 runtime 契约）。"""
    tg = get_json("^demo.Target", target_id)
    if tg:
        tg["runtime"] = target_runtime(tg)
    return tg


def list_targets() -> list[dict]:
    """返回全部数据目标（每项含归一化 runtime 契约）。"""
    items = list_json("^demo.Target")
    for tg in items:
        tg["runtime"] = target_runtime(tg)
    return items


def update_target(target_id: str, patch: dict) -> None:
    """更新数据目标字段。"""
    tg = get_target(target_id)
    if not tg:
        return
    tg.update(patch)
    set_json("^demo.Target", target_id, tg)


def add_target_table(target_id: str, schema: str, table: str,
                     columns: list[dict]) -> None:
    """将用户选定的表保存为目标表（含列结构，去重更新）。

    参数:
        target_id: 数据目标 ID。
        schema: 表所在 schema。
        table: 表名。
        columns: 列结构 [{"name": "...", "type": "...", "size": n}]。
    """
    tg = get_target(target_id)
    if not tg:
        return
    tables = [t for t in tg.get("tables", [])
              if not (t.get("schema") == schema and t.get("table") == table)]
    tables.append({
        "schema": schema,
        "table": table,
        "columns": columns,
        "status": "selected",
    })
    tg["tables"] = tables
    set_json("^demo.Target", target_id, tg)


# ---------------- 分层领域模型 ----------------

def _model_record(model, prefix: str) -> tuple[str, dict]:
    """将 Pydantic 领域模型转为可存储记录。"""
    record = model.model_dump(by_alias=True, exclude_none=True)
    ident = record.get("id") or _gen_id(prefix)
    record["id"] = ident
    record.setdefault("created_at", datetime.now().isoformat())
    return ident, record


def save_source_asset(model: SourceAssetModel | dict) -> dict:
    ident, record = _model_record(model if isinstance(model, SourceAssetModel)
                                  else SourceAssetModel(**model), "SA")
    set_json("^demo.SourceAsset", ident, record)
    return record


def get_source_asset(asset_id: str) -> dict | None:
    return get_json("^demo.SourceAsset", asset_id)


def list_source_assets(source_id: str | None = None) -> list[dict]:
    rows = list_json("^demo.SourceAsset")
    # 兼容旧版发现流程写入的 ^demo.DataAsset。
    if not rows:
        rows = list_json("^demo.DataAsset")
    return [r for r in rows if not source_id or r.get("source_id") == source_id]


def save_target_interface(model: TargetInterfaceModel | dict) -> dict:
    ident, record = _model_record(model if isinstance(model, TargetInterfaceModel)
                                  else TargetInterfaceModel(**model), "TI")
    set_json("^demo.TargetInterface", ident, record)
    return record


def get_target_interface(interface_id: str) -> dict | None:
    return get_json("^demo.TargetInterface", interface_id)


def list_target_interfaces() -> list[dict]:
    rows = list_json("^demo.TargetInterface")
    if not rows:
        rows = list_json("^demo.Target")
    return rows


def save_transformation_plan(plan: TransformationPlan | dict) -> dict:
    ident, record = _model_record(plan if isinstance(plan, TransformationPlan)
                                  else TransformationPlan(**plan), "TP")
    record["id"] = ident
    set_json("^demo.TransformationPlan", ident, record)
    return record


def get_transformation_plan(plan_id: str) -> dict | None:
    return get_json("^demo.TransformationPlan", plan_id)


def list_transformation_plans() -> list[dict]:
    return list_json("^demo.TransformationPlan")


def update_transformation_plan(plan_id: str, patch: dict) -> dict | None:
    plan = get_transformation_plan(plan_id)
    if not plan:
        return None
    plan.update(patch)
    set_json("^demo.TransformationPlan", plan_id, plan)
    return plan


# Domain-oriented aliases kept stable for callers that use model terminology.
create_source_asset = save_source_asset
create_target_interface = save_target_interface
create_transformation_plan = save_transformation_plan
