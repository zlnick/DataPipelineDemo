"""AI 智能推荐 API（Agent A：数据转换 + Agent C1：转换验证-修复）。"""

import logging

from flask import Blueprint, request

from backend.services import llm_client, transformation_validator
from backend.utils import error, success

logger = logging.getLogger(__name__)

ai_bp = Blueprint("ai", __name__, url_prefix="/api/ai")


@ai_bp.post("/recommend")
def recommend():
    """Agent A：数据转换推荐（资产→目标表匹配 + 字段映射）。"""
    body = request.get_json(silent=True) or {}
    assets = body.get("assets") or []
    targets = body.get("targets") or []
    if not assets:
        return error("缺少资产列表（assets）"), 400
    if not targets:
        return error("缺少目标表列表（targets）"), 400

    try:
        result = llm_client.recommend_transformation(assets, targets)
    except llm_client.AgentError as exc:
        return error(str(exc)), 400
    except Exception as exc:
        logger.error("Agent A 推荐失败: %s", exc)
        return error(f"AI 推荐失败: {exc}"), 500

    recs = result.get("recommendations", [])
    # 归一化为映射结构（asset→source + 补 id），供 C1 验证-修复
    normalized = transformation_validator.normalize_recommendations(recs)
    trans_fix = transformation_validator.validate_and_fix_transformation(normalized, assets)
    if trans_fix["status"] == "ok":
        normalized = trans_fix["mappings"]
    else:
        logger.warning("转换验证-修复未完全解决: %s", trans_fix["message"])
    # 返回时补回 asset 字段（前端 Recommend 页使用 rec.asset）
    out_recs = [{"asset": r.get("source", ""), **r} for r in normalized]
    return success({
        "recommendations": out_recs,
        "count": len(out_recs),
        "validation": trans_fix.get("report", {}),
    }, "AI 推荐完成")
