"""转换关系（Mapping）API。"""

from flask import Blueprint, request

from backend.services import repository
from backend.utils import error, success

mappings_bp = Blueprint("mappings", __name__, url_prefix="/api/mappings")


def _enrich_target_types(mappings: list[dict]) -> None:
    """按目标类型自动补全 mapping.target_type（就地修改）。

    背景：前端确认 AI 推荐时 mapping 不带 target_type，schemas 默认成 "DB"，
    导致 SOAP 目标的转换关系在 TransformProcess 路由时误走 DB 分支
    （向不存在的 SQLOp_{表} 投递，报 ErrBusinessDispatchNameNotRegistered）。
    此处依据已登记的 SOAP 目标的实体/表名匹配目标类型并回填。
    """
    soap_tables = set()
    for tg in repository.list_targets():
        if (tg.get("type") or "").upper() != "SOAP":
            continue
        for tb in tg.get("tables") or []:
            for key in ("table", "entity_name", "name"):
                if tb.get(key):
                    soap_tables.add(str(tb[key]).lower())
    for m in mappings:
        tbl = (m.get("target_table") or "").lower()
        if tbl in soap_tables:
            m["target_type"] = "SOAP"
        elif not m.get("target_type"):
            m["target_type"] = "DB"


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
    # 按目标类型补全 target_type（SOAP 目标实体 / DB 目标表）
    _enrich_target_types(mappings)
    count = repository.save_mappings(mappings)
    return success({"count": count, "mappings": repository.list_mappings()}, "转换关系已保存")


@mappings_bp.get("")
def list_mappings():
    """转换关系列表。"""
    return success({"items": repository.list_mappings()})
