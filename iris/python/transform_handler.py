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


def _split_args(expr):
    """按逗号切分函数参数，忽略引号内与括号嵌套内的逗号。

    参数:
        expr: 函数括号内的内容（如 "Patient.FamilyName, ' ', Patient.GivenName"）。

    返回:
        参数列表。
    """
    args = []
    depth = 0
    cur = ""
    quote = None
    for ch in expr:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            cur += ch
        elif ch == "(":
            depth += 1
            cur += ch
        elif ch == ")":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            args.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        args.append(cur.strip())
    return args


def _get_field(resource, source_path):
    """从源数据按字段路径提取值（兼容多段「限定名」前缀 + 扁平行末段兜底）。

    参数:
        resource: 源数据（FHIR 资源 dict / SQL 行 dict）。
        source_path: 字段路径（如 name[0].given[0]，或 SQL 行扁平列名）。

    返回:
        归一后的值；提取失败返回 None。
    """
    path = str(source_path or "").strip()
    val = _get_path(resource, path)
    if val is None and "." in path:
        # 逐级剥前缀：`SQLUser.Patient.ID`（schema.表.列）/ `Patient.name[0].family` / `表.列`
        # ⚠ 旧实现只剥**一层**（`split(".", 1)[1]`）：三段全限定名（SQL 源映射实测常这么写）
        #   剥成 `Patient.ID` 仍取不到 → **整行值全为 null** → DB 目标 INSERT 报
        #   `<Field 'SQLUser.Patient.ID' is required>`（消息 Error、目标零落地，而生成接口一路
        #   `code:0`；2026-09-17 Round 2 实测）。限定名深度不必假设，逐级尝试即可。
        parts = path.split(".")
        for i in range(1, len(parts)):
            val = _get_path(resource, ".".join(parts[i:]))
            if val is not None:
                break
    if val is None and isinstance(resource, dict) and path:
        # 扁平源行（SQL 轮询行 = 裸列名）兜底：按**末段**做大小写不敏感匹配——
        # 覆盖 `Patient.familyname` 这类限定名与实际列名大小写不一致的写法。
        last = path.split(".")[-1].strip().lower()
        for k, v in resource.items():
            if str(k).strip().lower() == last:
                val = v
                break
    if isinstance(val, list):
        val = val[0] if val else None
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
    return val


def _eval_expr(resource, source_expr):
    """解析映射 source 表达式并求值。

    支持的表达式（映射 source 字段由 AI / 用户生成，需覆盖常见转换）：
      1. 字符串字面量：' ' / "X"（引号包裹）
      2. 字段路径：Patient.FamilyName / name[0].family / SQL 行列名（含 "表.列" 前缀兼容）
      3. concat 函数：concat(字段, ' ', 字段)（参数可为上述任意组合，支持嵌套）

    参数:
        resource: 源数据 dict。
        source_expr: 表达式字符串。

    返回:
        求值结果（None 表示无法提取）。
    """
    expr = (source_expr or "").strip()
    if not expr:
        return None
    # 1. 字面量（成对引号包裹）
    if len(expr) >= 2 and expr[0] == expr[-1] and expr[0] in "'\"":
        return expr[1:-1]
    # 2. concat(…)：拼接各参数求值结果
    lower = expr.lower()
    if lower.startswith("concat(") and expr.endswith(")"):
        inner = expr[len("concat("):-1]
        pieces = []
        for arg in _split_args(inner):
            val = _eval_expr(resource, arg)
            if val is not None:
                pieces.append(str(val))
        return "".join(pieces)
    # 3. 其余表达式按字段路径兜底处理
    return _get_field(resource, expr)


def _extract_value(resource, source_expr, transform):
    """按映射从源数据提取并转换值（支持受控指令 / concat 表达式 / 字段路径 / 字面量）。

    受控指令（LLM 决策的 transform，平台只执行）：
      - constant:<值>：无源字段时输出固定值（如 intent=constant:order）；
      - date：取日期前 10 位；
      - term_map:<skill>：运行期由判码 Skill 处理（此处返回源值，双 coding 由打包器追加）。

    参数:
        resource: 源数据（FHIR 资源 dict / SQL 行 dict）。
        source_expr: source 表达式（concat(...) / 字段路径 / 源列名）；可为 None（配合常量指令）。
        transform: 可选的受控指令。

    返回:
        转换后的值；无法提取返回 None。
    """
    tr = str(transform or "")
    if tr.startswith("constant:"):
        return tr[len("constant:"):]
    if tr == "constant" and source_expr:
        # 兼容写法：LLM 有时把常量值放在 source 且 transform 只写 "constant"
        return str(source_expr)
    if not source_expr:
        return None
    val = _eval_expr(resource, source_expr)
    if val is None:
        return None
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

    幂等防重：同一 (ResourceType, ResourceId, MappingId) 已在队列（无论
    pending/processed）则跳过——游标未推进或重复同步时避免无限堆积，
    保证后入队资源（如 Observation）也能被下游消费到。

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
        rtype, rid = res.get("resourceType", ""), res.get("id", "")
        if not rid:
            continue
        # 幂等检查：同 (ResourceType, ResourceId, MappingId) 已在队列（pending/processed）则跳过。
        # 注：iris.sql 结果集对象无 fetchall 属性，用可迭代协议遍历。
        exists = False
        for _ in iris.sql.exec(
                "SELECT ID FROM FHIRQueue WHERE ResourceType=? AND ResourceId=? AND MappingId=?",
                rtype, rid, mapping_id):
            exists = True
            break
        if exists:
            continue  # 已存在（pending/processed）→ 跳过防重复
        iris.sql.exec(
            "INSERT INTO FHIRQueue (ResourceType, ResourceId, ResourceJson, MappingId, Status) "
            "VALUES (?, ?, ?, ?, ?)",
            rtype, rid, json.dumps(res, ensure_ascii=False), mapping_id, "pending")
        count += 1
    return str(count)


def transform_resource_json(resource_json, field_mappings_json):
    """按字段映射将源数据（FHIR 资源 / SQL 行）转换为目标行 JSON，返回 row JSON 字符串。

    参数:
        resource_json: 源数据——FHIR 资源 JSON 字符串，或已解析对象（dict / SQL 行行 JSON）。
        field_mappings_json: 字段映射 JSON 数组字符串，
            [{"source": "name[0].family", "target": "FamilyName", "transform": null}, ...]。

    返回:
        目标行 JSON 字符串。
    """
    resource = resource_json if isinstance(resource_json, dict) else json.loads(resource_json)
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
