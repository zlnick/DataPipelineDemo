"""AI 智能推荐 API（Agent A：数据转换 + Agent C1：转换验证-修复）。"""

import logging

from flask import Blueprint, request

from backend.services import domain_models, llm_client, repository, transformation_validator
from backend.utils import error, success

logger = logging.getLogger(__name__)

ai_bp = Blueprint("ai", __name__, url_prefix="/api/ai")


def _enrich_ai_semantics(assets: list[dict], targets: list[dict]) -> tuple[list[dict], list[dict]]:
    """把接口分析 Agent 的 AI 语义结论（资产/目标）补充进 Agent A 的上下文。

    只做上下文增强（assets/targets 载荷结构保持不变，缺省字段补全），不改变 LLM 决策。
    """
    asset_by_name = {}
    for a in repository.list_assets() or []:
        if a.get("name") and (a.get("ai_semantics") or a.get("description")):
            asset_by_name[str(a["name"]).lower()] = a
    assets = [dict(a) for a in assets]
    for a in assets:
        rec = asset_by_name.get(str(a.get("name", "")).lower())
        if not rec:
            continue
        a.setdefault("description", rec.get("ai_semantics") or rec.get("description") or "")
        a.setdefault("ai_semantics", rec.get("ai_semantics") or "")
        a.setdefault("key_hint", rec.get("key_hint") or "")

    # 目标（表/实体）语义：方向判定与说明来自目标记录（接口分析 Agent 写回）
    tg_by_table = {}
    for tg in repository.list_targets() or []:
        for tb in tg.get("tables") or []:
            nm = tb.get("entity_name") or tb.get("table") or ""
            if nm and (tb.get("ai_semantics") or tb.get("direction")):
                tg_by_table[str(nm).lower()] = tb
    targets = [dict(t) for t in targets]
    for t in targets:
        nm = t.get("table") or t.get("entity_name") or t.get("name") or ""
        rec = tg_by_table.get(str(nm).lower())
        if not rec:
            continue
        t.setdefault("ai_semantics", rec.get("ai_semantics") or "")
        t.setdefault("direction", rec.get("direction") or "")
        t.setdefault("ai_reason", rec.get("ai_reason") or "")
    return assets, targets


@ai_bp.post("/recommend")
def recommend():
    """Agent A：数据转换推荐（资产→目标表匹配 + 字段映射）。"""
    body = request.get_json(silent=True) or {}
    # 新分层模型优先；保留旧 assets/targets 请求格式。
    source_models = body.get("source_models") or []
    target_models = body.get("target_models") or []
    assets = body.get("assets") or source_models
    targets = body.get("targets") or target_models
    if not assets:
        return error("缺少资产列表（assets）"), 400
    if not targets:
        return error("缺少目标表列表（targets）"), 400

    try:
        assets, targets = _enrich_ai_semantics(assets, targets)
        result = llm_client.recommend_transformation(
            assets, targets, source_models=source_models or assets,
            target_models=target_models or targets)
    except llm_client.AgentError as exc:
        return error(str(exc)), 400
    except Exception as exc:
        logger.error("Agent A 推荐失败: %s", exc)
        return error(f"AI 推荐失败: {exc}"), 500

    recs = result.get("recommendations", [])
    # 归一化为映射结构（asset→source + 补 id），供 C1 验证-修复
    normalized = transformation_validator.normalize_recommendations(recs)
    trans_fix = transformation_validator.validate_and_fix_transformation(
        normalized, assets, target_models=target_models or targets)
    if trans_fix["status"] == "ok":
        normalized = trans_fix["mappings"]
    else:
        logger.warning("转换验证-修复未完全解决: %s", trans_fix["message"])
    # 返回时补回 asset 字段（前端 Recommend 页使用 rec.asset）
    out_recs = [{"asset": r.get("source", ""), **r} for r in normalized]
    transformation_plan = result.get("transformation_plan")
    if not isinstance(transformation_plan, dict) or not transformation_plan.get("mappings"):
        transformation_plan = {
            "source_models": source_models or assets,
            "target_models": target_models or targets,
            "mappings": normalized,
            "status": "validated" if trans_fix["status"] == "ok" else "draft",
        }
    return success({
        "recommendations": out_recs,
        "transformation_plan": transformation_plan,
        "count": len(out_recs),
        "validation": trans_fix.get("report", {}),
    }, "AI 推荐完成")


@ai_bp.post("/verify")
@ai_bp.post("/validate")
@ai_bp.post("/verify-facts")
def verify_recommendation():
    """对转换计划执行事实验证（不调用 LLM）。"""
    body = request.get_json(silent=True) or {}
    plan = body.get("transformation_plan") or body
    if not isinstance(plan, dict):
        return error("缺少 transformation_plan"), 400
    return success(domain_models.verify_transformation_plan(plan), "事实验证完成")
