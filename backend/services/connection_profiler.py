"""连接探查 Agent（Connection Profiler）：探测数据源/数据目标并产出运行契约。

对已登记的数据源/目标做真实探测，输出/刷新其 runtime（Connection Contract，见
docs/ConnectionContract-设计.md），成为前端 / AI 上下文 / 管道生成 / 自动验证的单一参数来源：

- FHIR 源：GET metadata → 版本 / 认证 / 分页 / `_lastUpdated` 增量能力；资产=已发现资源真实字段
  （字段发现 = 拉 `?_count=10` 样例资源递归合并路径，**覆盖全部已发现资源类型**；
  采到的字段回写资产记录 `^demo.DataAsset` / `^demo.SourceAsset`，供 AI 映射 / UI / C1 使用。
  ⚠ 空库 / 某类型无数据 → 该类型字段为空，只能靠 `StructureDefinition` 兜底；
  本服务器无 core StructureDefinition（`StructureDefinition?url=...` 恒 total=0），故**演 FHIR 源前必须先造数**）
- SQL 源：JDBC 连通 + 已选表列 → 轮询增量键（key_field）
- DB 目标：连通 / 表存在（UPSERT 语义）
- SOAP 目标：WSDL 导入 + BO MessageMap → 操作语义判定（Add/Create→写入型，Get/Query→查询型）
                  + endpoint 可达性
"""

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from backend.services import fhir_client, iris_connector, jdbc_client, repository

logger = logging.getLogger(__name__)

# ---------------- FHIR 源字段采样（样例资源发现字段）----------------
# 背景：原实现硬编码只对 Patient / Observation 采样（第一代 FHIR→DB demo 的两个核心资源），
# 其它类型（Encounter / Condition / MedicationRequest…）字段**永远为空** → AI 映射缺源字段、
# 采到的字段也只留在 runtime，未回写资产。现：范围=全部已发现资源类型 + 回写资产记录。
_CORE_SAMPLE_TYPES = ("Patient", "Observation")  # 优先级最高（demo 核心资源）
# 单次分析最多采样多少个资源类型（0=不限）；145 类全采样会让 analyze 变慢，故设上限
FHIR_SAMPLE_MAX_TYPES = int(os.environ.get("FHIR_SAMPLE_MAX_TYPES", "40"))
# 采样总耗时预算（秒）：超出即停止（避免个别类型的慢查询拖垮整个分析）
FHIR_SAMPLE_BUDGET_SECONDS = float(os.environ.get("FHIR_SAMPLE_BUDGET", "30"))


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
            elif isinstance(obj, (str, int, float, bool)) or obj is None:
                # 数组里的**标量元素**（如 name[0].given[0] / address[0].line[0]）：
                # 原实现只处理 dict/list，标量元素被静默丢弃 → Patient 资产看不到 given/line
                if prefix:
                    fields.append(prefix)

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


def _fhir_sample_order(fhir_assets: list[dict], max_types: int,
                       re_sample_types: set[str] | None = None) -> tuple[list[dict], int]:
    """给出待采样的 FHIR 资产顺序（字段为空，或字段是"暂定"来源需重新采样），返回 (计划, 超上限跳过数)。

    顺序：核心资源（Patient/Observation）→ 平台已建模的 US Core 资源 → 其余按名称；
    参数:
        fhir_assets: 数据源 runtime 的资产列表（{name, type, fields}）。
        max_types: 单次采样的类型上限（<=0 表示不限）。
        re_sample_types: **需重新采样**的类型集合 —— 上一轮字段来自规范快照 `spec_model` /
            AI 规范推断 `ai_spec` 的"暂定字段"：一旦库里出现真实数据，应改用真实数据形态
            （数据优先，长期成立）。为空集 = 只采样字段为空的类型（旧行为）。
    返回:
        (plan, skipped_by_cap)
    """
    _re = {str(t).lower() for t in (re_sample_types or set())}
    candidates = [a for a in (fhir_assets or [])
                  if str(a.get("name") or "").strip()
                  and (not a.get("fields") or str(a.get("name")).lower() in _re)]
    if not candidates:
        return [], 0
    try:  # 平台已建模资源（US Core 11 类）；惰性导入避免循环依赖
        from backend.services import fhir_target_model as _ftm
        modeled = set(_ftm.DEFAULT_RESOURCE_TYPES)
    except Exception:  # noqa: BLE001 - 模型清单缺失不影响采样（退化为按名称排序）
        modeled = set()

    def _rank(name: str) -> tuple:
        if name in _CORE_SAMPLE_TYPES:
            return (0, _CORE_SAMPLE_TYPES.index(name), "")
        if name in modeled:
            return (1, 0, name.lower())
        return (2, 0, name.lower())

    ordered = sorted(candidates, key=lambda a: _rank(str(a.get("name") or "")))
    if max_types and max_types > 0 and len(ordered) > max_types:
        return ordered[:max_types], len(ordered) - max_types
    return ordered, 0


