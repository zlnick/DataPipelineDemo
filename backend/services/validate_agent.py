"""验证-修复 Agent（C2：数据管道验证-修复）：工具事实检查 + AI 判断 + 分层修复闭环。

职责范围 = 数据管道（组件拓扑/编译/启动/消息流转），与数据转换验证-修复
（Agent C1，transformation_validator.py）分离：
- C2 验证对象 = 管道拓扑 + 生成结果，知识域 = IRIS 互操作性；
- C1 验证对象 = 转换关系（字段映射），知识域 = FHIR 字段语义；
- 事实检查工具共享（pipeline_validator），L1/L2/L3 分层修复框架各自实现。

验证原则：
- 事实检查（组件枚举/编译/启动/消息状态）由函数工具完成（AI 做不了精确事实检查）；
- 验证策略（错误是否实质、如何修复）由 AI Agent 动态组合；
- 修复分层有界：L1 规则修复 → L2 LLM 重试带错误反馈 ≤2 轮 → L3 回退默认；
- 经验沉淀入 ^demo.ValidationIssue 并在后续 prompt 注入。
"""

import json
import logging

from backend.services import iris_connector, pipeline_validator, type_registry
from backend.services.llm_client import _call_llm

logger = logging.getLogger(__name__)

# ===== Agent C2：管道验证-修复（系统提示词，聚焦 IRIS 管道） =====
SYSTEM_PROMPT_VALIDATE = (
    "你是 IRIS 数据管道验证与修复专家。给定数据管道生成/验证报告（事实检查结果，"
    "覆盖组件拓扑/编译/启动/消息流转）与上下文，判断哪些问题是实质性错误并给出修复动作。"
    "原则：1. 事实检查结果可信（组件类型合法性、className 有效性、编译错误、启动状态、"
    "消息状态），你只负责判断影响与选择修复路径。"
    "2. 修复动作从白名单选择："
    '{"action":"fix_topology","fix":{"components":[...]}} 修正拓扑后重新生成；'
    '{"action":"rebuild","message":"带错误反馈重新生成"}；'
    '{"action":"start","message":"重启 Production"}；'
    '{"action":"fallback","message":"回退默认拓扑"}；'
    '{"action":"none","message":"无需修复"}。'
    "3. 只修实质性错误（编译失败、必选组件缺失、className 无效、启动失败、消息 Error），"
    "warning 类（如尚无消息流转）不视为错误。"
    "4. 参考 past_issues 历史解决方案，避免重复错误。"
    "严格输出 JSON（不要输出其他文字），格式："
    '{"assessment":[{"issue":"...","severity":"error|warning","impact":"high|low","reason":"..."}],'
    '"fix":{"action":"...","message":"...","fix":{"..."}},'
    '"experience":{"pattern":"历史问题模式","resolution":"解决方案"}}'
)

# 修复动作白名单（管道域）
VALID_ACTIONS = {"fix_topology", "rebuild", "start", "fallback", "none"}


def judge_issues(report: dict, context: dict, past_issues: list[dict]) -> dict:
    """Agent C：判断问题 + 给出修复动作（LLM 单轮决策）。

    参数:
        report: pipeline_validator.run_validation 输出
        context: {source_type, target_type, production, component_types}
        past_issues: ^demo.ValidationIssue 历史经验
    返回:
        {"assessment": [...], "fix": {"action", "message", "fix"}, "experience": {...}}
    """
    user_content = json.dumps({
        "validation_report": report,
        "context": context,
        "past_issues": past_issues,
    }, ensure_ascii=False, indent=2)
    result = _call_llm(SYSTEM_PROMPT_VALIDATE, user_content, "验证修复Agent")
    fix = result.get("fix") or {}
    if fix.get("action") not in VALID_ACTIONS:
        fix = {"action": "fallback", "message": "LLM 返回未知动作，回退默认拓扑"}
    return {
        "assessment": result.get("assessment", []),
        "fix": fix,
        "experience": result.get("experience", {}) or {},
    }


def _l1_rule_fix(topology: dict | None, source_type: str,
                 target_type: str) -> tuple[dict, bool]:
    """L1 规则修复：明确机械规则（剔除未知组件、补 className、补必选组件）。

    返回: (修正后的拓扑, 是否有改动)
    """
    components = (topology or {}).get("components", [])
    templates: dict[str, dict] = {}
    for c in type_registry.get_source_components(source_type):
        templates[c["type"]] = c
    for c in type_registry.get_target_components(target_type):
        templates[c["type"]] = c
    for c in type_registry.get_common_components():
        templates[c["type"]] = c

    changed = False
    fixed: list[dict] = []
    for c in components:
        ctype = c.get("type", "")
        if ctype not in templates:
            changed = True  # 剔除未知类型组件
            continue
        if not c.get("className"):
            c = {**c, "className": templates[ctype]["className"],
                 "comment": c.get("comment") or templates[ctype].get("comment", "")}
            changed = True
        fixed.append(c)

    seen = {c.get("type") for c in fixed}
    for req in ("TransformProcess", "JavaGateway"):
        if req not in seen and req in templates:
            tpl = templates[req]
            fixed.append({
                "type": req, "name": tpl["className"], "className": tpl["className"],
                "comment": tpl.get("comment", ""),
                "settings": [{"target": "Host", "name": k, "value": v}
                             for k, v in tpl.get("settings", {}).items()],
            })
            changed = True

    new_topo = {
        "production": (topology or {}).get("production")
        or pipeline_validator.PRODUCTION_NAME,
        "components": fixed,
    }
    return new_topo, changed


