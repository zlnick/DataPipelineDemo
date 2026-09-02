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


def _get_table_columns(table: str) -> list[str]:
    """查询目标表列名（information_schema，按 ordinal_position 排序）。"""
    rows = iris_connector.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='SQLUser' AND table_name=? ORDER BY ordinal_position",
        [table])
    return [r[0] for r in rows]


def check_recommendations(mappings: list[dict]) -> dict:
    """检查转换关系：结构完整性 + 目标列名存在于目标表。

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
        cols = _get_table_columns(table)
        if not cols:
            issues.append({"severity": "error", "item": mid,
                           "message": f"目标表 {table} 不存在或列结构为空"})
            continue
        for fm in m.get("field_mappings", []):
            target = fm.get("target", "")
            if target and target not in cols:
                issues.append({"severity": "error", "item": f"{mid}.{target}",
                               "message": f"目标列 {target} 不存在于表 {table}"})
            if not fm.get("source"):
                issues.append({"severity": "warning", "item": f"{mid}.{target}",
                               "message": "字段映射缺少 source 路径"})
    errors = [i for i in issues if i.get("severity") == "error"]
    return {"ok": len(errors) == 0, "issues": issues, "count": len(mappings or [])}


def _path_root(path: str) -> str:
    """取路径首段（不含下标），如 name[0].family → name。"""
    return (path or "").split("[")[0].split(".")[0]


def check_source_fields(assets: list[dict], mappings: list[dict]) -> dict:
    """检查转换关系的源字段路径是否在已知资产字段中（warning 级，供 AI 语义判断）。

    说明：FHIR 资源字段远多于资产登记的 key 字段，因此未知路径只标 warning
    （不阻塞），由转换验证 Agent 判断是否语义错配。
    """
    issues: list[dict] = []
    asset_roots: dict[str, set[str]] = {}
    for a in assets or []:
        asset_roots[a.get("name", "")] = {_path_root(f) for f in a.get("fields", [])}
    for m in mappings or []:
        src = m.get("source", "")
        known = asset_roots.get(src, set())
        if not known:
            continue  # 资产未登记字段，跳过
        for fm in m.get("field_mappings", []):
            path = fm.get("source", "")
            root = _path_root(path)
            if root and root not in known:
                issues.append({"severity": "warning", "item": f"{m.get('id', '?')}.{path}",
                               "message": f"源路径 {path} 不在资产 {src} 的已知字段中"})
    return {"ok": True, "issues": issues}


def check_pipeline_topology(topology: dict | None, source_type: str,
                            target_type: str) -> dict:
    """检查拓扑：组件枚举合法 + 必选组件齐全 + className 非空。

    返回: {"ok": bool, "issues": [...], "components": [type...]}
    """
    issues: list[dict] = []
    components = (topology or {}).get("components", [])
    if not components:
        issues.append({"severity": "error", "item": "components",
                       "message": "拓扑没有组件"})
    allowed = set()
    for c in type_registry.get_source_components(source_type):
        allowed.add(c["type"])
    for c in type_registry.get_target_components(target_type):
        allowed.add(c["type"])
    for c in type_registry.get_common_components():
        allowed.add(c["type"])
    seen: set[str] = set()
    for c in components:
        ctype = c.get("type", "")
        if ctype not in allowed:
            issues.append({"severity": "error", "item": ctype,
                           "message": f"组件类型 {ctype} 不在类型注册表中"})
        if not c.get("className"):
            issues.append({"severity": "error", "item": ctype,
                           "message": f"组件 {ctype} 缺少 className"})
        seen.add(ctype)
    for req in ("TransformProcess", "JavaGateway"):
        if req not in seen:
            issues.append({"severity": "error", "item": req,
                           "message": f"缺少必选组件 {req}"})
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


def check_smoke(limit: int = 10) -> dict:
    """运行级 smoke test：最近消息流转是否全部 Completed。

    返回: {"ok": bool, "level": "info|warning|error", "message": str, "items": [...]}
    """
    try:
        data = iris_connector.class_method_value("demo.PipelineQuery", "GetLogs", limit)
        items = json.loads(data or "[]")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "level": "error",
                "message": f"消息检查失败: {exc}", "items": []}
    errors = [i for i in items if i.get("status") != "Completed"]
    if errors:
        return {"ok": False, "level": "error",
                "message": f"{len(errors)}/{len(items)} 条消息未完成（存在运行错误）",
                "items": errors}
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


def run_transformation_validation(mappings: list[dict], assets: list[dict] | None = None) -> dict:
    """转换关系专项验证（供转换验证-修复 Agent C1 使用）。

    覆盖：映射结构/目标列存在性（error）+ 源字段路径（warning）。
    """
    results = {
        "recommendations": check_recommendations(mappings),
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


def run_pipeline_validation(topology: dict | None, source_type: str = "FHIR",
                            target_type: str = "DB",
                            production: str = PRODUCTION_NAME) -> dict:
    """数据管道专项验证（供管道验证-修复 Agent C2 使用）。

    覆盖：拓扑完整性 + 编译 + 启动 + 消息流转（不含转换关系）。
    """
    results = {
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
            if res.get("level") != "warning" and res.get("message"):
                issues.append({"check": key, "severity": "error",
                               "item": key, "message": res.get("message", "")})
    errors = [i for i in issues if i.get("severity") == "error"]
    warnings = [i for i in issues if i.get("severity") == "warning"]
    return {"ok": len(errors) == 0, "results": results, "issues": issues,
            "error_count": len(errors), "warning_count": len(warnings)}


# ---- 经验沉淀（^demo.ValidationIssue） ----
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
