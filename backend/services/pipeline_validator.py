"""管道生成验证工具（事实检查用函数，AI 做不了精确事实检查）。

验证原则（与验证-修复 Agent 配合）：
- 事实检查（列名/编译/启动/消息状态）用函数工具；
- 验证策略（该验证什么、错误是否实质、如何修复）由 AI Agent 动态组合；
- 修复分层有界：L1 规则修复 → L2 LLM 重试带错误反馈 ≤2 轮 → L3 回退默认；
- 经验沉淀入 ^demo.ValidationIssue 并在后续 prompt 注入。
"""

import json
import logging
import time

from backend.services import iris_connector, type_registry

logger = logging.getLogger(__name__)

PRODUCTION_NAME = "demo.DataflowProduction"


# 目标类型的同义标记：模型/实体上的 schema 字段是判据（DB 实体 = SQLUser、FHIR = FHIR、SOAP = SOAP）
_TYPE_ALIASES = {
    "DB": {"DB", "SQLUSER", "SQL"},
    "FHIR": {"FHIR"},
    "SOAP": {"SOAP", "WSDL"},
}


def _model_type_token(obj: dict) -> str:
    """取模型/实体/目标的类型标记（schema 优先，其次 type/target_type）；无标记返回 ""。"""
    for k in ("schema", "type", "target_type"):
        v = str(obj.get(k) or "").strip().upper()
        if v:
            return v
    return ""


def _type_matches(obj: dict, want: str) -> bool:
    """声明类型与对象类型是否一致。

    **同名跨类型是常态**（DB 表 `Patient` 与 FHIR 资源 `Patient` 同名），按名匹配列结构时
    必须带上类型，否则 DB 映射会被拿 FHIR 列结构校验（实测致映射被误剔除/误改）。
    对象无类型标记时放行（历史数据兼容）。
    """
    if not want:
        return True
    token = _model_type_token(obj)
    if not token:
        return True
    return token in _TYPE_ALIASES.get(want, {want})


def _get_table_columns(table: str, target_models: list[dict] | None = None,
                       target_type: str | None = None) -> list[str]:
    """查询目标表/实体列名（**类型感知**：同名跨类型不混用）。

    优先从传入的 target_models 或持久化的 Target/TargetInterface 获取列名结构
    （支持 SOAP 实体或未建 SQL 表的目标）；无匹配时降级查 information_schema。

    target_type 非空时：只采纳同类型的模型/实体（DB→SQLUser、FHIR→FHIR、SOAP→SOAP），
    且降级 SQL 查询仅对 DB 类型生效——避免 FHIR/SOAP 目标名与 SQL 表名同名时误取列。
    """
    if not table:
        return []
    want = str(target_type or "").strip().upper()

    def _extract_cols(obj: dict) -> list[str]:
        cols = obj.get("columns") or obj.get("fields") or []
        res = []
        for c in cols:
            if isinstance(c, dict) and c.get("name"):
                res.append(str(c["name"]))
            elif isinstance(c, str) and c:
                res.append(c)
        return res

    # 1. 优先从请求传入的 target_models / targets 匹配
    for tm in target_models or []:
        tm_table = (tm.get("table") or tm.get("name") or tm.get("target_name")
                    or tm.get("entity_name") or "")
        if tm_table != table or not _type_matches(tm, want):
            continue
        cols = _extract_cols(tm)
        if cols:
            return cols

    # 2. 查 Repository 中持久化的 Target / TargetInterface
    try:
        from backend.services import repository
        for tg in repository.list_targets():
            if not _type_matches(tg, want):
                continue
            # SOAP 实体或 DB 表
            for tb in tg.get("tables") or []:
                tb_name = tb.get("table") or tb.get("entity_name") or ""
                if tb_name == table and _type_matches(tb, want):
                    cols = _extract_cols(tb)
                    if cols:
                        return cols
            # 兼容 target 顶层名/服务名
            if tg.get("name") == table or (tg.get("connection") or {}).get("service") == table:
                cols = _extract_cols(tg.get("connection") or {})
                if cols:
                    return cols
    except Exception as exc:  # noqa: BLE001
        logger.debug("从 Repository 获取列名失败: %s", exc)

    # 3. 降级查数据库 information_schema.columns（仅 SQL/DB 目标适用）
    if want and want not in _TYPE_ALIASES["DB"]:
        return []
    try:
        rows = iris_connector.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='SQLUser' AND table_name=? ORDER BY ordinal_position",
            [table])
        return [r[0] for r in rows]
    except Exception:  # noqa: BLE001
        return []


def is_open_fhir_target(table: str, target_models: list[dict] | None = None) -> bool:
    """判断目标表/实体是否为 FHIR 开放候选（服务器支持但平台未建模，字段由 Agent 自定）。"""
    if not table:
        return False

    def _matches(obj: dict) -> bool:
        nm = obj.get("table") or obj.get("name") or obj.get("entity_name") or ""
        if str(nm) != table:
            return False
        if obj.get("open"):
            return True
        # 兼容旧行：FHIR 且无列结构也按开放目标处理
        if (obj.get("schema") == "FHIR" or obj.get("type") == "FHIR"):
            cols = obj.get("columns") or obj.get("fields") or []
            return not cols
        return False

    for tm in target_models or []:
        if _matches(tm):
            return True
    try:
        from backend.services import repository
        for tg in repository.list_targets() or []:
            if (tg.get("type") or "").upper() != "FHIR":
                continue
            for tb in tg.get("tables") or []:
                if _matches(tb):
                    return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取 Repository 判断 FHIR 开放目标失败: %s", exc)
    return False


def check_recommendations(mappings: list[dict], target_models: list[dict] | None = None) -> dict:
    """检查转换关系：结构完整性 + 目标列名存在于目标表/实体。

    说明：FHIR 开放候选（服务器支持、平台未建模）无列结构，其字段映射由 Agent 依
    FHIR R4 规范自定，故只给 warning（不阻塞），不做“目标列不存在”硬校验。

    返回: {"ok": bool, "issues": [{"severity", "item", "message"}], "count": int}
    """
    issues: list[dict] = []
    for m in mappings or []:
        mid = m.get("id", "?")
        table = m.get("target_table", "")
        if not m.get("id"):
            issues.append({"severity": "error", "item": "id", "message": "映射缺少 id"})
        if not m.get("source"):
            issues.append({"severity": "error", "item": mid, "message": "缺少源资产 source"})
        if not table:
            issues.append({"severity": "error", "item": mid, "message": "缺少目标表 target_table"})
            continue
        cols = _get_table_columns(table, target_models, m.get("target_type"))
        open_fhir = is_open_fhir_target(table, target_models)
        if not cols and not open_fhir:
            issues.append({"severity": "error", "item": mid,
                           "message": f"目标表/实体 {table} 不存在或列结构为空"})
            continue
        if not cols and open_fhir:
            issues.append({"severity": "warning", "item": mid,
                           "message": f"目标 {table} 为 FHIR 开放类型（服务器支持、平台未建模），"
                                      f"字段映射不校验列名，由 Agent 依 FHIR R4 规范自定"})
        for fm in m.get("field_mappings", []):
            target = fm.get("target", "")
            if cols and target and target not in cols:
                issues.append({"severity": "error", "item": f"{mid}.{target}",
                               "message": f"目标列 {target} 不存在于目标表/实体 {table}"})
            tr = str(fm.get("transform") or "").strip()
            # constant/concat 指令不依赖 source（常量值写在 transform 内），不报缺少 source
            if not fm.get("source") and not tr.startswith(("constant", "concat(")):
                issues.append({"severity": "warning", "item": f"{mid}.{target}",
                               "message": "字段映射缺少 source 路径"})
    errors = [i for i in issues if i.get("severity") == "error"]
    return {"ok": len(errors) == 0, "issues": issues, "count": len(mappings or [])}


def _path_root(path: str) -> str:
    """取路径首段（不含下标和表名前缀），如 name[0].family → name；PatientTable.ID → ID。"""
    p = (path or "").strip()
    if "." in p and not p.startswith("concat"):
        p = p.split(".", 1)[1]
    return p.split("[")[0].split(".")[0]


