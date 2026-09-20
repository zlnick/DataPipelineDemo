# -*- coding: utf-8 -*-
"""术语映射**生成期盘点**（只读：不写任何缓存/配置，不调 LLM 判定）。

架构（2026-09-18 最终口径，用户确认）：
  · **术语服务器是唯一事实源**（`Terminology.Mapping.CodeMap`）；
  · **运行期**：管道的 BP 经**共享 BO** `demo.TerminologyOperation` 直查服务器（零本地码表副本）；
  · **生成期（本模块）**：只做**盘点**——把 `term_map:<skill>` 涉及到的**源表实际编码值**批量送
    `/mapping/availability`，产出 ``covered / negative / missing / pending / unresolved`` 与
    **待办清单**（缺哪些码）；
  · **默认放行**：缺映射**不再中止生成**（真实世界形态：数据先落地，术语缺口走"待办 + AI 补录"治理），
    运行期由共享 BO 命中 `missing` 时**默认降级**（保留源编码 + `meta.tag: unmapped`，不静默）；
    需要"必须先齐备才允许生成"的合规场景，可显式开启**严格模式**（`strict_terms=true` → 仍返回
    `TERM_MAP_INCOMPLETE` 400）。
  · 补录仍由 LLM 判定 Agent 产出（`tools/term_map_build.py` → 写回服务器），**平台不写死码表**。

本模块**不**做：写缓存、镜像、暂停/恢复组件（旧 `term_cache.build_term_cache_safe` 已随本地缓存机制下线）。
"""
import json
import logging

import iris.dbapi

from backend.services import iris_connector, term_catalog
from backend.services.transform_directives import TERM_SKILLS, term_skill

logger = logging.getLogger(__name__)

# 源数据源默认命名空间（无 jdbc_url / 反查不到时最后兜底；演示默认 SQL 源 = USER）
DEFAULT_SOURCE_NS = "USER"
_SQL_SCHEMA = "SQLUser"
# 显示名列启发式（给补录工具做 LLM 查询词；取不到就用编码本身）
_NAME_HINTS = ("name", "text", "display", "description", "title")


def _close(conn) -> None:
    """显式关闭连接（立即释放许可单元）；关闭失败不抛错。"""
    try:
        if conn is not None:
            conn.close()
    except Exception:  # noqa: BLE001 - 连接可能已失效
        pass


def _iter_demo_mappings() -> list[dict]:
    """读取 ^demo.Mapping 全部映射（连接用后即关：IRIS 许可按连接计数）。"""
    import iris

    conn = iris_connector.get_connection()
    try:
        native = iris.createIRIS(conn)
        out = []
        for _, raw in native.iterator("^demo.Mapping"):
            if raw:
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    continue
        return out
    finally:
        _close(conn)


def _split_source(src) -> tuple[str, str, bool]:
    """拆 source 表达式 → (源表, 源列, 是否可解析)。兼容 `表.列` / `schema.表.列` / FHIR 路径。

    FHIR 路径（含 `[`）或函数表达式返回 ok=False —— 盘点据此报"无法枚举编码值"（不静默跳过）。
    """
    text = str(src or "").strip()
    if not text or "(" in text or "[" in text:
        return "", "", False
    parts = [p for p in text.split(".") if p]
    if len(parts) < 2:
        return "", "", False
    return parts[-2], parts[-1], True


def _jobs_from_mappings(mappings: list[dict] | None) -> dict[tuple[str, str], dict]:
    """从映射里提取需要术语转换的 (源表, skill) 任务。

    返回 ``{(表名, skill): {"table","skill","column","source_id","unresolved":[...]}}``；
    键是二元组 —— 调用方（`routes/pipelines.py`）用 ``f"{t}:{s}"`` 展示期望任务。
    """
    jobs: dict[tuple[str, str], dict] = {}
    for m in mappings or []:
        src_id = str(m.get("source_id") or "")
        for fm in (m.get("field_mappings") or []):
            tr = str(fm.get("transform") or "")
            if not tr.startswith("term_map:"):
                continue
            skill = tr[len("term_map:"):].strip()
            if not term_skill(skill):
                continue
            key0 = ("", skill)
            table, column, ok = _split_source(fm.get("source"))
            if not ok:
                job = jobs.setdefault(key0, {"table": "", "skill": skill, "column": "",
                                             "source_id": src_id, "unresolved": []})
                job["unresolved"].append(str(fm.get("source") or "?"))
                continue
            key = (table, skill)
            job = jobs.setdefault(key, {"table": table, "skill": skill,
                                        "column": column, "source_id": src_id,
                                        "unresolved": []})
            if not job.get("column"):
                job["column"] = column
            if not job.get("source_id") and src_id:
                job["source_id"] = src_id
    return jobs


