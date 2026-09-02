"""数据转换验证-修复 Agent（C1）：验证转换关系（字段映射）的合法性。

与管道验证-修复 Agent（C2，validate_agent.py）职责分离：
- C1 验证对象 = 转换关系（source 路径 → target 列 + transform），知识域 = FHIR 字段语义；
- C2 验证对象 = 数据管道（组件拓扑/编译/启动/消息），知识域 = IRIS 互操作性；
- 事实检查工具共享（pipeline_validator），L1/L2/L3 分层修复框架各自实现。
"""

import json
import logging

from backend.services import pipeline_validator
from backend.services.llm_client import _call_llm

logger = logging.getLogger(__name__)

# ===== Agent C1：转换验证-修复（系统提示词，聚焦 FHIR 字段语义） =====
SYSTEM_PROMPT_TRANS_VALIDATE = (
    "你是医疗数据转换专家。给定转换关系（源 FHIR 资产 → 目标表列映射）与验证报告"
    "（目标列存在性/源字段路径等事实检查），判断哪些映射问题是实质性错误并给出修复。"
    "原则：1. 事实检查结果可信（目标列不存在、结构缺失是硬错误），"
    "你负责判断字段语义与选择修复路径。"
    "2. 修复动作从白名单选择："
    '{"action":"fix_mappings","fix":{"mappings":[...]}} 修正字段映射（如 target 列名纠错、补 source 路径、'
    '保留 transform）；'
    '{"action":"drop_mapping","fix":{"mapping_id":"M1","reason":"..."}} 删除无意义映射；'
    '{"action":"fallback","message":"保留原映射"}；'
    '{"action":"none","message":"无需修复"}。'
    "3. 只修实质性错误（target 列不存在、source 路径明显错误、字段语义错配），"
    "warning 级（源字段不在资产已知字段）仅在语义明显错配时修复。"
    "4. 参考 past_issues 历史解决方案，避免重复错误。"
    "严格输出 JSON（不要输出其他文字），格式："
    '{"assessment":[{"issue":"...","severity":"error|warning","impact":"high|low","reason":"..."}],'
    '"fix":{"action":"...","message":"...","fix":{"..."}},'
    '"experience":{"pattern":"历史问题模式","resolution":"解决方案"}}'
)

# 修复动作白名单（转换域）
VALID_TRANS_ACTIONS = {"fix_mappings", "drop_mapping", "fallback", "none"}


def normalize_recommendations(recs: list[dict]) -> list[dict]:
    """把 Agent A 输出（asset/target_table 风格）归一化为映射结构（id/source/target_table）。

    使转换验证（check_recommendations 期望 id/source）与生成管道（MappingItem）结构统一。
    """
    normalized: list[dict] = []
    for i, r in enumerate(recs or [], 1):
        normalized.append({
            "id": r.get("id") or f"R{i}",
            "source": r.get("source") or r.get("asset") or "",
            "target_table": r.get("target_table", ""),
            "field_mappings": r.get("field_mappings", []),
            "confidence": r.get("confidence"),
            "reason": r.get("reason"),
        })
    return normalized


def judge_transformation(report: dict, context: dict,
                         past_issues: list[dict]) -> dict:
    """Agent C1：判断转换关系问题 + 给出修复动作（LLM 单轮决策）。"""
    user_content = json.dumps({
        "validation_report": report,
        "context": context,
        "past_issues": past_issues,
    }, ensure_ascii=False, indent=2)
    result = _call_llm(SYSTEM_PROMPT_TRANS_VALIDATE, user_content, "转换验证Agent")
    fix = result.get("fix") or {}
    if fix.get("action") not in VALID_TRANS_ACTIONS:
        fix = {"action": "fallback", "message": "LLM 返回未知动作，保留原映射"}
    return {
        "assessment": result.get("assessment", []),
        "fix": fix,
        "experience": result.get("experience", {}) or {},
    }


def _l1_fix_mappings(mappings: list[dict]) -> tuple[list[dict], bool]:
    """L1 转换规则修复：剔除指向不存在目标列的字段映射（机械规则）。"""
    changed = False
    fixed: list[dict] = []
    for m in mappings or []:
        table = m.get("target_table", "")
        cols = pipeline_validator._get_table_columns(table) if table else []
        fms = []
        for fm in m.get("field_mappings", []):
            target = fm.get("target", "")
            if target and cols and target not in cols:
                changed = True
                continue
            fms.append(fm)
        if len(fms) != len(m.get("field_mappings", [])):
            changed = True
        fixed.append({**m, "field_mappings": fms})
    return fixed, changed


def validate_and_fix_transformation(mappings: list[dict],
                                    assets: list[dict] | None = None,
                                    max_rounds: int = 2) -> dict:
    """转换验证-修复闭环（≤2 轮）：事实检查 → L1 规则 → L2 LLM 决策 → 验证。

    参数:
        mappings: 转换关系列表（Agent A 输出或用户确认）
        assets: 源资产结构（含 fields），用于源路径检查（可选）
    返回:
        {"status": "ok"|"failed", "rounds": [...], "mappings", "report", "message"}
    """
    context = {
        "assets": assets or [],
        "mapping_count": len(mappings or []),
    }
    past_issues = pipeline_validator.load_validation_issues()
    current_mappings = list(mappings or [])
    rounds_log: list[dict] = []
    report: dict = {}

    for round_i in range(max_rounds + 1):
        report = pipeline_validator.run_transformation_validation(
            current_mappings, assets)
        errors = [i for i in report["issues"] if i.get("severity") == "error"]
        err_summary = "；".join(f"[{i.get('check')}] {i.get('message')}" for i in errors)
        rounds_log.append({"round": round_i, "error_count": len(errors),
                           "summary": err_summary or "无错误"})
        if not errors:
            return {"status": "ok", "rounds": rounds_log,
                    "mappings": current_mappings, "report": report,
                    "message": "转换验证通过"}

        # L1 规则修复（机械规则：剔除坏列映射）
        new_mappings, changed = _l1_fix_mappings(current_mappings)
        if changed:
            current_mappings = new_mappings
            logger.info("转换 L1 规则修复生效: 映射改动=True")
            continue

        # L2：LLM 决策修复
        decision = judge_transformation(report, context, past_issues)
        fix = decision.get("fix", {})
        action = fix.get("action", "fallback")
        logger.info("转换验证 Agent 决策: action=%s message=%s",
                    action, fix.get("message", ""))
        if action == "fix_mappings":
            new_ms = fix.get("fix", {}).get("mappings")
            if isinstance(new_ms, list) and new_ms:
                current_mappings = new_ms
        elif action == "drop_mapping":
            mid = fix.get("fix", {}).get("mapping_id")
            if mid:
                current_mappings = [m for m in current_mappings
                                    if m.get("id") != mid]

        # 经验沉淀
        exp = decision.get("experience") or {}
        if exp.get("pattern") and exp.get("resolution"):
            saved = pipeline_validator.save_validation_issue(
                exp["pattern"], exp["resolution"], source="trans-validate")
            logger.info("转换验证经验已沉淀: %s", saved.get("ok"))

    return {"status": "failed", "rounds": rounds_log,
            "mappings": current_mappings, "report": report,
            "message": "超过最大修复轮数仍存在问题"}

