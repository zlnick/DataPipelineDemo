"""转换关系（Mapping）API。"""

from flask import Blueprint, request

from backend.services import repository
from backend.utils import error, success

mappings_bp = Blueprint("mappings", __name__, url_prefix="/api/mappings")


@mappings_bp.post("")
def save_mappings():
    """保存转换关系（用户确认 AI 推荐后）。

    请求体: {"mappings": [{"id": "M1", "source": "Patient", "target_table": "Patient",
                           "field_mappings": [{"source": "name[0].family", "target": "FamilyName"}]}]}
    """
    body = request.get_json(silent=True) or {}
    mappings = body.get("mappings") or []
    if not mappings:
        return error("缺少 mappings"), 400
    count = repository.save_mappings(mappings)
    return success({"count": count, "mappings": repository.list_mappings()}, "转换关系已保存")


@mappings_bp.get("")
def list_mappings():
    """转换关系列表。"""
    return success({"items": repository.list_mappings()})