def _path_candidates(path: str) -> set[str]:
    """路径的等价写法候选（逐级剥前缀 + 去数组下标），用于与"已知字段清单"容错比对。

    背景（2026-09-16 缺陷 L 的姊妹问题，2026-09-17 实测噪声）：`mapping.source` 是**自由文本**，
    前缀写法不固定 —— FHIR 路径可能是 `Patient.name[0].family`（带资源名前缀）也可能是
    `name[0].family`（资产字段清单里的形态）；SQL 列可能是 `ID` / `Patient.ID` / `SQLUser.Patient.ID`。
    原实现只比对"首段"（`_path_root`）→ 正确的 FHIR 映射被误报「源路径 … 不在资产 … 的已知字段中」
    （5 条噪音告警，会误导用户去"修"本来正确的映射）。现让**任一等价形态命中**即算已知。
    """
    p = (path or "").strip()
    if not p or p.startswith("concat("):
        return set()
    parts = p.split(".")
    out: set[str] = set()
    for i in range(len(parts)):
        cand = ".".join(parts[i:])
        if not cand:
            continue
        out.add(cand.lower())
        out.add(cand.split("[")[0].lower())
        out.add(cand.split(".")[0].split("[")[0].lower())
    return out


def check_source_fields(assets: list[dict], mappings: list[dict]) -> dict:
    """检查转换关系的源字段路径是否在已知资产字段中（warning 级，供 AI 语义判断）。

    说明：FHIR 资源字段远多于资产登记的 key 字段，因此未知路径只标 warning
    （不阻塞），由转换验证 Agent 判断是否语义错配。
    """
    issues: list[dict] = []
    all_known_fields: set[str] = set()
    asset_roots: dict[str, set[str]] = {}
    for a in assets or []:
        fs = set(a.get("fields", []))
        all_known_fields.update({str(f).lower() for f in fs})
        asset_roots[a.get("name", "")] = ({str(f).lower() for f in fs}
                                          | {_path_root(f).lower() for f in fs})

    for m in mappings or []:
        src = m.get("source", "")
        known = asset_roots.get(src, all_known_fields)
        if not known:
            continue  # 资产未登记字段，跳过
        for fm in m.get("field_mappings", []):
            if not isinstance(fm, dict):
                continue
            tr = str(fm.get("transform") or "").strip()
            # 常量/表达式：值不是源列，跳过源列存在性检查（避免把常量值当列名误报）
            if tr.startswith("constant") or tr.startswith("concat("):
                continue
            path = str(fm.get("source") or "")
            if not path:
                continue
            # 函数/常量/已有已知字段均算合法（多候选逐级匹配，见 _path_candidates）
            if path.startswith("concat(") or " " in path:
                continue
            cands = _path_candidates(path)
            if cands & known or cands & all_known_fields:
                continue
            issues.append({"severity": "warning", "item": f"{m.get('id', '?')}.{path}",
                           "message": f"源路径 {path} 不在资产 {src} 的已知字段中"})
    return {"ok": True, "issues": issues}



def check_pipeline_topology(topology: dict | None, source_type: str | None = None,
                            target_type: str | None = None,
                            source_types: list[str] | None = None,
                            target_types: list[str] | None = None) -> dict:
    """检查拓扑：组件枚举合法 + 必选组件齐全 + className 非空。

    单管道传 source_type/target_type；多管道传 source_types/target_types（并集）。
    返回: {"ok": bool, "issues": [...], "components": [type...]}
    """
    issues: list[dict] = []
    components = (topology or {}).get("components", [])
    if not components:
        issues.append({"severity": "error", "item": "components",
                       "message": "拓扑没有组件"})
    allowed = set()
    for st in source_types or ([source_type] if source_type else []):
        for c in type_registry.get_source_components(st):
            allowed.add(c["type"])
    for tt in target_types or ([target_type] if target_type else []):
        for c in type_registry.get_target_components(tt):
            allowed.add(c["type"])
    for c in type_registry.get_common_components():
        allowed.add(c["type"])
    # 设计 Skill 专属组件的生成契约（sql2fhir-patient-tx 等）由 executor 生成，拓扑校验需放行
    for c in type_registry.get_pipeline_asset_components():
        allowed.add(c["type"])
    # SQL 源管道的查询 BO = 现成 SQLOperation(EnsLib.SQL.Operation.GenericOperation) 读语义，
    # 与目标类型无关，SQL 源拓扑需放行
    if source_type == "SQL" or "SQL" in (source_types or []):
        allowed.add("SQLOperation")
    seen: set[str] = set()
    for c in components:
        ctype = c.get("type", "")
        if ctype not in allowed:
            issues.append({"severity": "error", "item": ctype,
                           "message": f"组件类型 {ctype} 不在类型注册表中"})
        if not c.get("className"):
            issues.append({"severity": "error", "item": ctype,
                           "message": f"组件 {ctype} 缺少 className"})
        if ctype == "SQLService":
            query_setting = next((s for s in c.get("settings", []) if s.get("name") == "Query"), None)
            if not query_setting or not query_setting.get("value"):
                issues.append({"severity": "error", "item": "SQLService.Query",
                               "message": "SQLService 缺少 SQL 查询语句 Query"})
        seen.add(ctype)
    # 必选：处理组件（TransformProcess 或 Skill 聚合 BP PatientTxProcess）+ JavaGateway
    process_seen = any(t in seen for t in ("TransformProcess", "PatientTxProcess"))
    if not process_seen:
        issues.append({"severity": "error", "item": "process",
                       "message": "缺少处理组件（TransformProcess 或 PatientTxProcess）"})
    if "JavaGateway" not in seen:
        issues.append({"severity": "error", "item": "JavaGateway",
                       "message": "缺少必选组件 JavaGateway"})
    if not topology or not topology.get("production"):
        issues.append({"severity": "error", "item": "production",
                       "message": "缺少 production 名称"})
    errors = [i for i in issues if i.get("severity") == "error"]
    return {"ok": len(errors) == 0, "issues": issues,
            "components": [c.get("type") for c in components]}


def check_compile(production: str = PRODUCTION_NAME) -> dict:
    """编译状态检查（IRIS 重新编译验证，失败返回错误详情）。

    返回: {"ok": bool, "message": str}
    """
    try:
        data = iris_connector.class_method_value("demo.PipelineQuery", "CheckCompile")
        return json.loads(data or '{"ok": false, "message": "IRIS 调用无返回"}')
    except Exception as exc:  # noqa: BLE001 - 验证函数需捕获所有异常并转报告
        logger.error("编译检查失败: %s", exc)
        return {"ok": False, "message": f"编译检查调用失败: {exc}"}


def check_start(production: str = PRODUCTION_NAME) -> dict:
    """Production 运行状态检查。

    返回: {"ok": bool, "running": bool, "production": str, "message": str}
    """
    try:
        running = bool(iris_connector.class_method_value(
            "demo.PipelineQuery", "ProductionStatus"))
        return {"ok": running, "running": running, "production": production,
                "message": "Production 运行中" if running else "Production 未运行"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "running": False,
                "message": f"启动检查失败: {exc}"}


def wait_for_messages_settle(timeout: int = 60, interval: int = 5,
                             minutes: int = 15) -> dict:
    """等待 Ens 消息处理完成（生成/重启后立即校验会误判为失败）。

    背景：源适配器按 CallInterval 轮询、BP 还要同步查询子表，消息从产生到 Completed
    需要数秒到数十秒。此前生成后**立刻**校验，把"尚未处理完"当成"运行错误"→ 触发
    "自动重建一次"（重建会停 Production 并把在途消息挂起 Status=9）→ 假失败循环，
    表现为"第二条管道生成失败"。

    这里做有界等待：
      - 终态（9=Completed / 4=Discarded / 7=Aborted / 8=Error）不计入 pending；
      - 挂起（5=Suspended）的消息调用 `Ens.MessageHeader.ResendMessage` 重投一次；
      - pending 归零或超时后返回。

    ⚠ 状态码取值（`Ens.DataType.MessageStatus`，实测 LogicalToDisplay）：
      1=Created 2=Queued 3=Delivered 4=Discarded 5=Suspended 6=Deferred
      7=Aborted 8=Error 9=**Completed** —— 早期把 9 当成"挂起"、把 1/2 当终态是错的
      （会把已完成消息当未完成 → 白等 + 误重投）。
    返回 {"settled": bool, "waited": int, "pending": int}
    """
    import datetime as _dt
    import time as _time

    since = _dt.datetime.now() - _dt.timedelta(minutes=max(1, int(minutes)))
    waited = 0
    resent: set[str] = set()
    pending = -1
    _final = "(4, 7, 8, 9)"   # Discarded / Aborted / Error / Completed
    while True:
        try:
            rows = iris_connector.query(
                "SELECT ID, Status FROM Ens.MessageHeader WHERE TimeCreated > ? "
                f"AND SourceConfigName NOT LIKE 'Ens.%' AND Status NOT IN {_final}",
                [since])
        except Exception as exc:  # noqa: BLE001
            logger.warning("消息状态查询失败: %s", exc)
            return {"settled": False, "waited": waited, "pending": -1,
                    "message": str(exc)}
        pending = len(rows or [])
        if pending == 0:
            return {"settled": True, "waited": waited, "pending": 0}
        for r in rows or []:
            if int(r[1]) == 5 and str(r[0]) not in resent:   # 5 = Suspended
                resent.add(str(r[0]))
                try:
                    iris_connector.class_method_value(
                        "Ens.MessageHeader", "ResendMessage", str(r[0]))
                    logger.info("已重投挂起消息 #%s", r[0])
                except Exception as exc:  # noqa: BLE001
                    logger.warning("重投挂起消息 #%s 失败: %s", r[0], exc)
        if waited >= max(0, int(timeout)):
            logger.warning("等待消息处理超时：仍有 %d 条未完成（等待 %ds）", pending, waited)
            return {"settled": False, "waited": waited, "pending": pending}
        _time.sleep(interval)
        waited += interval


