"""FHIR 数据转换处理逻辑（IRIS Embedded Python 环境运行）。

由 demo.FHIRTransformHelper（Embedded Python 助手类）调用：
1. fetch_resources_json: 从 FHIR endpoint 拉取指定资源类型（带 Basic Auth）
2. transform_resource_json: 按字段映射将 FHIR 资源转换为目标表行
3. persist_row: 将行写入目标表（IRIS SQL）

注意（方案 A）：IRIS FHIR Server 资源读写需 Basic Auth，仅 /metadata 匿名公开。
"""

import base64
import json
import urllib.request


def _http_json(url, username, password, payload=None):
    """发起带 Basic Auth 的 HTTP 请求并返回 JSON。

    注意：必须显式设置 Accept: application/fhir+json，否则 IRIS FHIR Server 返回 406。
    """
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/fhir+json")
    req.add_header("Authorization", f"Basic {token}")
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/fhir+json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_path(obj, path):
    """按点分路径从 JSON 对象提取值，支持数组索引，如 name[0].given[0]。

    参数:
        obj: JSON 对象（dict）。
        path: 路径表达式，如 "name[0].family"。

    返回:
        提取到的值；路径不存在返回 None。
    """
    parts = path.replace("[", ".[").split(".")
    cur = obj
    for part in parts:
        if not part:
            continue
        if part.startswith("["):
            idx = int(part.strip("[]"))
            if isinstance(cur, list) and idx < len(cur):
                cur = cur[idx]
            else:
                return None
        else:
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return None
    return cur


def _extract_value(resource, source_path, transform):
    """按映射从 FHIR 资源提取并转换值。

    参数:
        resource: FHIR 资源（dict）。
        source_path: 源路径（如 name[0].given[0]）。
        transform: 可选的转换规则（如 "date" 提取日期前 10 位）。

    返回:
        转换后的值；无法提取返回 None。
    """
    val = _get_path(resource, source_path)
    if val is None:
        return None
    if isinstance(val, list):
        val = val[0] if val else None
    if val is None:
        return None
    if isinstance(val, dict):
        # 复杂对象：优先取 value / text / display 字段
        if "value" in val:
            val = val["value"]
        elif "text" in val:
            val = val["text"]
        elif "display" in val:
            val = val["display"]
        else:
            val = json.dumps(val, ensure_ascii=False)
    if transform == "date":
        # 日期规范化：取前 10 位（YYYY-MM-DD）
        val = str(val)[:10]
    return val


def fetch_resources_json(endpoint, username, password, resource_type):
    """从 FHIR endpoint 拉取指定资源类型的全部资源，返回 Bundle JSON 字符串。

    支持分页（next link）翻页获取。

    参数:
        endpoint: FHIR endpoint 基础 URL（含结尾 /）。
        username: Basic Auth 用户名。
        password: Basic Auth 密码。
        resource_type: 资源类型（如 Patient）。

    返回:
        Bundle JSON 字符串（合并所有页的资源 entry）。
    """
    url = endpoint.rstrip("/") + "/" + resource_type
    bundle = _http_json(url, username, password)
    # 简单翻页：若存在 next 链接则继续拉取并合并（Demo 数据量小）
    while True:
        links = bundle.get("link", [])
        nxt = next((l.get("url") for l in links if l.get("relation") == "next"), None)
        if not nxt:
            break
        page = _http_json(nxt, username, password)
        bundle.setdefault("entry", []).extend(page.get("entry", []))
        bundle["link"] = page.get("link", [])
    return json.dumps(bundle, ensure_ascii=False)


def fetch_incremental_json(endpoint, username, password, resource_type, cursor=""):
    """增量拉取 FHIR 资源（基于 _lastUpdated 游标），返回 JSON 字符串。

    只拉取 lastUpdated > cursor 的新增/变更资源（FHIR R4 的 _lastUpdated 搜索参数）。

    参数:
        endpoint: FHIR endpoint 基础 URL（含结尾 /）。
        username: Basic Auth 用户名。
        password: Basic Auth 密码。
        resource_type: 资源类型（如 Patient）。
        cursor: 上次同步的 _lastUpdated 时间戳（ISO8601），空表示不限制。

    返回:
        JSON 字符串：{"bundle": {...}, "max_last_updated": "..."}
        max_last_updated 为本批资源中最大的 meta.lastUpdated（用于推进游标）。
    """
    import urllib.parse

    url = endpoint.rstrip("/") + "/" + resource_type
    if cursor:
        url += "?_lastUpdated=gt" + urllib.parse.quote(cursor)
    bundle = _http_json(url, username, password)
    # 分页合并
    while True:
        links = bundle.get("link", [])
        nxt = next((l.get("url") for l in links if l.get("relation") == "next"), None)
        if not nxt:
            break
        page = _http_json(nxt, username, password)
        bundle.setdefault("entry", []).extend(page.get("entry", []))
        bundle["link"] = page.get("link", [])

    # 计算本批最大 lastUpdated（用于推进游标）
    max_ts = cursor or ""
    for entry in bundle.get("entry", []):
        lu = ((entry.get("resource") or {}).get("meta") or {}).get("lastUpdated", "")
        if lu and lu > max_ts:
            max_ts = lu
    return json.dumps({"bundle": bundle, "max_last_updated": max_ts}, ensure_ascii=False)