def _fhir_spec_model_fields(resource_type: str) -> list[str]:
    """**平台规范快照**兜底：已建模资源从 `fhir_target_model` 取标准元素路径。

    FHIR 是标准 —— 库里没有数据也能知道资源有哪些元素。优先级：真实样例（数据形态）>
    服务器 StructureDefinition（若有）> **平台内置 R4/US Core 模型**（本函数）。
    未建模类型返回 []（调用方转 AI 按 R4 规范推断）。
    """
    try:
        from backend.services import fhir_target_model as _ftm
        return _ftm.source_field_paths(resource_type)
    except Exception as exc:  # noqa: BLE001 - 模型缺失不影响其它兜底路径
        logger.warning("FHIR 规范快照字段读取失败 %s: %s", resource_type, exc)
        return []


def backfill_fhir_fields(source_id: str, mapping: dict) -> int:
    """把 FHIR 资产字段回写资产记录（公开包装，供路由在 AI 规范补全后复用）。"""
    return _fhir_backfill_fields(source_id, mapping)


def _fhir_backfill_fields(source_id: str, mapping: dict) -> int:
    """把采样到的 FHIR 资产真实字段回写资产记录，返回实际回写条数。

    按**资产 id** 精确写入（`repository.update_asset` 会同步 `^demo.DataAsset` 与
    `^demo.SourceAsset`），且只处理该数据源名下的资产 —— 不触碰其它数据源（尤其 SQL 数据源）：
    SQL 资产的 fields=列名是 C1 补齐映射与管道生成（`_resolve_sql_source_tables`）的输入，不可被清空。
    参数:
        source_id: 数据源 id（为空则不写，防御性）。
        mapping: {资源类型: 字段路径列表}。
    返回:
        实际回写的资产条数。
    """
    if not source_id or not mapping:
        return 0
    written = 0
    try:
        for asset in repository.list_assets(source_id):
            fields = mapping.get(str(asset.get("name") or ""))
            if not fields or asset.get("fields") == fields:
                continue
            repository.update_asset(asset.get("id") or "", {"fields": list(fields)})
            written += 1
    except Exception as exc:  # noqa: BLE001 - 回写失败不影响本次契约探查
        logger.warning("FHIR 资产字段回写失败（不影响本次分析）: %s", exc)
    return written


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
                # 回环归一：前端登记的 endpoint 常是浏览器视角的 localhost/127.0.0.1，
                # backend 在独立容器里连不上（Connection refused）→ 探测统一走内部地址，
                # 与 profile_target 同口径；对外保存的仍是用户登记值（见 backend/config.to_internal_url）。
                from backend.config import to_internal_url
                _probe = to_internal_url(ep)
                fcaps, fpoll = _fhir_capabilities(_probe, conn.get("username"), conn.get("password"))
                caps.update(fcaps)
                poll.update(fpoll)
                health = {"ok": True,
                          "checked_at": datetime.datetime.now().isoformat(),
                          "detail": f"metadata OK（fhir {caps.get('fhir_version', '')}）"}
                # FHIR 资产真实字段：以样例资源（?_count=10）递归合并可访问路径发现字段。
                # 范围=**全部已发现资源类型**（原实现硬编码仅 Patient/Observation → 其它类型字段永远为空）；
                # 顺序=核心资源 → 已建模资源 → 其它；受类型上限（FHIR_SAMPLE_MAX_TYPES）与
                # 总耗时预算（FHIR_SAMPLE_BUDGET）约束（145 类全采样会让分析明显变慢）。
                # **无数据也能知道字段（FHIR 是标准）**：三层兜底
                #   ① 真实样例（数据形态，最准）
                #   ② 服务器 StructureDefinition（本环境实测 total=0 → 探一次即知不可用）
                #   ③ **平台规范快照** `fhir_target_model`（US Core 11 类，元素路径=转换引擎访问路径）
                #   ④ 仍未命中（未建模类型）→ 记入 `pending_ai_types`，由接口分析 Agent 按 R4 规范补
                _prev_prov = ((base.get("note") or {}).get("fields") or {}).get("provenance") or {}
                _re_sample = {t for t, k in _prev_prov.items() if k in ("spec_model", "ai_spec")}
                _plan, _skipped_cap = _fhir_sample_order(assets, FHIR_SAMPLE_MAX_TYPES, _re_sample)
                _deadline = time.monotonic() + FHIR_SAMPLE_BUDGET_SECONDS
                _found: dict = {}
                _empty: list = []
                _provenance: dict = {}
                _spec_model: list = []
                _pending_ai: list = []
                _sd_usable = None
                _skipped_budget = 0
                for _idx, _a in enumerate(_plan):
                    if time.monotonic() > _deadline:
                        _skipped_budget = len(_plan) - _idx
                        break
                    _rt = str(_a.get("name") or "")
                    _f = _fhir_sample_fields(_probe, conn.get("username"), conn.get("password"), _rt)
                    _src_kind = "sampled"
                    if not _f:
                        if _sd_usable is None:
                            _f = _fhir_resource_fields(_probe, conn.get("username"), conn.get("password"), _rt)
                            _sd_usable = bool(_f)
                        elif _sd_usable:
                            _f = _fhir_resource_fields(_probe, conn.get("username"), conn.get("password"), _rt)
                        if _f:
                            _src_kind = "structure_definition"
                    if not _f:
                        # 规范快照兜底：已建模资源的标准元素路径（无需任何数据）
                        _f = _fhir_spec_model_fields(_rt)
                        if _f:
                            _src_kind = "spec_model"
                            _spec_model.append(_rt)
                    if _f:
                        _a["fields"] = _f
                        _found[_rt] = _f
                        _provenance[_rt] = _src_kind
                    else:
                        _empty.append(_rt)
                        _pending_ai.append(_rt)   # 未建模且无数据 → 待 AI 按 R4 规范补
                # 采到的字段回写资产记录（^demo.DataAsset / ^demo.SourceAsset，按资产 id 精确写入）
                _backfilled = _fhir_backfill_fields(ds.get("id") or "", _found)
                base.setdefault("note", {})["fields"] = {
                    "found": len(_found), "found_types": sorted(_found),
                    "empty_types": sorted(_empty),
                    "skipped_cap": _skipped_cap, "skipped_budget": _skipped_budget,
                    "backfilled": _backfilled, "structure_definition": bool(_sd_usable),
                    # 字段来源审计（数据优先；规范兜底；AI 补全由路由层写入 ai_spec_types）
                    "provenance": _provenance,
                    "sampled_types": sorted(t for t, k in _provenance.items() if k == "sampled"),
                    "spec_model_types": sorted(_spec_model),
                    "pending_ai_types": sorted(_pending_ai),
                    "ai_spec_types": [],
                }
                logger.info("FHIR 字段发现[%s]：有字段 %d 类（采样 %d / 规范快照 %d / StructureDefinition %d），"
                            "仍空 %d 类（待 AI 按 R4 规范补），超类型上限跳过 %d，超耗时预算跳过 %d，回写资产 %d",
                            ds.get("id") or "", len(_found),
                            sum(1 for k in _provenance.values() if k == "sampled"),
                            sum(1 for k in _provenance.values() if k == "spec_model"),
                            sum(1 for k in _provenance.values() if k == "structure_definition"),
                            len(_empty), _skipped_cap, _skipped_budget, _backfilled)
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
    elif kind == "FHIR":
        # FHIR REST 目标：metadata 能力刷新 + 投递语义（事务 Bundle）+ 候选资源事实
        # （实测缺陷：FHIR 目标行原先无 runtime/direction，UI「运行契约/投递语义」列空白）
        ep = conn.get("base_url") or conn.get("endpoint") or ""
        if not ep:
            health = {"ok": False, "detail": "缺少 base_url",
                      "checked_at": datetime.datetime.now().isoformat()}
        else:
            try:
                from backend.config import to_internal_url
                fcaps, _fpoll = _fhir_capabilities(to_internal_url(ep),
                                                   conn.get("username"), conn.get("password"))
                caps.update(fcaps)
                health = {"ok": True, "checked_at": datetime.datetime.now().isoformat(),
                          "detail": f"FHIR metadata OK（fhir {caps.get('fhir_version', '')}）"}
            except Exception as exc:  # noqa: BLE001
                health = {"ok": False, "checked_at": datetime.datetime.now().isoformat(),
                          "detail": str(exc)[:200]}
        _rt_names = [a.get("name") for a in assets if a.get("name")]
        _cand = dict(base.get("candidates") or {})
        _cand.setdefault("count", len(_rt_names))
        _cand.setdefault("resource_types", _rt_names)
        _cand["reachable"] = bool(health.get("ok"))
        base["candidates"] = _cand
        # FHIR 目标的候选资源事实在 runtime.candidates（注册/刷新时由 _discover_fhir_entities 写入），
        # runtime.assets 通常为空 → 说明文案与 resources 用 candidates（实测：原先写"0 类候选资源"）
        _cand_types = [str(t) for t in (_cand.get("resource_types") or []) if str(t)]
        _listed = (_cand_types or _rt_names)
        delivery = {
            "mechanism": "fhir_transaction",
            "target": "Bundle(transaction) → FHIR REST",
            "resources": _listed[:20],
            "note": (f"FHIR 目标走标准 REST：以 transaction Bundle（PUT/POST + application/fhir+json）"
                     f"投递（候选资源 {_cand.get('count', len(_listed))} 类，"
                     f"其中已建模 {_cand.get('modeled_count', 0)} 类；"
                     f"实际映射由 AI 匹配决定）"
                     + ("" if health.get("ok") else "；⚠ 端点当前不可达，生成前请确认 base_url")),
        }
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