def check_smoke(limit: int = 10) -> dict:
    """运行级 smoke test：最近消息流转是否全部 Completed。

    注意区分「运行出错」（Error/Aborted → 判失败）与「尚未处理完」（Queued/InProgress/
    Suspended → 只报 warning，交给 wait_for_messages_settle 等待与重投），
    否则生成后立即校验会把在途消息误判为失败。

    返回: {"ok": bool, "level": "info|warning|error", "message": str, "items": [...]}
    """
    try:
        data = iris_connector.class_method_value("demo.PipelineQuery", "GetLogs", limit)
        items = json.loads(data or "[]")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "level": "error",
                "message": f"消息检查失败: {exc}", "items": []}
    _done = ("Completed", "Discarded")
    _hard = ("Error", "Aborted")
    errors = [i for i in items if str(i.get("status")) in _hard]
    pending = [i for i in items
               if str(i.get("status")) not in _done and str(i.get("status")) not in _hard]
    if errors:
        return {"ok": False, "level": "error",
                "message": f"{len(errors)}/{len(items)} 条消息运行出错",
                "items": errors}
    if pending:
        return {"ok": False, "level": "warning",
                "message": f"{len(pending)}/{len(items)} 条消息尚未完成（仍在队列/处理中）",
                "items": pending}
    if not items:
        return {"ok": True, "level": "warning",
                "message": "尚无消息流转，可调用 /generate-mock 触发增量同步验证"}
    return {"ok": True, "level": "info",
            "message": f"最近 {len(items)} 条消息均 Completed"}


def run_validation(mappings: list[dict], topology: dict | None,
                   source_type: str = "FHIR", target_type: str = "DB",
                   production: str = PRODUCTION_NAME) -> dict:
    """聚合全部事实检查，返回统一报告（供验证-修复 Agent 判断与前端展示）。

    返回: {"ok": bool, "results": {...}, "issues": [...],
           "error_count": int, "warning_count": int}
    """
    results = {
        "recommendations": check_recommendations(mappings),
        "pipeline": check_pipeline_topology(topology, source_type, target_type),
        "compile": check_compile(production),
        "start": check_start(production),
        "smoke": check_smoke(),
    }
    issues: list[dict] = []
    for key, res in results.items():
        for issue in res.get("issues", []):
            issues.append({"check": key, **issue})
        if not res.get("ok") and not res.get("issues"):
            # 编译/启动类检查只有 ok/message，统一视为 error
            if res.get("level") != "warning" and res.get("message"):
                issues.append({"check": key, "severity": "error",
                               "item": key, "message": res.get("message", "")})
    errors = [i for i in issues if i.get("severity") == "error"]
    warnings = [i for i in issues if i.get("severity") == "warning"]
    return {"ok": len(errors) == 0, "results": results, "issues": issues,
            "error_count": len(errors), "warning_count": len(warnings)}


def run_transformation_validation(mappings: list[dict], assets: list[dict] | None = None,
                                target_models: list[dict] | None = None) -> dict:
    """转换关系专项验证（供转换验证-修复 Agent C1 使用）。

    覆盖：映射结构/目标列存在性（error）+ 源字段路径（warning）。
    """
    results = {
        "recommendations": check_recommendations(mappings, target_models),
        "source_fields": check_source_fields(assets or [], mappings),
    }
    issues: list[dict] = []
    for key, res in results.items():
        for issue in res.get("issues", []):
            issues.append({"check": key, **issue})
    errors = [i for i in issues if i.get("severity") == "error"]
    warnings = [i for i in issues if i.get("severity") == "warning"]
    return {"ok": len(errors) == 0, "results": results, "issues": issues,
            "error_count": len(errors), "warning_count": len(warnings)}


def _fhir_base_from_topology(topology: dict | None) -> str:
    """从拓扑的 HTTPOperation 组件推导 FHIR base（供目标效果校验使用）。"""
    comp = next((c for c in (topology or {}).get("components", [])
                 if c.get("className") == "EnsLib.HTTP.GenericOperation"), None)
    if not comp:
        return ""
    host = port = ""
    for s in comp.get("settings", []) or []:
        if s.get("name") == "HTTPServer":
            host = str(s.get("value") or "")
        if s.get("name") == "HTTPPort":
            port = str(s.get("value") or "")
    if not host:
        return ""
    from backend.config import to_internal_url
    return to_internal_url(
        f"http://{host}:{port or '52773'}/csp/healthshare/fhirserver/fhir/r4")


def _pipeline_has_messages(minutes: int = 60) -> int:
    """近 minutes 分钟内**业务**消息条数（排除 Ens 框架心跳；-1 = 查询失败，0 = 尚无业务消息）。

    注意：必须排除 `Ens.*` 框架组件（Ens.ScheduleService/ScheduleHandler 心跳一直存在），
    否则"源表还没有数据/尚未轮询"的场景会被误判为"有消息流转但目标为空"，从而误报生成失败。
    """
    import datetime as _dt
    since = _dt.datetime.now() - _dt.timedelta(minutes=max(1, int(minutes)))
    try:
        rows = iris_connector.query(
            "SELECT COUNT(*) FROM Ens.MessageHeader WHERE TimeCreated > ? "
            "AND SourceConfigName NOT LIKE 'Ens.%'", [since])
        return int(rows[0][0]) if rows else 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("消息流转计数失败: %s", exc)
        return -1


def check_target_effect(target_types: list[str] | None = None,
                        expected: dict[str, int] | None = None,
                        fhir_base: str = "",
                        strict: bool = True,
                        wait_seconds: int = 0) -> dict:
    """目标侧落地效果事实检查（**有界等待版**：防"生成后立即计数"的竞态假失败）。

    2026-09-17 实测（Round 2，SQL 源 → FHIR 组）：生成接口在同一秒内就做落地判定，源适配器刚投出的
    行还在 `BP → HTTPOperation` 途中 → FHIR `Patient` 计数 0 → 判 error「落地数 0 < 预期 1」
    （假失败：生成后数秒复查即 3）。故加**有界等待**：仅当判定失败时以 3s 间隔复判，直到
    成功或超出 `wait_seconds`；`wait_seconds=0`（默认）行为与旧版一致（单次判定）。

    判定本身见 `_evaluate_target_effect`（FHIR 资源计数 / DB 表行数 + 预期对比）。
    """
    import time as _time

    deadline = _time.monotonic() + max(0, int(wait_seconds))
    warned = False
    while True:
        res = _evaluate_target_effect(target_types, expected, fhir_base, strict)
        if res.get("ok") or _time.monotonic() >= deadline:
            return res
        if not warned:
            logger.info("目标落地检查未通过（可能有在途消息），最多等待 %ds 后复判：%s",
                        int(wait_seconds),
                        [i.get("message") for i in (res.get("issues") or [])][:2])
            warned = True
        _time.sleep(3)


