"""连接探查 Agent（Connection Profiler）：探测数据源/数据目标并产出运行契约。

对已登记的数据源/目标做真实探测，输出/刷新其 runtime（Connection Contract，见
docs/ConnectionContract-设计.md），成为前端 / AI 上下文 / 管道生成 / 自动验证的单一参数来源：

- FHIR 源：GET metadata → 版本 / 认证 / 分页 / `_lastUpdated` 增量能力；资产=已发现资源真实字段
- SQL 源：JDBC 连通 + 已选表列 → 轮询增量键（key_field）
- DB 目标：连通 / 表存在（UPSERT 语义）
- SOAP 目标：WSDL 导入 + BO MessageMap → 操作语义判定（Add/Create→写入型，Get/Query→查询型）
                  + endpoint 可达性
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from backend.services import fhir_client, iris_connector, jdbc_client, repository

logger = logging.getLogger(__name__)


def _op_kind(method: str) -> str:
    """按 SOAP 方法名粗判操作语义（写入型 / 查询型）。"""
    m = (method or "").lower()
    if m.startswith(("add", "create", "insert", "update", "put", "post", "save", "upsert", "write")):
        return "write"
    if m.startswith(("get", "query", "search", "find", "list", "read", "fetch", "check")):
        return "read"
    return "other"


def _fhir_capabilities(endpoint: str, username: str, password: str) -> tuple[dict, dict]:
    """探测 FHIR metadata：版本 / 分页 / _lastUpdated 增量支持；返回 (capabilities, poll)。"""
    cap = fhir_client.get_capability_statement(endpoint, username, password)
    fhir_version = cap.get("fhirVersion") or ""
    search_params: set[str] = set()
    for r in cap.get("rest") or []:
        for sp in r.get("searchParam") or []:
            search_params.add(str(sp.get("name") or ""))
        for res in r.get("resource") or []:
            for sp in res.get("searchParam") or []:
                search_params.add(str(sp.get("name") or ""))
    incremental = "_lastUpdated" in search_params
    caps = {
        "auth": "basic", "paginated": True, "fhir_version": fhir_version,
        "incremental_search": "_lastUpdated" if incremental else "",
    }
    poll = {"mechanism": "cursor" if incremental else "full"}
    if not incremental:
        poll["note"] = "FHIR Server 未声明 _lastUpdated 搜索参数，增量受限（将全量拉取）"
    return caps, poll


def _http_reachable(url: str) -> dict:
    """HTTP 端点可达性：能收到任何 HTTP 响应（含 4xx/5xx）即视为可达。"""
    if not url:
        return {"ok": False, "detail": "缺少 endpoint"}
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=8) as _:
            return {"ok": True, "detail": "http 200"}
    except urllib.error.HTTPError as exc:  # noqa: BLE001 - 有响应即可达（4xx/5xx 说明服务在）
        return {"ok": True, "detail": f"http {exc.code}"}
    except Exception as exc:  # noqa: BLE001 - 连接失败/超时
        return {"ok": False, "detail": str(exc)[:200]}

def _fhir_sample_fields(endpoint: str, username: str, password: str,
                        resource_type: str) -> list[str]:
    """拉取若干真实样例资源，递归合并可访问字段路径（真实数据形态，多条并集更全）。

    例：Patient 样例 → id / name[0].family / name[0].given[0] / gender / address[0].city ...
    数组元素以 [0] 表示，与 AI 映射/转换引擎的访问路径一致。
    返回: 去重后的字段路径列表（最多 60 个）。
    """
    import base64

    try:
        base = endpoint.rstrip("/") + "/"
        req = urllib.request.Request(base + f"{resource_type}?_count=10")
        req.add_header("Accept", "application/fhir+json")
        req.add_header("Authorization",
                       "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode())
        with urllib.request.urlopen(req, timeout=15) as resp:
            bundle = json.loads(resp.read().decode("utf-8"))
        entries = bundle.get("entry") or []
        if not entries:
            return []
        fields: list[str] = []

        def walk(obj, prefix, depth):
            if depth > 6:
                return
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k.startswith("_"):  # 忽略 FHIR 扩展包装元素
                        continue
                    p = f"{prefix}.{k}" if prefix else k
                    if isinstance(v, (dict, list)):
                        walk(v, p, depth + 1)
                    elif isinstance(v, (str, int, float, bool)) or v is None:
                        fields.append(p)
            elif isinstance(obj, list):
                for i, item in enumerate(obj[:1]):  # 数组访问统一取 [0]
                    walk(item, f"{prefix}[{i}]", depth + 1)

        for entry in entries:
            resource = entry.get("resource") or {}
            walk(resource, "", 0)
        # 去重并按稳定顺序返回（resourceType 对转换无意义，过滤）
        seen: list[str] = []
        for f in fields:
            if f and f != "resourceType" and f not in seen:
                seen.append(f)
        return seen[:60]
    except Exception as exc:  # noqa: BLE001 - 样例提取失败返回空
        logger.warning("FHIR 样例字段提取失败 %s: %s", resource_type, exc)
        return []


def _fhir_resource_fields(endpoint: str, username: str, password: str,
                          resource_type: str) -> list[str]:
    """拉取指定 FHIR 资源类型的 StructureDefinition，提取可访问字段路径。

    基于 snapshot.element 的 path 与 max（* 表示多值数组 → 访问加 [0]），
    输出形如 name[0].family / given[0] 的路径（去掉资源类型前缀），供 AI/转换使用。
    失败返回空列表。
    """
    import base64

    try:
        base = endpoint.rstrip("/") + "/"
        url = (f"{base}StructureDefinition?url="
               f"{urllib.parse.quote('http://hl7.org/fhir/StructureDefinition/' + resource_type)}")
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/fhir+json")
        req.add_header("Authorization",
                       "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode())
        with urllib.request.urlopen(req, timeout=15) as resp:
            bundle = json.loads(resp.read().decode("utf-8"))
        sd = next((e.get("resource") for e in bundle.get("entry", [])
                   if e.get("resource", {}).get("resourceType") == "StructureDefinition"), None)
        if not sd:
            return []
        elements = sd.get("snapshot", {}).get("element", [])
        path_max: dict[str, str] = {}
        for el in elements:
            if el.get("path") and el.get("max") is not None:
                path_max[el["path"]] = str(el["max"])
        paths = [el.get("path", "") for el in elements]
        leaves = [p for p in paths
                  if p and p.startswith(resource_type + ".") and not any(
                      q.startswith(p + ".") for q in paths)]
        fields: list[str] = []
        prefix = resource_type + "."
        for leaf in leaves:
            segs = leaf[len(prefix):].split(".")
            access = []
            acc = resource_type
            for seg in segs:
                acc = acc + "." + seg
                access.append(f"{seg}[0]" if path_max.get(acc) == "*" else seg)
            if access:
                fields.append(".".join(access))
        return fields[:80]
    except Exception as exc:  # noqa: BLE001 - 字段提取失败不致命
        logger.warning("FHIR StructureDefinition 解析失败 %s: %s", resource_type, exc)
        return []


def profile_source(ds: dict) -> dict:
    """探查数据源并返回其 runtime 契约（含 health）。调用方负责写回记录。"""
    import datetime

    base = repository.datasource_runtime(ds)
    kind = base.get("kind", "")
    conn = base.get("connection") or {}
    health: dict = {"ok": False, "checked_at": "", "detail": ""}
    caps = dict(base.get("capabilities") or {})
    poll = dict(base.get("poll") or {})
    assets = list(base.get("assets") or [])

    if kind == "FHIR":
        ep = conn.get("endpoint") or ""
        if not ep:
            health["detail"] = "缺少 endpoint"
        else:
            try:
                fcaps, fpoll = _fhir_capabilities(ep, conn.get("username"), conn.get("password"))
                caps.update(fcaps)
                poll.update(fpoll)
                health = {"ok": True,
                          "checked_at": datetime.datetime.now().isoformat(),
                          "detail": f"metadata OK（fhir {caps.get('fhir_version', '')}）"}
                # FHIR 资产真实字段（demo 核心资源从 StructureDefinition 提取，供 AI/转换使用）
                _core = {"Patient", "Observation"}
                for _a in assets:
                    _rt = _a.get("name") or ""
                    if _rt in _core and not _a.get("fields"):
                        _f = _fhir_sample_fields(ep, conn.get("username"), conn.get("password"), _rt)
                        if not _f:
                            _f = _fhir_resource_fields(ep, conn.get("username"), conn.get("password"), _rt)
                        if _f:
                            _a["fields"] = _f
            except Exception as exc:  # noqa: BLE001
                health = {"ok": False,
                          "checked_at": datetime.datetime.now().isoformat(),
                          "detail": str(exc)[:200]}
    elif kind == "SQL":
        cfg = ds.get("config") or {}
        try:
            if cfg.get("jdbc_url"):
                res = jdbc_client.test_connection(cfg)
            else:
                rows = iris_connector.query("SELECT 1")
                res = {"ok": bool(rows), "message": "localTarget OK" if rows else "ping 失败"}
            health = {"ok": bool(res.get("ok")),
                      "checked_at": datetime.datetime.now().isoformat(),
                      "detail": (res.get("message") or "")[:200]}
            # 增量键探测兜底：向导已写 config.key_field 用之；否则按表列启发式选键
            # （优先 ID/Code/No 类列，其次首列），并探测更新时间列作提示
            key_field = (cfg.get("key_field") or "").strip()
            if not key_field and assets:
                first_asset_fields = assets[0].get("fields") or []
                candidates = [f for f in first_asset_fields if str(f).lower() in ("id", "code", "no", "key", "patno")]
                key_field = str(candidates[0] if candidates else (first_asset_fields[0] if first_asset_fields else "ID"))
            if key_field:
                poll["mechanism"] = "key_field"
                poll["key_column"] = key_field
                time_cols = []
                for a in assets:
                    time_cols += [str(f) for f in (a.get("fields") or [])
                                  if str(f).lower() in ("updatedat", "lastupdated", "modifiedon", "update_time")]
                if time_cols:
                    poll["time_column_candidates"] = time_cols
                if not time_cols and not (cfg.get("key_field") or ""):
                    poll["note"] = "未检测到 IDENTITY/时间增量列，轮询以所选主键/首列为准（可改选更新时间列做增量）"
        except Exception as exc:  # noqa: BLE001
            health = {"ok": False, "detail": str(exc)[:200]}

    return {**base, "capabilities": caps, "poll": poll, "assets": assets, "health": health}



def profile_target(tg: dict) -> dict:
    """探查数据目标并返回其 runtime 契约（含 delivery 操作语义 / health）。"""
    import datetime

    base = repository.target_runtime(tg)
    kind = base.get("kind", "")
    conn = base.get("connection") or {}
    caps = dict(base.get("capabilities") or {})
    delivery = dict(base.get("delivery") or {})
    assets = list(base.get("assets") or [])
    health: dict = {"ok": False, "checked_at": "", "detail": ""}

    if kind == "SOAP":
        bo_class = conn.get("bo_class") or ""
        ops: list[dict] = []
        if bo_class:
            try:
                raw = iris_connector.class_method_value("demo.WSDLImporter", "GetMessageMap", bo_class)
                for item in json.loads(raw or "[]"):
                    ops.append({"name": item.get("method"), "kind": _op_kind(item.get("method")),
                                "request_message": item.get("requestClass")})
            except Exception as exc:  # noqa: BLE001
                logger.warning("SOAP MessageMap 解析失败 %s: %s", bo_class, exc)
        write_ops = [o for o in ops if o.get("kind") == "write"]
        caps["operations"] = ops
        caps["write_supported"] = bool(write_ops)
        delivery = {"mechanism": "soap_operation",
                    "target": (write_ops[0]["name"] if write_ops else conn.get("service") or "default")}
        if not ops:
            delivery["note"] = "未解析到写入操作（请确认 WSDL 已导入生成 BO）"
        elif not write_ops:
            delivery["note"] = "目标仅查询型（Get/Query），不建议作为数据管道写入目标"
        health = _http_reachable(conn.get("endpoint") or "")
        health["checked_at"] = datetime.datetime.now().isoformat()
    elif kind == "DB":
        try:
            conn0 = tg.get("connection") or {}
            if conn0.get("jdbc_url"):
                res = jdbc_client.test_connection(conn0)
                health = {"ok": bool(res.get("ok")), "detail": (res.get("message") or "")[:200]}
            else:
                rows = iris_connector.query("SELECT 1")
                health = {"ok": bool(rows), "detail": "localTarget OK"}
        except Exception as exc:  # noqa: BLE001
            health = {"ok": False, "detail": str(exc)[:200]}
        health["checked_at"] = datetime.datetime.now().isoformat()

    return {**base, "capabilities": caps, "delivery": delivery, "assets": assets, "health": health}