def _skills_of(jobs: dict[tuple[str, str], dict]) -> list[str]:
    """本次涉及的 skill（按登记顺序，输出稳定）。"""
    used = {s for (_t, s) in jobs}
    return [s for s in TERM_SKILLS if s in used]


def _namespace_for_source(source_id: str, table: str) -> tuple[str, str]:
    """反查源数据源命名空间（= DSN 名；IRIS 的 JDBC/SQL 连接按命名空间）。

    返回 ``(namespace, note)``：note 非空表示退了默认命名空间（**显式提示，不静默**）。
    """
    try:
        from backend.services import jdbc_dsn, repository
    except Exception as exc:  # noqa: BLE001 - 服务导入失败不应让盘点崩
        return DEFAULT_SOURCE_NS, f"无法导入 repository/jdbc_dsn（{exc}），按默认命名空间读取"

    ds = None
    if source_id:
        try:
            ds = repository.get_datasource(source_id)
        except Exception as exc:  # noqa: BLE001 - 读取失败退化到按表名反查
            logger.warning("读取数据源 %s 失败: %s", source_id, exc)
    note = ""
    if ds is None and table:
        try:
            for cand in repository.list_datasources():
                if str(cand.get("type") or "").upper() not in ("SQL", "DB"):
                    continue
                names = [str(a.get("name") or "") for a in repository.list_assets(cand.get("id") or "")]
                if any(n and (n == table or n.split(".")[-1] == table) for n in names):
                    ds = cand
                    note = f"按源表 {table} 反查到数据源 {cand.get('id')}"
                    break
        except Exception as exc:  # noqa: BLE001 - 反查失败按默认
            logger.warning("按表名反查数据源失败: %s", exc)
    if ds is None:
        return DEFAULT_SOURCE_NS, f"未找到 {table or '(未知表)'} 所属数据源，按默认命名空间 {DEFAULT_SOURCE_NS} 读取"

    jdbc = ""
    try:
        jdbc = jdbc_dsn.jdbc_url_of(ds) or ""
    except Exception as exc:  # noqa: BLE001 - 无 jdbc_url 时按默认
        logger.warning("读取数据源 %s 的 jdbc_url 失败: %s", ds.get("id"), exc)
    if jdbc:
        try:
            ns = jdbc_dsn.namespace_of(jdbc) or ""
        except Exception:  # noqa: BLE001 - URL 形态异常按默认
            ns = ""
        if ns:
            return ns, note
    runtime = (ds.get("runtime") or {}).get("connection") or {}
    if runtime.get("dsn"):
        return str(runtime["dsn"]), note
    return DEFAULT_SOURCE_NS, f"{note}；数据源 {ds.get('id')} 无 jdbc_url/DSN，按默认命名空间 {DEFAULT_SOURCE_NS} 读取"


def _open_namespace(ns: str):
    """打开指定命名空间的 DB-API 连接（调用方必须 `_close`）。"""
    from backend.config import IRISConfig

    return iris.dbapi.connect(hostname=IRISConfig.HOST, port=IRISConfig.PORT, namespace=ns,
                              username=IRISConfig.USERNAME, password=IRISConfig.PASSWORD)