def _evaluate_target_effect(target_types: list[str] | None = None,
                            expected: dict[str, int] | None = None,
                            fhir_base: str = "",
                            strict: bool = True) -> dict:
    """目标侧落地效果事实检查（FHIR：资源计数；DB：目标表行数）。

    用于 C2 判断"事务是否真的落库"——避免"消息 Completed 但目标为空"（如 FHIR 200 + OperationOutcome 回滚）。
    expected: {资源类型/表名: 预期行数}；缺省只做"全为 0"的探测。
    strict: True=有业务消息流转时"低于预期"判 **error**；False（如消息尚未 settle）只判 warning。

    实测缺陷：`expected` 未按管道布局传入时，`Patient=3 / Encounter=Condition=MedicationRequest=0`
    会被判"通过"（口径太宽）——调用方应传布局声明的资源集合（见 routes/pipelines.py）。
    """
    import base64
    import json as _json
    import urllib.request

    from backend.config import FHIRConfig
    types = [str(t).upper() for t in (target_types or [])]
    issues: list[dict] = []
    counts: dict[str, int] = {}
    _fhir_handled: set[str] = set()      # 已按 FHIR 资源判定的预期名（防同名 DB 表重复/覆盖判定）
    _flowing = _pipeline_has_messages() > 0
    if "FHIR" in types:
        from backend.config import to_internal_url
        # 兜底地址用 **目标** 仓库（FHIRSERVER）：本函数是"转换结果是否落地"的判定，
        # 不能跟着 FHIRConfig.BASE_URL（演示默认 = 源仓库 DemoFHIR）走，否则会去数源仓库。
        base = to_internal_url(
            fhir_base or FHIRConfig.TARGET_BASE_URL or "").rstrip("/") + "/"
        auth = base64.b64encode(
            f"{FHIRConfig.USERNAME}:{FHIRConfig.PASSWORD}".encode()).decode()
        # 计数集合 = 平台核心资源 ∪ 调用方声明的预期资源（布局驱动，避免"只数四类"漏判）
        _rt_list = list(dict.fromkeys(
            ["Patient", "Encounter", "Condition", "MedicationRequest"]
            + [str(k) for k in (expected or {}).keys() if str(k)]))
        for rt in _rt_list:
            try:
                req = urllib.request.Request(
                    base + rt + "?_summary=count",
                    headers={"Authorization": "Basic " + auth,
                             "Accept": "application/fhir+json"})
                data = _json.load(urllib.request.urlopen(req, timeout=10))
                counts[rt] = int(data.get("total") or 0)
            except Exception as exc:  # noqa: BLE001
                counts[rt] = -1
                issues.append({"severity": "warning", "item": rt,
                               "message": f"FHIR {rt} 计数失败: {exc}"})
        pos = [v for v in counts.values() if v >= 0]
        if pos and all(v == 0 for v in pos):
            if not _flowing:
                # 尚无业务消息流转（如源表为空/未点「生成演示数据」、源适配器尚未首次轮询）：
                # 目标为空属正常，只报 warning 并给出可操作提示，避免误报"生成失败"
                issues.append({"severity": "warning", "item": "FHIR",
                               "message": "目标暂无数据且尚无业务消息流转：请确认源表已生成演示数据"
                                          "（CLINIC 源可调用 /api/datasources/<id>/seed），"
                                          "并等待源适配器首次轮询后再校验"})
            else:
                issues.append({"severity": "error", "item": "FHIR",
                               "message": "FHIR 目标全部资源为 0：事务可能被拒（OperationOutcome）或未投递"})
        for rt, n in (expected or {}).items():
            got = counts.get(rt, -1)
            _fhir_handled.add(str(rt))
            if got >= 0 and got < int(n):
                issues.append({"severity": "error" if (strict and _flowing) else "warning",
                               "item": rt,
                               "message": f"FHIR {rt} 落地数 {got} < 预期 {n}"
                                          + ("（事务可能被拒或部分未投递；若为子资源为 0，"
                                             "优先查聚合 BP 是否真的发起了子表查询）" if _flowing
                                             else "（尚无业务消息流转，仅提示）")})
    if "DB" in types and expected:
        for tbl, n in expected.items():
            # 同名冲突：expected 来自 FHIR 布局时（如 "Patient" 既是 FHIR 资源又是 DB 目标表），
            # 该预期已由 FHIR 分支判定 → 不得再用 DB 表计数覆盖/重复报错（实测会把 FHIR Patient=3 判成 0）
            if str(tbl) in _fhir_handled:
                continue
            try:
                rows = iris_connector.query(f"SELECT COUNT(*) FROM SQLUser.{tbl}")
                got = int(rows[0][0]) if rows else -1
                counts.setdefault(tbl, got)
                if got >= 0 and got < int(n):
                    issues.append({"severity": "error", "item": tbl,
                                   "message": f"目标表 {tbl} 行数 {got} < 预期 {n}"})
            except Exception as exc:  # noqa: BLE001
                issues.append({"severity": "warning", "item": tbl,
                               "message": f"目标表 {tbl} 计数失败: {exc}"})
    errors = [i for i in issues if i.get("severity") == "error"]
    return {"ok": not errors, "issues": issues, "counts": counts}


def _layout_query_bos(bp_names: list[str] | None = None) -> list[str]:
    """读取 sql2fhir 布局声明的查询 BO 名清单（去重）。

    读取顺序（多管道隔离后）：
    ① 拓扑里每个聚合 BP 的**实例级**布局 `^demo.Config("sql2fhir","layout",<BP名>)`；
    ② 全局键 `^demo.Config("sql2fhir","layout")`（单管道口径 / 历史兼容）。
    元素可能是 `{"bo_name": "..."}` 或直接是名字字符串；读不到返回 []（不静默猜测）。
    """
    raws: list[str] = []
    for bp in (bp_names or []):
        try:
            r = iris_connector.global_get("^demo.Config", "sql2fhir", "layout", str(bp))
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取 sql2fhir 实例布局(%s)失败: %s", bp, exc)
            r = None
        if r:
            raws.append(r)
    try:
        g = iris_connector.global_get("^demo.Config", "sql2fhir", "layout")
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取 sql2fhir 全局布局失败: %s", exc)
        g = None
    if g:
        raws.append(g)
    out: list[str] = []
    for raw in raws:
        try:
            layout = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except ValueError:
            continue
        for bo in (layout or {}).get("query_bos") or []:
            nm = bo.get("bo_name") if isinstance(bo, dict) else bo
            if nm and str(nm) not in out:
                out.append(str(nm))
    return out



