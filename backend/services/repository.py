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
from backend.services import iris_connector

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
        iris_connector.reset_connections()


def get_json(global_name: str, key: str) -> dict | None:
    """读取一个 JSON 记录。"""
    native, conn = _get_native()
    try:
        raw = native.get(global_name, key)
        return json.loads(raw) if raw else None
    finally:
        iris_connector.reset_connections()


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
        iris_connector.reset_connections()


def list_keys(global_name: str) -> list[str]:
    """返回 global 的全部一级 key（按 IRIS 排序）。"""
    native, conn = _get_native()
    try:
        return [str(k) for k, raw in native.iterator(global_name) if raw is not None]
    finally:
        iris_connector.reset_connections()


def delete_json(global_name: str, key: str) -> bool:
    """删除 global 中一个 key（不存在也返回 True，幂等）。"""
    native, conn = _get_native()
    try:
        native.kill(global_name, key)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("删除 global %s(%s) 失败: %s", global_name, key, exc)
        return False
    finally:
        iris_connector.reset_connections()


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
            jdbc = cfg["jdbc_url"]
            # 命名 DSN 推导：jdbc:IRIS://host:port/<命名空间> → 用命名空间作 DSN 名
            # （CLINIC 源须连 CLINIC DSN，不能退化为 localTarget/USER）
            dsn_name = ""
            if jdbc.lower().startswith("jdbc:iris:"):
                dsn_name = jdbc.rsplit("/", 1)[-1].strip()
            conn = {
                "jdbc_url": jdbc,
                "dsn": cfg.get("dsn") or dsn_name or "",
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
    if kind == "FHIR":
        # FHIR：连接参数归一（base_url 同时以 endpoint 别名输出，供 check_connection 复用），
        # 能力/健康/候选集沿用注册与 refresh-resources 时保存的运行时（勿丢弃）。
        base = conn0.get("base_url") or ""
        conn = {
            "base_url": base,
            "endpoint": base,
            "username": conn0.get("username") or "",
            "password": conn0.get("password") or "",
            "profile_base": conn0.get("profile_base") or "",
        }
        delivery = {"mechanism": "fhir_put", "target": "FHIR server"}
        assets = [
            {"entity_name": (tb.get("entity_name") or tb.get("table")),
             "table": (tb.get("table")),
             "schema": tb.get("schema") or "FHIR",
             "profile": tb.get("profile") or "",
             "fields": tb.get("fields") or [c.get("name") if isinstance(c, dict) else str(c)
                                            for c in (tb.get("columns") or [])]}
            for tb in (tg.get("tables") or []) if tb.get("table")
        ]
        return {
            "kind": kind, "role": "target",
            "connection": conn,
            "capabilities": rt.get("capabilities") or {},
            "assets": assets, "poll": rt.get("poll") or {},
            "delivery": {**delivery, **(rt.get("delivery") or {})},
            "health": rt.get("health") or {},
            "candidates": rt.get("candidates") or {},
        }
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


def _sql_table_asset_id(ds_id: str, schema: str, table: str) -> str:
    """SQL 表资产的**稳定 id**（按表名派生，而非提交序号）。

    背景（实测缺陷）：原实现用 `{ds_id}_T{序号}`——二次选表时同一序号会指向另一张表，
    新表把旧表资产**静默覆盖**（`^demo.DataAsset` 以 id 为键），导致已选表丢失、
    后续 AI 匹配缺源资产。改为按 `schema.table` 派生：同一张表永远同一个 id。
    """
    key = f"{schema}.{table}" if schema else str(table)
    safe = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in key)
    return f"{ds_id}_T{safe}"