def _default_topology(mappings: list[dict], source_type: str,
                      target_type: str) -> dict:
    """L3 回退：按注册表 + 映射重建完整拓扑（等价 build_pipeline_topology）。"""
    from backend.routes.pipelines import build_pipeline_topology  # 延迟 import 避免循环依赖
    return build_pipeline_topology(mappings, source_type, target_type)


def validate_and_fix_pipeline(mappings: list[dict], topology: dict | None,
                              generate_fn, source_type: str = "FHIR",
                              target_type: str = "DB",
                              production: str = pipeline_validator.PRODUCTION_NAME,
                              max_rounds: int = 2) -> dict:
    """验证-修复闭环（≤2 轮）：工具检查 → L1 规则修复 → L2 LLM 决策 → L3 回退。

    参数:
        mappings: 转换关系列表
        topology: 完整拓扑（build_pipeline_topology 输出）
        generate_fn: 重新生成的可调用对象 generate_fn(topology, mappings) -> str
    返回:
        {"status": "ok"|"failed", "rounds": [...], "topology", "mappings",
         "report", "message"}
    """
    context = {
        "source_type": source_type,
        "target_type": target_type,
        "production": production,
        "component_types": [c.get("type") for c in (topology or {}).get("components", [])],
    }
    past_issues = pipeline_validator.load_validation_issues()
    current_topology = topology or {}
    rounds_log: list[dict] = []
    report: dict = {}

    for round_i in range(max_rounds + 1):
        report = pipeline_validator.run_pipeline_validation(
            current_topology, source_type, target_type, production)
        errors = [i for i in report["issues"] if i.get("severity") == "error"]
        err_summary = "；".join(f"[{i.get('check')}] {i.get('message')}" for i in errors)
        rounds_log.append({"round": round_i, "error_count": len(errors),
                           "summary": err_summary or "无错误"})
        if not errors:
            return {"status": "ok", "rounds": rounds_log,
                    "topology": current_topology, "mappings": list(mappings or []),
                    "report": report, "message": "验证通过"}

        # L1 规则修复（管道域机械规则：剔未知组件/补 className/补必选）
        new_topo, topo_fixed = _l1_rule_fix(current_topology, source_type, target_type)
        if topo_fixed:
            current_topology = new_topo
            logger.info("管道 L1 规则修复生效: 拓扑改动=True")
            # L1 修复后立即重新生成，下一轮验证修复效果
            try:
                gen_result = generate_fn(current_topology, mappings)
                logger.info("管道 L1 修复后重新生成: %s", gen_result)
            except Exception as exc:  # noqa: BLE001 - 修复循环需记录并继续
                logger.error("管道 L1 修复后重新生成失败: %s", exc)
            continue

        # L2：LLM 决策修复
        decision = judge_issues(report, context, past_issues)
        fix = decision.get("fix", {})
        action = fix.get("action", "fallback")
        logger.info("管道验证 Agent 决策: action=%s message=%s",
                    action, fix.get("message", ""))
        if action == "fix_topology":
            comps = fix.get("fix", {}).get("components")
            if isinstance(comps, list) and comps:
                current_topology = {"production": production, "components": comps}
        elif action == "start":
            # 启动 Production（Ens 恢复场景：验证通过但 Production 未运行）
            try:
                start_result = iris_connector.class_method_value(
                    "demo.PipelineQuery", "StartProduction", production)
                logger.info("启动 Production: %s", start_result)
            except Exception as exc:  # noqa: BLE001
                logger.error("启动 Production 失败: %s", exc)
        elif action == "fallback":
            current_topology = _default_topology(
                list(mappings or []), source_type, target_type)

        # 重新生成（L2/L3；none/start 不触发重新生成，start 已单独执行启动）
        if action not in ("none", "start"):
            try:
                gen_result = generate_fn(current_topology, mappings)
                logger.info("第 %d 轮重新生成: %s", round_i + 1, gen_result)
            except Exception as exc:  # noqa: BLE001 - 修复循环需记录并继续
                logger.error("第 %d 轮重新生成失败: %s", round_i + 1, exc)

        # 经验沉淀
        exp = decision.get("experience") or {}
        if exp.get("pattern") and exp.get("resolution"):
            saved = pipeline_validator.save_validation_issue(
                exp["pattern"], exp["resolution"], source="validate-agent")
            logger.info("管道验证经验已沉淀: %s", saved.get("ok"))

    return {"status": "failed", "rounds": rounds_log,
            "topology": current_topology, "mappings": list(mappings or []),
            "report": report, "message": "超过最大修复轮数仍存在问题"}


