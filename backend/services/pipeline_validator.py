"""管道生成验证工具（事实检查用函数，AI 做不了精确事实检查）。

验证原则（与验证-修复 Agent 配合）：
- 事实检查（列名/编译/启动/消息状态）用函数工具；
- 验证策略（该验证什么、错误是否实质、如何修复）由 AI Agent 动态组合；
- 修复分层有界：L1 规则修复 → L2 LLM 重试带错误反馈 ≤2 轮 → L3 回退默认；
- 经验沉淀入 ^demo.ValidationIssue 并在后续 prompt 注入。
"""

import json
import logging

from backend.services import iris_connector, type_registry

logger = logging.getLogger(__name__)

PRODUCTION_NAME = "demo.DataflowProduction"


def _get_table_columns(table: str, target_models: list[dict] | None = None) -> list[str]:
    """查询目标表/实体列名。

    优先从传入的 target_models 或持久化的 Target/TargetInterface 获取列名结构
    （支持 SOAP 实体或未建 SQL 表的目标）；无匹配时降级查 information_schema。
    """
    if not table:
        return []

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
        tm_table = tm.get("table") or tm.get("name") or tm.get("target_name") or ""
        if tm_table == table:
            cols = _extract_cols(tm)
            if cols:
                return cols

    # 2. 查 Repository 中持久化的 Target / TargetInterface
    try:
        from backend.services import repository
        for tg in repository.list_targets():
            # SOAP 实体或 DB 表
            for tb in tg.get("tables") or []:
                tb_name = tb.get("table") or tb.get("entity_name") or ""
                if tb_name == table:
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

    # 3. 降级查数据库 information_schema.columns
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
        cols = _get_table_columns(table, target_models)
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
        all_known_fields.update(fs)
        asset_roots[a.get("name", "")] = fs | {_path_root(f) for f in fs}

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
            root = _path_root(path)
            # 函数/常量/已有已知字段均算合法
            if path.startswith("concat(") or " " in path or root in known or root in all_known_fields:
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
                        fhir_base: str = "") -> dict:
    """目标侧落地效果事实检查（FHIR：资源计数；DB：目标表行数）。

    用于 C2 判断"事务是否真的落库"——避免"消息 Completed 但目标为空"（如 FHIR 200 + OperationOutcome 回滚）。
    expected: {资源类型/表名: 预期行数}；缺省只做"全为 0"的探测。
    """
    import base64
    import json as _json
    import urllib.request

    from backend.config import FHIRConfig
    types = [str(t).upper() for t in (target_types or [])]
    issues: list[dict] = []
    counts: dict[str, int] = {}
    if "FHIR" in types:
        from backend.config import to_internal_url
        base = to_internal_url(fhir_base or FHIRConfig.BASE_URL or "").rstrip("/") + "/"
        auth = base64.b64encode(
            f"{FHIRConfig.USERNAME}:{FHIRConfig.PASSWORD}".encode()).decode()
        for rt in ("Patient", "Encounter", "Condition", "MedicationRequest"):
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
            if _pipeline_has_messages() == 0:
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
            if got >= 0 and got < int(n):
                issues.append({"severity": "error", "item": rt,
                               "message": f"FHIR {rt} 落地数 {got} < 预期 {n}"
                                          "（事务可能被拒或部分未投递）"})
    if "DB" in types and expected:
        for tbl, n in expected.items():
            try:
                rows = iris_connector.query(f"SELECT COUNT(*) FROM SQLUser.{tbl}")
                got = int(rows[0][0]) if rows else -1
                counts[tbl] = got
                if got >= 0 and got < int(n):
                    issues.append({"severity": "error", "item": tbl,
                                   "message": f"目标表 {tbl} 行数 {got} < 预期 {n}"})
            except Exception as exc:  # noqa: BLE001
                issues.append({"severity": "warning", "item": tbl,
                               "message": f"目标表 {tbl} 计数失败: {exc}"})
    errors = [i for i in issues if i.get("severity") == "error"]
    return {"ok": not errors, "issues": issues, "counts": counts}


def classify_runtime_error(text: str) -> dict:
    """把运行期错误文本分类并给出修复方向（用于回喂对应 Agent）。"""
    t = str(text or "")
    schema_kw = ("OperationOutcome", "MissingRequiredProperty", "MalformedValue", "NotAnArray",
                 "InvalidId", "InvalidJsonContent", "NotAllowed", "UnexpectedObject",
                 "UnexpectedPropertyName", "InvalidResource")
    code_kw = ("SUBSCRIPT", "INVALID OREF", "UNDEFINED", "CLASS DOES NOT EXIST", "MPP5377",
               "QUIT argument", "PROPERTY DOES NOT EXIST", "PYTHON EXCEPTION", "INVALID CLASS",
               "ILLEGAL VALUE", "Invalid command")
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
                            expect_targets: dict[str, int] | None = None) -> dict:
    """数据管道专项验证（供管道验证-修复 Agent C2 使用）。

    覆盖：拓扑完整性 + 编译 + 启动 + 消息流转 + **目标落地效果**（FHIR/DB 计数）
    + **运行期错误分类**（fhir_schema / bp_code，供回喂对应 Agent）。
    expect_targets: {资源类型或表名: 预期数量}（可选，用于判定"是否真的落库"）。
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
    }
    # 目标侧落地效果（FHIR/DB 计数）—— 防"消息 Completed 但目标为空"
    _ttypes = target_types or ([target_type] if target_type else [])
    if expect_targets or _ttypes:
        eff = check_target_effect(_ttypes, expect_targets or {},
                                  _fhir_base_from_topology(topology))
        results["target_effect"] = {"ok": eff.get("ok"), "issues": eff.get("issues", []),
                                    "counts": eff.get("counts", {}),
                                    "message": f"目标计数: {eff.get('counts', {})}"}
    # 运行期错误（带分类建议，供回喂对应 Agent）
    # 只看最近 3 分钟的运行期错误：生成/重启后立即校验，3 分钟足以覆盖本次产生的错误，
    # 避免把「生成之前的历史错误」当成本次错误去触发 BP 修复循环
    _rerr = collect_runtime_errors(8, minutes=3)
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
    try:
        res = iris_connector.class_method_value(
            "demo.PipelineQuery", "SetItemsEnabled", production, csv,
            1 if enabled else 0)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc), "items": names}
    config_ok = str(res).endswith(":ok")
    # ② 运行期即时生效（配置层已写；运行期失败不回滚配置，但必须显式报出）
    applied: list[str] = []
    failed: list[dict] = []
    for n in names:
        try:
            st = iris_connector.class_method_value(
                "Ens.Director", "EnableConfigItem", n, 1 if enabled else 0, 1)
        except Exception as exc:  # noqa: BLE001 - 生产未运行时运行期切换不可用（配置已写好）
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
    # ③ 复核运行态，返回差异（调用方/UI 可据此提示"需重启生产"）
    mismatch: list[str] = []
    want = 1 if enabled else 0
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
            "runtime_mismatch": mismatch}


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
                from backend.config import FHIRConfig
                url = (conn.get("endpoint") or conn.get("base_url")
                       or FHIRConfig.BASE_URL or "").strip()
                _, _poll = connection_profiler._fhir_capabilities(
                    url, conn.get("username") or "", conn.get("password") or "")
                health["ok"] = True
                health["detail"] = "metadata 可达"
            elif kind == "SOAP":
                url = (conn.get("endpoint") or conn.get("base_url") or "").strip()
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