def save_sql_table_assets(ds_id: str, tables: list[dict],
                          merge: bool = True) -> dict:
    """保存 SQL 数据源选定的表资产（name=表名，fields=列名）。

    参数:
        ds_id: 数据源 ID。
        tables: [{"schema": "...", "table": "...", "columns": [{"name","type","size"}]}]
        merge: True=与**已选表合并**（本次未提交的已选表保留 → 二次选表不丢表，默认）；
               False=按本次提交集合替换（显式清理）。

    返回:
        {"assets": 本次保存的资产, "merged": 合并后的全部 SQL 表资产（顺序=已选在前、新表在后）,
         "total": 合并后数量, "replaced": 被本次替换（未提交）移除的表名}
    """
    ds = get_datasource(ds_id)
    prior = [a for a in list_assets(ds_id) if (a.get("type") or "SQL_TABLE") == "SQL_TABLE"]
    by_name = {str(a.get("name") or "").lower(): a for a in prior if a.get("name")}
    saved: list[dict] = []
    for tb in tables:
        table = str(tb.get("table") or "")
        schema = str(tb.get("schema") or "")
        if not table:
            continue
        existing = by_name.get(table.lower()) or {}
        asset_id = existing.get("id") or _sql_table_asset_id(ds_id, schema, table)
        record = {
            "id": asset_id,
            "source_id": ds_id,
            "name": table,
            "type": "SQL_TABLE",
            "fields": [c["name"] for c in tb.get("columns", [])],
            "structure": {
                "schema": schema,
                "table": table,
                "columns": tb.get("columns", []),
            },
            "source_name": ds.get("name", "") if ds else "",
        }
        # 复用既有资产时保留接口分析 Agent 的语义产物（ai_semantics/key_hint/ai_analysis）
        for k, v in (existing or {}).items():
            if k.startswith("ai_") or k in ("key_hint", "description"):
                record.setdefault(k, v)
        set_json("^demo.DataAsset", asset_id, record)
        saved.append(record)
        by_name[table.lower()] = record

    saved_ids = {a["id"] for a in saved}
    merged: list[dict] = []
    for a in prior:
        merged.append(next((s for s in saved if s["id"] == a["id"]), a))
    merged.extend(a for a in saved if a["id"] not in {p["id"] for p in prior})
    replaced = [a.get("name") for a in prior if a.get("id") not in saved_ids] if not merge else []
    # 数据源 `tables` 字段沿用旧结构 [{schema, table, columns}]（下游 datasource_runtime /
    # 管道生成按此结构读取）——资产记录（含 id/structure）在 assets 中返回，两者形状区分清楚。
    merged_tables = [{
        "schema": ((a.get("structure") or {}).get("schema") or ""),
        "table": (a.get("name") or ""),
        "columns": ((a.get("structure") or {}).get("columns") or []),
    } for a in merged]
    return {"assets": saved, "merged": merged, "tables": merged_tables,
            "total": len(merged_tables), "replaced": replaced}



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

def _identity_part(text) -> str:
    """身份比对用的名称归一：小写、去空格、**取末段**（剥掉 schema/数据源前缀）。

    2026-09-16 修 N10（同身份重复映射再生）：源资产名存在两种写法 —— 早期分析产物是
    `Patient`，重新选表/分析后变成 `DS71120_TSQLUser.Patient`（数据源_模式.表）。
    原身份判定直接用原字符串比较，于是 `Patient` ≠ `DS71120_TSQLUser.Patient` 被判成
    「不同身份」，保存时 id 撞号 → 派生 `R1_3 / R2_2 / R3_2 / R5_2` 这类重复行，
    映射列表翻倍（布局层虽已按 (源表, 目标资源) 去重、生成不受影响，但登记层不该脏）。
    取末段与 `sql2fhir_executor.normalize_mappings` 的折叠口径保持一致。
    """
    s = str(text or "").strip().lower().strip('"').strip("[]")
    if not s:
        return ""
    return s.rsplit(".", 1)[-1]


def _mapping_identity(m: dict) -> tuple:
    """映射身份 = (源, 目标实体, 目标类型)：同一身份视为同一条转换关系。

    `source`/`target_table` 都按 `_identity_part` 归一（剥 schema/数据源前缀），
    使 `Patient` 与 `DS71120_TSQLUser.Patient` 归为同一身份（N10）。
    """
    if not isinstance(m, dict):
        return ()
    src = _identity_part(m.get("source") or m.get("asset") or m.get("source_table") or "")
    tgt = _identity_part(m.get("target_table") or m.get("target") or m.get("target_entity") or "")
    tt = str(m.get("target_type") or "").strip().lower()
    return (src, tgt, tt) if (src or tgt) else ()


def _unique_mapping_id(base: str, used: set) -> str:
    """为 base 找一个未被占用的派生命名（R1 → R1_2 → R1_3 …）。"""
    base = str(base).strip() or _gen_id("M")
    if base not in used:
        return base
    n = 2
    while f"{base}_{n}" in used:
        n += 1
    return f"{base}_{n}"