def _name_column(conn, table: str) -> str:
    """按 information_schema 猜显示名列（供补录工具给 LLM 查询词；取不到返回空）。"""
    cur = conn.cursor()
    try:
        cur.execute("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
                    (_SQL_SCHEMA, table))
        cols = [str(r[0]) for r in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001 - 元数据读取失败视为无显示名列
        logger.warning("读取表 %s 列清单失败: %s", table, exc)
        return ""
    finally:
        try:
            cur.close()
        except Exception:  # noqa: BLE001 - 关闭游标失败可忽略
            pass
    for c in cols:
        low = c.lower()
        if any(low.endswith(h) for h in _NAME_HINTS):
            return c
    return ""


def _distinct_codes(conn, table: str, code_col: str, name_col: str = "") -> list[tuple[str, str]]:
    """读某表编码列的 DISTINCT 值（**原样大小写**：与运行期查键/送回服务器的值一致）。

    ⚠ 必须 `%EXACT(...)`：IRIS 默认排序规则（%SQLUPPER）下 `SELECT DISTINCT Code` 返回**规范化值**
    （`I10.x09` → `I10.X09`），会让"实际存在"的码被判成另一形态（实测 2026-09-17）。
    """
    cur = conn.cursor()
    try:
        if name_col:
            cur.execute(f"SELECT DISTINCT %EXACT({code_col}), %EXACT({name_col}) FROM "
                        f"{_SQL_SCHEMA}.{table} WHERE {code_col} IS NOT NULL")
            rows = [(str(r[0]), str(r[1] or "")) for r in cur.fetchall() if r[0]]
        else:
            cur.execute(f"SELECT DISTINCT %EXACT({code_col}) FROM {_SQL_SCHEMA}.{table} "
                        f"WHERE {code_col} IS NOT NULL")
            rows = [(str(r[0]), "") for r in cur.fetchall() if r[0]]
    finally:
        try:
            cur.close()
        except Exception:  # noqa: BLE001 - 关闭游标失败可忽略
            pass
    return rows




def read_source_codes(jobs: dict[tuple[str, str], dict]) -> dict:
    """按 job 读源表编码值：``{("表", skill): [(code, name), …]}`` + 读取说明。

    连接纪律（IRIS 社区版仅 8 个许可单元，与 Ens 业务主机共用）：**按命名空间聚合，每库一次连接**，
    读完即关（用后即关，不常驻占单元）。
    """
    out: dict[tuple[str, str], list[tuple[str, str]]] = {}
    notes: list[str] = []
    by_ns: dict[str, list[tuple[str, str]]] = {}
    for key, job in jobs.items():
        if not job.get("table"):
            continue
        ns, note = _namespace_for_source(str(job.get("source_id") or ""), job["table"])
        if note:
            notes.append(note)
        by_ns.setdefault(ns, []).append(key)
    for ns, keys in by_ns.items():
        conn = _open_namespace(ns)
        try:
            for key in keys:
                job = jobs[key]
                name_col = _name_column(conn, job["table"])
                out[key] = _distinct_codes(conn, job["table"], job["column"] or "ID", name_col)
        finally:
            _close(conn)
    return {"codes": out, "namespaces": {k: v for k, v in by_ns.items()}, "notes": notes}


def _availability_of(skill: str, codes: list[str]) -> dict:
    """调服务器 availability（按 skill 登记的体系对）。"""
    reg = term_skill(skill) or {}
    return term_catalog.availability(str(reg.get("source_system") or ""),
                                     str(reg.get("target_system") or ""), codes)



def precheck(mappings: list[dict] | None = None) -> dict:
    """**生成期盘点**（只读）：查术语服务器判断这些源编码能否转换。

    返回::

        {"ok", "blocking", "missing": ["<skill>:<码>", …], "pending": [...], "todo": [...],
         "covered", "negative", "unresolved": [...], "jobs": ["<表>:<skill>", …],
         "pairs": [{skill,sourceSystem,targetSystem,available,entries,active,…}],
         "items": {skill: {源编码: 服务器映射行}}, "sources": {...},
         "namespaces": {...}, "notes": [...], "catalogOk", "error", "skipped"}

    - ``blocking`` = missing + pending + unresolved；**是否中止由调用方决定**（默认放行 / 严格模式中止）；
    - ``todo`` = 待办清单（= missing，供 UI/报告展示"还差哪些码"；补录命令见 `hint()`）；
    - **不做判定、不写缓存**：missing 只是"服务器还没有这条映射"，补录走 `tools/term_map_build.py`。
    """
    if mappings is None:
        mappings = _iter_demo_mappings()
    jobs = _jobs_from_mappings(mappings)
    base = {"ok": True, "blocking": 0, "missing": [], "pending": [], "covered": 0, "negative": 0,
            "unresolved": [], "todo": [],
            "jobs": sorted(f"{t or '(未知表)'}:{s}" for (t, s) in jobs),
            "pairs": [], "items": {}, "sources": {}, "namespaces": {}, "notes": [],
            "catalogOk": True, "error": "", "skipped": ""}
    if not jobs:
        base["skipped"] = "无 term_map 术语映射指令"
        return base

    skills = _skills_of(jobs)
    base["pairs"] = [term_catalog.pair_info(s) for s in skills]
    base["catalogOk"] = all(bool(p.get("catalogOk")) for p in base["pairs"])

    unresolved: list[str] = []
    for job in jobs.values():
        for src in job.get("unresolved") or []:
            unresolved.append(f"{job['skill']}:{src}")
    base["unresolved"] = sorted(set(unresolved))

    read = read_source_codes(jobs)
    base["namespaces"] = read.get("namespaces") or {}
    base["notes"] = read.get("notes") or []
    codes_by_job = read.get("codes") or {}

    missing: list[str] = []
    pending: list[str] = []
    items: dict[str, dict] = {}
    sources: dict[str, dict] = {}
    covered = negative = 0
    for skill in skills:
        codes: list[str] = []
        for (table, s), rows in codes_by_job.items():
            if s != skill:
                continue
            for code, name in rows:
                codes.append(code)
                sources.setdefault(skill, {})[code] = {"table": table, "name": name}
        if not codes:
            continue
        try:
            availability = _availability_of(skill, codes)
        except term_catalog.TermCatalogError as exc:
            base["ok"] = False
            base["error"] = str(exc)
            base["blocking"] = int(base["blocking"]) + 1
            return base
        covered += int(availability.get("covered") or 0)
        negative += int(availability.get("negative") or 0)
        for code in availability.get("missingCodes") or []:
            missing.append(f"{skill}:{code}")
        for code in availability.get("pendingCodes") or []:
            pending.append(f"{skill}:{code}")
        items[skill] = dict(availability.get("items") or {})

    base["covered"] = covered
    base["negative"] = negative
    base["missing"] = sorted(missing)
    base["pending"] = sorted(pending)
    base["todo"] = sorted(set(base["missing"]))
    base["items"] = items
    base["sources"] = sources
    base["blocking"] = len(base["missing"]) + len(base["pending"]) + len(base["unresolved"])
    if not base["catalogOk"] and not base["pairs"]:
        base["ok"] = False
        base["error"] = "术语服务器目录不可用，无法确认术语映射覆盖面"
    elif base["unresolved"]:
        base["ok"] = False
        base["error"] = ("存在无法枚举编码值的 term_map 源（"
                         + "、".join(base["unresolved"][:5]) + "）：盘点需要 SQL 列形态的 source")
    else:
        base["ok"] = base["blocking"] == 0
    return base


def hint() -> str:
    """补录指引（缺映射时的**可执行**下一步；AI 判定仍归判定 Agent，平台不写死码表）。"""
    return ("补录方式：python3 tools/term_map_build.py（由判定 Agent 产出候选并写回术语服务器）；"
            "补录后**无需重新生成**——运行期由共享 BO 实时查询生效")


def degrade_message(gate: dict) -> str:
    """**默认降级**口径的说明（生成期不中止；运行期缺映射保留源编码 + meta.tag 标记）。"""
    if not gate or gate.get("skipped"):
        return ""
    todo = list(gate.get("todo") or gate.get("missing") or [])
    parts = []
    if todo:
        parts.append(f"术语服务器尚无 {len(todo)} 个源编码的转换映射（**默认降级放行**）："
                     + "、".join(todo[:8]) + ("…" if len(todo) > 8 else ""))
    if gate.get("unresolved"):
        parts.append("无法枚举编码值的 term_map 源：" + "、".join(gate["unresolved"][:5]))
    if gate.get("error"):
        parts.append(f"盘点异常：{gate['error']}")
    if not parts:
        return ""
    parts.append("这些编码在目标资源中保留源体系 coding 并打 `meta.tag=unmapped`（不静默、不阻断）；"
                 + hint())
    return "；".join(parts)


def gate_message(gate: dict) -> str:
    """**严格模式**（`strict_terms=true`）下的显式失败说明（缺哪些码、怎么补）。"""
    if not gate or gate.get("skipped"):
        return ""
    if gate.get("error"):
        head = str(gate["error"])
    else:
        head = "术语服务器缺少这些源编码的转换映射"
    parts = [f"术语预检未通过（严格模式）：{head}"]
    if gate.get("missing"):
        parts.append(f"缺映射 {len(gate['missing'])} 项：" + "、".join(gate["missing"][:8])
                     + ("…" if len(gate["missing"]) > 8 else ""))
    if gate.get("pending"):
        parts.append(f"待复核 {len(gate['pending'])} 项：" + "、".join(gate["pending"][:8]))
    if gate.get("unresolved"):
        parts.append("无法枚举编码值的 term_map 源：" + "、".join(gate["unresolved"][:5]))
    parts.append(hint())
    return "；".join(parts)