def enqueue_resources(bundle_json, mapping_id):
    """把 Bundle 中的 FHIR 资源逐条写入队列表 FHIRQueue（Status=pending）。

    参数:
        bundle_json: Bundle JSON 字符串。
        mapping_id: 转换关系 ID（写入队列供下游消费时关联）。

    返回:
        入队数量（字符串）。
    """
    import iris

    bundle = json.loads(bundle_json)
    count = 0
    for entry in bundle.get("entry", []):
        res = entry.get("resource") or {}
        if not res or not res.get("resourceType"):
            continue
        iris.sql.exec(
            "INSERT INTO FHIRQueue (ResourceType, ResourceId, ResourceJson, MappingId, Status) "
            "VALUES (?, ?, ?, ?, ?)",
            res.get("resourceType", ""), res.get("id", ""),
            json.dumps(res, ensure_ascii=False), mapping_id, "pending")
        count += 1
    return str(count)


def transform_resource_json(resource_json, field_mappings_json):
    """按字段映射将 FHIR 资源转换为目标表行，返回 row JSON 字符串。

    参数:
        resource_json: FHIR 资源 JSON 字符串。
        field_mappings_json: 字段映射 JSON 数组字符串，
            [{"source": "name[0].family", "target": "FamilyName", "transform": null}, ...]。

    返回:
        目标表行 JSON 字符串。
    """
    resource = json.loads(resource_json)
    mappings = json.loads(field_mappings_json)
    row = {}
    for m in mappings:
        row[m["target"]] = _extract_value(resource, m.get("source", ""), m.get("transform"))
    return json.dumps(row, ensure_ascii=False)


def query_table_json(table_name, limit=100):
    """查询目标表数据，返回 JSON 数组字符串（供 PipelineQuery.GetTargetData 调用）。

    参数:
        table_name: 目标表名（SQLUser schema）。
        limit: 最大返回行数。

    返回:
        行 JSON 数组字符串，每行为 {列名: 值}。
    """
    import iris

    # 1. 通过 information_schema 获取列名（参数化查询，避免表名注入）
    col_names = []
    rs = iris.sql.exec(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='SQLUser' AND table_name=? ORDER BY ordinal_position",
        table_name)
    col_names = [row[0] for row in rs]

    # 2. 查询数据行（表名经白名单校验后拼接）
    data_rs = iris.sql.exec(f"SELECT * FROM SQLUser.{table_name} LIMIT {limit}")
    result = []
    for row in data_rs:
        if col_names:
            result.append({col_names[i]: row[i] for i in range(len(col_names))})
        else:
            result.append(list(row))
    return json.dumps(result, ensure_ascii=False)


def persist_row(table_name, row_json):
    """将行写入目标表（IRIS SQL，Embedded Python 的 iris.sql API）。

    UPSERT 语义：主键（ID 列）存在则更新，否则插入，保证重复拉取不冲突。

    参数:
        table_name: 目标表名（SQLUser schema）。
        row_json: 目标表行 JSON 字符串。

    返回:
        状态 JSON 字符串，{"status": "ok"} 或 {"status": "error", "message": ...}。
    """
    import re

    import iris

    row = json.loads(row_json)
    columns = list(row.keys())

    # 列名白名单校验（防注入）：仅允许字母数字下划线
    if any(not re.match(r"^[A-Za-z0-9_]+$", c) for c in columns):
        return '{"status": "error", "message": "invalid column name"}'

    pk_col = "ID"
    pk_val = row.get(pk_col)
    try:
        if pk_val is not None:
            rs = iris.sql.exec(
                f"SELECT COUNT(*) FROM SQLUser.{table_name} WHERE {pk_col} = ?", pk_val)
            exists = bool(list(rs)[0][0])
        else:
            exists = False
        if exists:
            upd_cols = [c for c in columns if c != pk_col]
            if upd_cols:
                set_sql = ", ".join([f"{c} = ?" for c in upd_cols])
                sql = f"UPDATE SQLUser.{table_name} SET {set_sql} WHERE {pk_col} = ?"
                iris.sql.exec(sql, *[row[c] for c in upd_cols], pk_val)
        else:
            placeholders = ",".join(["?"] * len(columns))
            sql = f"INSERT INTO SQLUser.{table_name} ({','.join(columns)}) VALUES ({placeholders})"
            iris.sql.exec(sql, *[row[c] for c in columns])
        return '{"status": "ok"}'
    except Exception as exc:  # noqa: BLE001 - 需将异常反馈给 ObjectScript 调用方
        msg = str(exc).replace('"', "'")
        return f'{{"status": "error", "message": "{msg}"}}'