def save_mappings(mappings: list[dict], overwrite: bool = False, report: dict | None = None) -> int:
    """保存转换关系列表，返回保存数量。

    id 冲突规则（实测缺陷：Agent A 每轮都从 R1 起编号，跨批次保存会**静默覆盖**旧映射，
    并让已生成管道的 mapping_ids 指向被改写的 id）：
      1) 同一身份（源→目标实体/类型）已存在 → 复用其 id（重复保存 = 幂等更新，不新增）；
      2) 身份不同但 id 已被占用（跨批次撞号）→ 派生新 id（R1 → R1_2 → …），**绝不覆盖**；
      3) 同批次内重复 id 同样派生（原有行为保留）。

    2026-09-15 修 N6（重复身份的历史副本不被收敛）：原先 `by_identity` 用字典推导式构建，
    同一身份存在多条时**后写覆盖前写**，导致保存永远落在"孪生兄弟"中的任意一条上，另一条
    （内容陈旧）长期留存；下游按 /mappings 全量取映射时可能选中陈旧那条。
    现在：同一身份的所有行都收集起来，优先复用与提交 id 同名者、否则用最早落库的一条，
    并把该身份的**其余重复行一并删除**（报告写入 report["collapsed"]）。

    2026-09-16 修 N10（同身份重复映射再生）：身份判定前先把名称归一到「末段」（剥掉 schema/
    数据源前缀），使 `Patient` 与 `DS71120_TSQLUser.Patient` 归为同一身份 —— 原先两者被判成
    不同身份，保存时 id 撞号 → 派生 `R1_3 / R2_2 / R3_2 / R5_2` 等重复行，映射列表翻倍。
    归并删除副本时，同时把管道实例 `mapping_ids` 里对被删 id 的引用改写为幸存 id
    （`pipeline_instances.rewrite_mapping_id`），保证引用不悬空。

    参数:
        mappings: 待保存的转换关系（就地补全 id/status）。幂等：安全重复提交。
        overwrite: True=显式按给定 id 覆盖（用户明确的"就地改写"语义，如编辑既有映射）。
        report: 可选出参，回填 {"collapsed": [被归并删除的 id]}，供 API 层提示用户。
    """
    existing = [m for m in list_mappings() if isinstance(m, dict)]
    used: set[str] = {str(m.get("id")) for m in existing if m.get("id")}
    by_identity: dict[tuple, list[dict]] = {}
    for m in existing:
        key = _mapping_identity(m)
        if key:
            by_identity.setdefault(key, []).append(m)

    collapsed: list[str] = []
    for m in mappings:
        mid = str(m.get("id") or "").strip() or _gen_id("M")
        key = _mapping_identity(m)
        same = None
        if key and not overwrite:
            cands = by_identity.get(key) or []
            # 规范化选择：优先复用与提交 id 同名那条，否则用列表序最早的一条（稳定、可预测）
            same = next((c for c in cands if str(c.get("id")) == mid), None) or (cands[0] if cands else None)
        if same is not None and same.get("id"):
            # 同一条转换关系（源→目标相同）：更新内容、沿用原 id，不新增、不改名
            mid = str(same["id"])
        elif mid in used and not overwrite:
            # id 撞号但身份不同 → 派生新 id（否则会覆盖别的映射）
            mid = _unique_mapping_id(mid, used)
            logger.warning("映射 id 冲突：%s 已被其它转换关系占用，改用 %s（不覆盖既有映射）",
                           m.get("id"), mid)
        used.add(mid)
        m["id"] = mid
        m.setdefault("status", "confirmed")
        set_json("^demo.Mapping", mid, m)
        if key:
            # 归并同身份的陈旧重复行（保留本次落库这条）
            for dup in by_identity.get(key) or []:
                dup_id = str(dup.get("id") or "")
                if dup_id and dup_id != mid and dup_id in used:
                    if delete_json("^demo.Mapping", dup_id):
                        used.discard(dup_id)
                        collapsed.append(dup_id)
                        logger.warning("映射 %s 与 %s 身份相同（%s），已归并删除陈旧副本",
                                       dup_id, mid, key)
                        # 已生成管道的 mapping_ids 可能引用被删的 id → 改写为幸存 id，
                        # 避免展示侧出现指向不存在映射的悬空芯片（F4 类 id 漂移）。
                        # 惰性导入：pipeline_instances 模块级已 import repository，避免循环导入。
                        try:
                            from backend.services import pipeline_instances as _pi

                            _pi.rewrite_mapping_id(dup_id, mid)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("映射引用改写失败（%s → %s）：%s", dup_id, mid, exc)
            by_identity[key] = [m]
    if report is not None:
        report["collapsed"] = collapsed
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