def _layout_present(bp_names: list[str] | None = None) -> bool:
    """是否存在**可读**的 sql2fhir 布局记录（实例级或全局键，即使 `query_bos` 为空）。

    用途：区分两种"读不到查询 BO"的情形（二者都必须跳过检查，但理由不同、便于排障）：
    ① 布局**不存在**（异常/历史数据）→ 无法区分查询 BO 与 DB/SOAP 目标操作组件；
    ② 布局存在但声明 `query_bos=[]` —— **单表来源**（如只映射 Patient→Patient）按设计
       就没有子表查询，绝不能退化成"把拓扑里唯一的 SQLOperation 当查询 BO"。
    读不到 → False（fail-open）。
    """
    if not bp_names:
        return False
    for bp in bp_names:
        try:
            if iris_connector.global_get("^demo.Config", "sql2fhir", "layout", str(bp)):
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取 sql2fhir 实例布局(%s)失败: %s", bp, exc)
    try:
        return bool(iris_connector.global_get("^demo.Config", "sql2fhir", "layout"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取 sql2fhir 全局布局失败: %s", exc)
        return False


def check_query_dispatch(topology: dict | None = None, wait_seconds: int = 0) -> dict:
    """事实检查：聚合 BP 收到业务消息后，是否**真的向查询 BO 派发过子表查询**。

    背景（实测静默缺陷）：BP 用 `If (tCnt=0)` / `While (i<tCnt)` 比较空值（ObjectScript
    `(""=0)` 为 FALSE）→ 子查询循环一次都不进、Bundle 只含 Patient，而消息全 Completed、
    目标 Patient 有数、既有校验全绿——"零消息派发"是唯一可见线索。本检查看运行期
    `Ens.MessageHeader` 里是否存在 `→ {查询 BO}`（SourceConfigName 非 Ens.*）的派发记录。

    返回: {"ok", "checked", "bp_messages", "missing": [...], "issues": [...]}
    - checked=False：拓扑里没有「查询 BO + 聚合 BP」组合（如 SQL→SOAP/DB 管道），跳过；
    - 聚合 BP 尚无业务消息：只报 info/warning（源适配器可能还没首次轮询），不判失败。
    - wait_seconds>0：先**有界等待首个业务消息**再判定（生成链路传 20s；0=旧行为立即判定），
      否则会漏掉「BP 已有消息却从未派发子表查询」的静默缺陷（实测 2026-09-18）。
    """
    comps = (topology or {}).get("components") or []
    names = {str(c.get("name")) for c in comps if c.get("name")}
    bps = [c.get("name") for c in comps if c.get("type") == "PatientTxProcess" and c.get("name")]
    sqlops = [c.get("name") for c in comps if c.get("type") == "SQLOperation" and c.get("name")]
    if not bps or not sqlops:
        return {"ok": True, "checked": False, "issues": [], "missing": []}
    # 查询 BO 集合 = **sql2fhir 布局声明的 query_bos ∩ 本次拓扑**（布局是唯一事实源）。
    # ⚠ 不能把拓扑里的 SQLOperation 当查询 BO：多管道场景（sql2db + sql2fhir 同 Production）
    #   里 `SQLOp_PatientSource` 是 **DB 目标**操作组件；该组被许可调度停用/无消息时，
    #   旧口径会误报「BP 从未向查询 BO 派发」（并把 DB 目标名塞进 missing）→ 触发一次
    #   无谓的 BP 方法修复。
    # ⚠⚠ 2026-09-18 实测缺陷（本函数旧版）：**单表来源**的 sql2fhir 组（只映射 Patient，
    #   布局 `query_bos=[]`）在拓扑里恰好只有 1 个 SQLOperation（= DB 目标）时，会落进
    #   "单一 SQLOperation 且无布局（历史兼容）"分支 → 把 DB 目标当查询 BO → 误报 error →
    #   回喂 bp_code 修复 → AI 据此**幻觉出 `QueryChild`/`Ens.Util.Log` 逻辑** → 编译失败
    #   `#1054 Invalid expression`，白白烧掉修复轮次。故**删除该推断分支**：
    #   布局读不到/未声明子表查询时一律跳过（fail-open），只报可读原因。
    layout_bos = _layout_query_bos(bps)
    bos = [b for b in layout_bos if b in names]
    if not bos:
        if layout_bos:
            hint = (f"sql2fhir 布局声明的查询 BO {layout_bos} 均不在本次拓扑"
                    "（可能是其它管道的陈旧布局）")
        elif len(sqlops) > 1:
            hint = "拓扑含多个 SQLOperation 且无 sql2fhir 布局声明 → 无法区分查询 BO 与 DB 目标操作组件"
        elif _layout_present(bps):
            hint = ("本组 sql2fhir 布局未声明子表查询（单表来源，如只映射 Patient）"
                    "→ 按设计无子表查询派发")
        else:
            hint = ("无 sql2fhir 布局声明，无法确认唯一的 SQLOperation 是否为查询 BO"
                    "（多管道下它通常是 DB/SOAP 目标操作组件，不能当查询 BO）")
        return {"ok": True, "checked": False, "missing": [], "issues": [
            {"severity": "warning", "item": "query_dispatch",
             "message": hint + " → 跳过子表查询派发检查"}]}
    def _count(target: str) -> int:
        rows = iris_connector.query(
            "SELECT COUNT(*) FROM Ens.MessageHeader WHERE TargetConfigName = ? "
            "AND SourceConfigName NOT LIKE 'Ens.%'", [target])
        return int(rows[0][0]) if rows else 0
    try:
        # 有界等待首个业务消息：生成后源适配器可能尚未首次轮询 → in_bp==0 直接跳过会**漏掉**
        # "BP 从未派发子表查询"这类静默缺陷（实测 2026-09-18：层级记账 off-by-one → Bundle 只有
        # Patient、消息全 Completed、零错误）。给一个明确的等待窗口（默认 0 = 旧行为）。
        deadline = time.monotonic() + max(0, int(wait_seconds or 0))
        while True:
            in_bp = sum(_count(bp) for bp in bps)
            if in_bp or time.monotonic() >= deadline:
                break
            time.sleep(2)
        if in_bp == 0:
            return {"ok": True, "checked": False, "bp_messages": 0, "missing": [],
                    "issues": [{"severity": "warning", "item": "query_dispatch",
                                "message": "聚合 BP 尚无业务消息，跳过子表查询派发检查"}]}
        missing = [bo for bo in bos if _count(bo) == 0]
        issues: list[dict] = []
        if missing:
            issues.append({"severity": "error", "item": ";".join(missing),
                           "message": f"聚合 BP 已收到 {in_bp} 条消息，但从未向查询 BO 派发子表查询："
                                      f"{missing}——Bundle 必然缺子资源（多为 BP 代码缺陷："
                                      "空值比较使循环一次都不进，或子查询错误被静默吞掉）"})
        return {"ok": not missing, "checked": True, "bp_messages": in_bp,
                "missing": missing, "issues": issues}
    except Exception as exc:  # noqa: BLE001 - 检查失败不影响主判定
        return {"ok": True, "checked": False, "issues": [
            {"severity": "warning", "item": "query_dispatch",
             "message": f"子表查询派发检查失败: {str(exc)[:150]}"}]}


def classify_runtime_error(text: str) -> dict:
    """把运行期错误文本分类并给出修复方向（用于回喂对应 Agent）。"""
    t = str(text or "")
    schema_kw = ("OperationOutcome", "MissingRequiredProperty", "MalformedValue", "NotAnArray",
                 "InvalidId", "InvalidJsonContent", "NotAllowed", "UnexpectedObject",
                 "UnexpectedPropertyName", "InvalidResource")
    code_kw = ("SUBSCRIPT", "INVALID OREF", "UNDEFINED", "CLASS DOES NOT EXIST", "MPP5377",
               "QUIT argument", "PROPERTY DOES NOT EXIST", "PYTHON EXCEPTION", "INVALID CLASS",
               "ILLEGAL VALUE", "Invalid command",
               # 实测缺口：对象方法不存在是**运行期**错误（编译期不报），原先未列入 → BP 修复不触发
               "METHOD DOES NOT EXIST", "ErrBPTerminated")
    ref_kw = ("MalformedRelativeReference", "RelativeReference", "InvalidReference")
    if any(k in t for k in ref_kw):
        # 实测 2026-09-18：BP 从 layout **顶层** 读 refs（不存在该键）+ $IsObject 兜底成空数组
        # → 引用注入被静默跳过 → Bundle 里 subject/encounter 仍是裸源键 → FHIR 拒收。
        # 这是 **BP 代码**缺陷（不是映射缺陷）→ 必须回喂 BP 方法修复，而不是改映射。
        return {"kind": "bp_code",
                "advice": "引用注入未生效（BP 未按 layout.bundle.refs 写 urn:uuid: 引用，"
                          "常见于读成顶层 layout.refs 并用空数组兜底）→ method_updates 修复 BP"}
    if any(k in t for k in schema_kw):
        return {"kind": "fhir_schema",
                "advice": "修正字段映射/转换指令（C1 update_mapping）后重新生成"}
    if any(k in t for k in code_kw):
        return {"kind": "bp_code", "advice": "修正 BP 方法（method_updates 增量修复）后重新编译"}
    return {"kind": "other", "advice": "需结合消息/日志进一步定位"}


def collect_runtime_errors(limit: int = 10, minutes: int = 10) -> list[dict]:
    """收集**最近 minutes 分钟**内的运行期错误（Ens 事件日志 Type=Error），含分类建议。"""
    import datetime as _dt
    since = _dt.datetime.now() - _dt.timedelta(minutes=max(1, int(minutes)))
    try:
        rows = iris_connector.query(
            f"SELECT TOP {int(limit)} TimeLogged, ConfigName, Text FROM Ens_Util.Log "
            "WHERE Type='2' AND TimeLogged > ? ORDER BY ID DESC", [since])
    except Exception as exc:  # noqa: BLE001
        logger.warning("运行期错误收集失败: %s", exc)
        return []
    out = []
    for r in rows:
        text = str(r[2] or "")
        out.append({"time": str(r[0]), "config": str(r[1] or ""),
                    "text": text[:400], **classify_runtime_error(text)})
    return out


def run_pipeline_validation(topology: dict | None, source_type: str = "FHIR",
                            target_type: str = "DB",
                            production: str = PRODUCTION_NAME,
                            source_types: list[str] | None = None,
                            target_types: list[str] | None = None,
                            expect_targets: dict[str, int] | None = None,
                            effect_target_types: list[str] | None = None,
                            effect_wait: int = 30) -> dict:
    """数据管道专项验证（供管道验证-修复 Agent C2 使用）。

    覆盖：拓扑完整性 + 编译 + 启动 + 消息流转 + **目标落地效果**（FHIR/DB 计数）
    + **运行期错误分类**（fhir_schema / bp_code，供回喂对应 Agent）。
    expect_targets: {资源类型或表名: 预期数量}（可选，用于判定"是否真的落库"）。
    effect_target_types: 目标落地检查**只看这些类型**（缺省 = target_types）。
        调用方在有分组被许可调度停用时传「未被停用分组的类型」——那些分组组件已生成但未启动，
        目标必然无数据，若纳入判定会把"已生成但停用"误判为失败（实测缺陷：多管道第二条
        管道被调度停用 → 500，用户以为生成失败）。
    effect_wait: 落地判定的**有界等待秒数**（默认 30；0=单次判定）。
        生成后立即计数存在竞态（在途消息尚未落库）→ 实测假失败「FHIR Patient 落地数 0 < 预期 1」。
    多管道场景传 source_types/target_types（并集）。
    """
    # 生成/重启后消息仍在队列或处理中 → 有界等待（并重投挂起消息），避免误判为失败
    _settle = wait_for_messages_settle()
    results = {
        "pipeline": check_pipeline_topology(topology, source_type, target_type,
                                            source_types=source_types,
                                            target_types=target_types),
        "compile": check_compile(production),
        "start": check_start(production),
        "settle": {"ok": bool(_settle.get("settled")),
                   "level": "info" if _settle.get("settled") else "warning",
                   "message": f"等待消息处理 {_settle.get('waited')}s"
                              f"（未完成 {_settle.get('pending')} 条）"},
        "smoke": check_smoke(),
        # 子表查询派发事实检查（聚合 BP 是否真的调过查询 BO）——防"消息全 Completed 但 Bundle 缺资源"
        "query_dispatch": check_query_dispatch(topology, wait_seconds=20),
    }
    # 目标侧落地效果（FHIR/DB 计数）—— 防"消息 Completed 但目标为空"
    _ttypes = (list(effect_target_types) if effect_target_types is not None
               else (target_types or ([target_type] if target_type else [])))
    # 显式传 effect_target_types=[]（如本次分组全被许可调度停用）⇒ **完全跳过**落地判定；
    # 若只传空预期而仍带类型，则退化为"全为 0 探测"（保留原有语义）
    _skip_effect = effect_target_types is not None and not effect_target_types
    if not _skip_effect and (expect_targets or _ttypes):
        eff = check_target_effect(_ttypes, expect_targets or {},
                                  _fhir_base_from_topology(topology),
                                  strict=bool(_settle.get("settled")),
                                  wait_seconds=effect_wait)
        results["target_effect"] = {"ok": eff.get("ok"), "issues": eff.get("issues", []),
                                    "counts": eff.get("counts", {}),
                                    "message": f"目标计数: {eff.get('counts', {})}"}
    # 运行期错误（带分类建议，供回喂对应 Agent）
    # 只看最近 3 分钟的运行期错误：生成/重启后立即校验，3 分钟足以覆盖本次产生的错误，
    # 避免把「生成之前的历史错误」当成本次错误去触发 BP 修复循环
    _rerr = collect_runtime_errors(8, minutes=3)
    # 事实检查发现"BP 从未发起子表查询"（消息全 Completed 但 Bundle 缺资源）→ 归 bp_code，
    # 触发既有修复通道（repair_from_runtime_errors 的 method_updates 增量修复）
    _qd = results.get("query_dispatch") or {}
    for _it in (_qd.get("issues") or []):
        if _it.get("severity") == "error":
            _rerr.append({"kind": "bp_code", "config": "PatientTxProcess",
                          "text": str(_it.get("message", "")),
                          "advice": "聚合 BP 代码缺陷（子表查询未发起/错误被吞）：method_updates 增量修复"})
    if _rerr:
        kinds = sorted({e.get("kind", "") for e in _rerr})
        results["runtime"] = {
            "ok": False, "kinds": kinds,
            "issues": [{"severity": "warning", "item": e.get("config") or "runtime",
                        "message": f"[{e.get('kind')}] {e.get('text', '')[:200]} | 建议: {e.get('advice', '')}"}
                       for e in _rerr],
            "message": f"最近运行期错误 {len(_rerr)} 条（分类: {'/'.join(kinds)}）",
        }
    issues: list[dict] = []
    for key, res in results.items():
        for issue in res.get("issues", []):
            issues.append({"check": key, **issue})
        if not res.get("ok") and not res.get("issues"):
            if res.get("level") != "warning" and res.get("message"):
                issues.append({"check": key, "severity": "error",
                               "item": key, "message": res.get("message", "")})
    errors = [i for i in issues if i.get("severity") == "error"]
    warnings = [i for i in issues if i.get("severity") == "warning"]
    return {"ok": len(errors) == 0, "results": results, "issues": issues,
            "error_count": len(errors), "warning_count": len(warnings)}


# ---- 经验沉淀（^demo.ValidationIssue） ----
# ---- 许可预算（社区版 KeyLicenseUnits=8，每个 Ens 业务主机占 1 个） ----

def license_units() -> int:
    """IRIS 许可单元总数（社区版为 8；IRIS 空闲时可开 8 个并发连接）。"""
    try:
        return int(iris_connector.class_method_value("%SYSTEM.License", "KeyLicenseUnits") or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取许可单元数失败: %s", exc)
        return 0


def production_items(production: str = PRODUCTION_NAME) -> list[dict]:
    """读取 Production 组件清单（name/className/enabled），供前端启停面板与预算计算。"""
    try:
        data = iris_connector.class_method_value("demo.PipelineQuery", "GetItems", production)
        return json.loads(data or "[]")
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 Production 组件清单失败: %s", exc)
        return []


def schedule_groups(groups: list[dict] | None, units: int | None = None) -> dict:
    """按许可容量调度「管道分组」：放得下的启用，放不下的**生成但停用**（不失败）。

    背景（实测）：IRIS 社区版 `KeyLicenseUnits = 8`，每个 Ens 业务主机（BS/BP/BO）常驻
    占 1 个，后端自身连接再占 1 → 同时最多 `units - 1` 个业务主机。多管道提交时按
    **提交顺序贪心装箱**：整组放得下就启用（一组整体启停，不做组内裁剪），放不下的整组
    停用——组件照旧生成（Production 里能看到、也能被验证），用户在 UI 一键切换即可。

    参数:
        groups: [{"category": 管道类别, "items": [业务主机名, ...]}, ...]（按提交顺序；
                基础设施 JavaGateway 不参与预算，调用方勿放入）
        units:  许可单元数（缺省取 license_units()）
    返回:
        {"units","max_hosts","scheduled","suspended","unschedulable",
         "active_items","suspended_items","over_capacity","ok","note"}
        - scheduled / suspended: 类别名列表；
        - unschedulable: 单组自身就超过上限（即使独占也启不动 → 需减少组件或释放许可）；
        - over_capacity = 有分组被停用：**不是错误**（组件已生成、可一键切换）。
    """
    units = units if units is not None else license_units()
    max_hosts = max(0, (units or 0) - 1)
    scheduled: list[str] = []
    suspended: list[str] = []
    unschedulable: list[str] = []
    active_items: list[str] = []
    suspended_items: list[str] = []
    used = 0
    for g in groups or []:
        cat = str((g or {}).get("category") or "unknown")
        items = [str(n).strip() for n in ((g or {}).get("items") or []) if str(n).strip()]
        if not items:
            continue
        if len(items) > max_hosts:
            # 单组自身超过上限：即使独占全部许可也启不动
            unschedulable.append(cat)
            suspended.append(cat)
            suspended_items.extend(items)
            continue
        if used + len(items) <= max_hosts:
            used += len(items)
            scheduled.append(cat)
            active_items.extend(items)
        else:
            suspended.append(cat)
            suspended_items.extend(items)
    note = ""
    if suspended:
        note = (f"许可调度：{len(scheduled)} 组已启用（{used}/{max_hosts} 个业务主机），"
                f"{len(suspended)} 组因超出许可上限已生成但处于停用状态"
                f"（{', '.join(suspended)}）——可在「数据管道」卡片一键切换启用。")
        if unschedulable:
            note += ("注意：" + "、".join(unschedulable)
                     + f" 单组组件数已超过上限（最多 {max_hosts} 个业务主机）："
                       "需减少组件（如少选源表）或释放许可后才能启用。")
        logger.warning("许可调度：%s", note)
    return {"units": units, "max_hosts": max_hosts,
            "scheduled": scheduled, "suspended": suspended,
            "unschedulable": unschedulable,
            "active_items": active_items, "suspended_items": suspended_items,
            "over_capacity": bool(suspended), "ok": True, "note": note}


def apply_license_budget(active_items: list[str] | None = None,
                         production: str = PRODUCTION_NAME,
                         keep_categories: list[str] | None = None,
                         groups: list[dict] | None = None) -> dict:
    """生成管道前做「许可预算」：让不属于本次管道的业务主机让出许可单元。

    背景（实测）：IRIS 社区版 `KeyLicenseUnits = 8`，空闲时可开 8 个连接，而每个 Ens
    业务主机（BS/BP/BO）常驻占 1 个。多管道 Production 整体启动会立刻耗尽许可 →
    后端连不上 IRIS → 生成/编译/校验全部失败（表现为"追加第二条管道必然失败"）。
    等效做法就是用户手工的"生成新管道前先一一关闭旧管道的业务组件"，这里自动化：
      1. **按管道类别**（keep_categories）让整条管道让路——组件 Category 不在 keep 名单内的
         业务主机 Enabled=0（共享组件 shared / JavaGateway 永不因此停用）；
      2. **按组件名**精确启用本次拓扑内的组件，兜住无类别（历史遗留）组件。
    **许可调度**（groups 非空）：本次拓扑内部按组装箱，放不下的组停用（见 schedule_groups），
    超容量不再直接失败 —— 组件已生成，用户在 UI 一键切换即可。

    返回 {"units","max_hosts","active","disabled","disabled_by_category","keep_categories",
          "scheduled"?,"suspended"?,"suspended_items"?,"unschedulable"?,
          "over_capacity","note","ok","full_success"}；
    over_capacity=true 表示**有分组被调度停用**（不是失败；full_success=false 时请显式告知用户）。

    active_items: 本次管道要启用的业务主机名（源 BS + BP + BO）；groups 非空时可省略。
    keep_categories: 本次管道类别（管道实例 category），用于按管道让路。
    groups: 本次拓扑的管道分组（[{"category","items"}]），用于容量调度。
    """
    schedule = schedule_groups(groups) if groups else None
    if schedule is not None:
        active = list(schedule["active_items"])
    else:
        active = [str(n).strip() for n in (active_items or []) if str(n).strip()]
    keep_cats = [str(c).strip() for c in (keep_categories or []) if str(c).strip()]
    disabled: list[str] = []
    disabled_by_category: list[str] = []
    units = license_units()
    if keep_cats:
        try:
            raw_cat = iris_connector.class_method_value(
                "demo.PipelineQuery", "SetItemsEnabledByCategory", production,
                ",".join(keep_cats), 0)
            disabled_by_category = [x for x in str(raw_cat or "").split(",") if x.strip()]
            if disabled_by_category:
                logger.info("许可预算：按管道类别让路 %s（保留类别 %s）",
                            disabled_by_category, keep_cats)
        except Exception as exc:  # noqa: BLE001
            logger.warning("许可预算（按类别让路）失败: %s", exc)
    try:
        raw = iris_connector.class_method_value(
            "demo.PipelineQuery", "DisableNonActiveItems", production, ",".join(active))
        for name in str(raw or "").split(","):
            if name.strip() and name.strip() not in disabled:
                disabled.append(name.strip())
    except Exception as exc:  # noqa: BLE001
        logger.warning("许可预算（禁用非本次组件）失败: %s", exc)
    for name in disabled_by_category:
        if name not in disabled:
            disabled.append(name)
    if disabled:
        logger.info("许可预算：让路组件 %s（本次激活 %d 个，许可单元 %d）",
                    disabled, len(active), units)
    # 容量：有分组被调度停用（组内数量 > 剩余单元）；未传 groups 时退化为旧口径
    if schedule is not None:
        over = bool(schedule["suspended"])
        note = schedule.get("note") or ""
    else:
        over = bool(units) and (len(active) + 1) > units
        note = ""
        if over:
            note = (f"许可容量不足：本次管道需 {len(active)} 个业务主机（+1 个后端连接）"
                    f"> 许可单元 {units}（业务主机常驻各占 1 个，且组内不可裁剪）。")
    if over:
        logger.warning("许可预算超容量：%s", note)
    out = {"units": units, "max_hosts": max(0, (units or 0) - 1), "active": active,
           "disabled": disabled, "disabled_by_category": disabled_by_category,
           "keep_categories": keep_cats,
           "over_capacity": over, "note": note, "ok": True, "full_success": not over}
    if schedule is not None:
        _susp = {str(n) for n in schedule["suspended_items"]}
        out.update({"scheduled": schedule["scheduled"], "suspended": schedule["suspended"],
                    "unschedulable": schedule["unschedulable"],
                    "suspended_items": schedule["suspended_items"],
                    # 本次被调度停用的分组组件不算"让路"（它们本就是本次要生成的），
                    # 避免 UI 把同一件事显示两遍（让路 + 调度停用）
                    "disabled": [n for n in disabled if n not in _susp]})
    return out


def _item_running(name: str) -> bool:
    """Ens 组件主机**是否真的在运行**（`^Ens.Runtime("ConfigItem", <name>, "Job")=1`）。

    为什么不能只看配置/`IsItemEnabled`：配置 `Enabled=1` 与「主机进程已起」是两回事——
    `EnableConfigItem` 在配置已启用时会直接返回 "...already enabled..." 而**不启动主机**
    （实测：`IsItemEnabled=1` 但 `Job=0`、源 BS 不轮询）。运行态事实以 `Job` 为准。
    """
    try:
        return int(iris_connector.global_get("^Ens.Runtime", "ConfigItem", name, "Job") or 0) == 1
    except Exception:  # noqa: BLE001 - 读取失败按"未运行"处理（保守）
        return False


def set_items_enabled(names: list[str], enabled: bool,
                      production: str = PRODUCTION_NAME) -> dict:
    """批量启用/禁用 Production 组件（**配置层 + 运行期双写**，供用户切换管道占用许可）。

    为什么双写（实测 2026-09-14）：只改 `Ens.Config` 的 Enabled 并 `%Save()` 时，IRIS 的
    ScheduleHandler **不一定**立刻 reload —— 出现过「config 已改（Enabled=1）但 runtime 未变
    （组件根本没启动、源 BS 不轮询）」的静默失败，表现为"点了一键切换却没生效"。
    故这里：① 写配置（持久化，重启生产后仍生效）；② 逐个调运行期 API
    `Ens.Director.EnableConfigItem(name, enable, 1)` 立即生效；③ 用
    `Ens.Director.IsItemEnabled(name)` 复核，把不一致显式返回（`runtime_mismatch`）。

    注意：启用组件会占用许可单元（每个业务主机 1 个）；当"已启用组件数 + 后端连接数"
    超过 license_units() 时，IRIS 将拒绝新连接（后端接口会 500）。生成流程会在启动前
    自动做许可预算，手工切换时请留意此上限。
    """
    names = [str(n).strip() for n in (names or []) if str(n).strip()]
    csv = ",".join(names)
    if not csv:
        return {"ok": False, "message": "未指定组件名", "items": []}
    want = 1 if enabled else 0
    # ① **运行期优先**（2026-09-18 P3 修复：先运行期、再落配置）
    #    原实现先写配置：`EnableConfigItem(name,0,1)` 随后看到配置已是目标态 → 返回
    #    `<Ens>ErrGeneral … already disabled/already enabled in Production` → **空操作**
    #    → 作业照跑（实测：停用后 180s 的 6 个主机 job 仍为 1，许可不释放；与 N1 同构、方向相反）。
    #    先调运行期（此刻配置仍是原值 → 转换是真实转换，API 返回 "1"）→ 作业立即停/起；
    #    IRIS 会同步更新该项配置，随后落配置只是持久化 + 幂等复核。
    applied: list[str] = []
    failed: list[dict] = []
    for n in names:
        try:
            st = iris_connector.class_method_value(
                "Ens.Director", "EnableConfigItem", n, want, 1)
        except Exception as exc:  # noqa: BLE001 - 生产未运行时运行期切换不可用（配置层仍会写）
            failed.append({"name": n, "error": str(exc)[:160]})
            continue
        txt = "".join(ch for ch in str(st) if 32 <= ord(ch) < 127)[:160]
        low = txt.lower()
        # "already enabled/disabled" = 运行态本就是目标状态（良性，算生效）
        if txt.strip() in ("1", "True", "true") or "already enabled" in low \
                or "already disabled" in low:
            applied.append(n)
        else:
            failed.append({"name": n, "result": txt})
    # ② 落配置（持久化；运行期改动可能已由 IRIS 同步 → 幂等）
    res, config_ok = "", False
    try:
        res = iris_connector.class_method_value(
            "demo.PipelineQuery", "SetItemsEnabled", production, csv, want)
        config_ok = str(res).endswith(":ok")
    except Exception as exc:  # noqa: BLE001 - 运行期已生效，配置写失败要显式报出（不吞）
        logger.warning("落配置失败（运行期已生效）: %s", exc)
        failed.append({"name": "(config)", "error": str(exc)[:160]})
    # ③ 复核**真正的运行态**：配置 Enabled=1 ≠ 主机已起。
    #    实测（2026-09-15）：组件生成时 Enabled="false"（许可调度停用），用户一键启用时平台先写
    #    配置 Enabled=1 再调 EnableConfigItem → 后者看到配置已启用，直接返回
    #    `<Ens>ErrGeneral ... already enabled in Production ...` **而不启动主机** →
    #    UI 显示已启用、IsItemEnabled=1、runtime_applied 非空，但 `^Ens.Runtime("ConfigItem",n,"Job")=0`、
    #    源 BS 完全不轮询（静默不生效）。故此处按 Job 事实复核，必要时做「停用→启用」对来真正拉起主机。
    restarted: list[str] = []
    retried_down: list[str] = []
    still_down: list[str] = []
    still_up: list[str] = []
    for n in names:
        want_up = bool(enabled)
        up = _item_running(n)
        if up == want_up:
            continue
        try:
            if want_up:
                # 配置已启用但主机未起 → 先运行期停用再启用（实测这一对能真正拉起主机）
                iris_connector.class_method_value("Ens.Director", "EnableConfigItem", n, 0, 1)
                iris_connector.class_method_value("Ens.Director", "EnableConfigItem", n, 1, 1)
                restarted.append(n)
            else:
                # 2026-09-18 P3：停用方向的**镜像补救**（配置已停但主机仍在跑 → 先启用再停）
                iris_connector.class_method_value("Ens.Director", "EnableConfigItem", n, 1, 1)
                iris_connector.class_method_value("Ens.Director", "EnableConfigItem", n, 0, 1)
                retried_down.append(n)
        except Exception as exc:  # noqa: BLE001 - 补救失败进 still_*，由下面的生产重启收敛兜底
            logger.warning("运行期%s组件 %s 失败: %s", "拉起" if want_up else "停止", n, exc)
        if _item_running(n) != want_up:
            (still_down if want_up else still_up).append(n)
    if restarted:
        logger.info("运行期「停用→启用」拉起主机: %s", restarted)
    if retried_down:
        logger.info("运行期「启用→停用」真正停止主机: %s", retried_down)
    # ③b 兜底：仍有不一致 → **一次有界 Production 重启收敛**（全量下发配置；P3）
    converged = False
    if still_up or still_down:
        converged = _converge_by_production_restart(production)
        if converged:
            still_down = [n for n in still_down if not _item_running(n)]
            still_up = [n for n in still_up if _item_running(n)]
    if still_down or still_up:
        logger.warning("运行期启停未完全生效（已尝试运行期补救%s）: 未起=%s 未停=%s",
                       " + 生产重启收敛" if converged else "", still_down, still_up)
    # ④ 复核配置-运行一致性，返回差异（调用方/UI 可据此提示"需重启生产"）
    mismatch: list[str] = []
    for n in names:
        try:
            if int(iris_connector.class_method_value(
                    "Ens.Director", "IsItemEnabled", n) or 0) != want:
                mismatch.append(n)
        except Exception:  # noqa: BLE001 - 复核失败不算差异
            pass
    if mismatch:
        logger.warning("运行期启停未完全生效（可重启生产收敛）: %s", mismatch)
    return {"ok": config_ok, "result": str(res), "units": license_units(),
            "enabled": bool(enabled), "items": names,
            "runtime_applied": applied, "runtime_failed": failed,
            "runtime_restarted": restarted, "runtime_retried_stop": retried_down,
            "runtime_still_down": still_down,
            "runtime_still_up": still_up, "runtime_mismatch": mismatch,
            "converged_by_restart": converged}


def _converge_by_production_restart(production: str = PRODUCTION_NAME) -> bool:
    """有界 Production 重启收敛（P3 兜底）：StopProduction → StartProduction，全量下发配置。

    为什么需要：个别主机可能在运行期启停 API 之后仍与配置不一致（作业没停/没起）。
    重启生产 = IRIS 按持久化配置重新下发一次，是**确定性收敛**手段（平台原来的提示就是
    "请重启 Production 收敛"，此处把它自动化）。返回是否执行且收敛成功（running=1）。
    非破坏性：只重启生产进程，不改配置、不删数据、不动其它组件。
    """
    import time as _t
    try:
        iris_connector.class_method_value("Ens.Director", "StopProduction")
        for _ in range(20):                       # 最多等 ~20s 让生产真正停下
            _t.sleep(1)
            try:
                running = iris_connector.class_method_value(
                    "Ens.Director", "IsProductionRunning")
                if not running or str(running) in ("0", "False", "false"):
                    break
            except Exception:  # noqa: BLE001 - 查询失败按"继续等"处理
                pass
        started = iris_connector.class_method_value(
            "Ens.Director", "StartProduction", production)
        ok = str(started).strip() in ("1", "True", "true")
        logger.warning("为收敛运行期启停差异，已重启 Production（start=%s）", started)
        return ok
    except Exception as exc:  # noqa: BLE001 - 收敛失败不抛（调用方按 still_* 显式报告）
        logger.warning("Production 重启收敛失败: %s", exc)
        return False


def save_validation_issue(pattern: str, resolution: str,
                          source: str = "generate") -> dict:
    """记录问题模式与解决方案到 ^demo.ValidationIssue（后续注入验证 Agent）。

    注意: pattern/resolution 中不应包含字符 "|"（IRIS 侧用 "|" 分段存储）。
    """
    pattern = (pattern or "").replace("|", " ")
    resolution = (resolution or "").replace("|", " ")
    try:
        result = iris_connector.class_method_value(
            "demo.PipelineQuery", "SaveValidationIssue", pattern, resolution, source)
        return {"ok": result == "OK", "pattern": pattern}
    except Exception as exc:  # noqa: BLE001
        logger.warning("沉淀验证经验失败: %s", exc)
        return {"ok": False, "message": str(exc)}


def load_validation_issues(limit: int = 5) -> list[dict]:
    """读取最近的问题经验（注入验证 Agent prompt，作为修复参考）。"""
    try:
        data = iris_connector.class_method_value(
            "demo.PipelineQuery", "GetValidationIssues", limit)
        return json.loads(data or "[]")
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取验证经验失败: %s", exc)
        return []


# ---- 生成前连通性检查（Connection Contract 前置门禁） ----

def check_connection(sources: list[dict] | None = None,
                     targets: list[dict] | None = None) -> dict:
    """生成前的源/目标连通性检查（D1 类参数错误在生成前拦截）。

    sources/targets 为已归一的 runtime 契约（repository.datasource_runtime / target_runtime）。
    优先用契约 health；health 缺失或失败时现场最小探测（FHIR metadata / SOAP endpoint / SQL ping）。
    返回: {"ok": bool, "issues": [...], "results": [{item, role, ok, detail}]}
    """
    issues: list[dict] = []
    results: list[dict] = []
    import datetime

    from backend.services import connection_profiler

    def _probe(rt: dict) -> dict:
        """现场最小连通探测，返回 health dict。"""
        kind = (rt.get("kind") or "").upper()
        conn = rt.get("connection") or {}
        health: dict = {"ok": False, "detail": "", "checked_at": datetime.datetime.now().isoformat()}
        try:
            if kind == "FHIR":
                # ⚠ 回环归一：登记值常是浏览器视角的 http://localhost:52773/...，backend 在独立
                #   容器里连不上 → 门禁会误报「Connection refused」。与 connection_profiler
                #   的 profile_source/profile_target 同口径（对外保存的仍是登记值）。
                from backend.config import FHIRConfig, to_internal_url
                url = to_internal_url((conn.get("endpoint") or conn.get("base_url")
                                       or FHIRConfig.SOURCE_BASE_URL or FHIRConfig.BASE_URL
                                       or "").strip())
                cap, _poll = connection_profiler._fhir_capabilities(
                    url, conn.get("username") or "", conn.get("password") or "")
                health["ok"] = bool(cap)
                health["detail"] = "metadata 可达（fhir %s）" % (cap.get("fhir_version") or "?")
            elif kind == "SOAP":
                from backend.config import to_internal_url
                url = to_internal_url((conn.get("endpoint") or conn.get("base_url") or "").strip())
                h = connection_profiler._http_reachable(url)
                health.update(h)
            elif kind in ("SQL", "DB"):
                rows = iris_connector.query("SELECT 1")
                health["ok"] = bool(rows)
                health["detail"] = "localTarget OK" if rows else "ping 失败"
            else:
                health["detail"] = f"未支持类型 {kind}"
        except Exception as exc:  # noqa: BLE001
            health["detail"] = str(exc)[:200]
            health["ok"] = False
        return health

    for rt in (sources or []) + (targets or []):
        if not rt or not rt.get("kind"):
            continue
        kind = rt.get("kind")
        role = rt.get("role") or ("source" if rt in (sources or []) else "target")
        health = rt.get("health") or {}
        if not health.get("ok"):
            health = _probe(rt)  # 契约未探查/曾失败 → 现场再试
        item = f"{role}.{kind}"
        ok = bool(health.get("ok"))
        detail = health.get("detail") or ""
        results.append({"item": item, "role": role, "kind": kind, "ok": ok, "detail": detail})
        if not ok:
            issues.append({"severity": "error", "item": item,
                           "message": f"{item} 不可达/未配置: {detail}"})
    return {"ok": not issues, "issues": issues, "results": results}

