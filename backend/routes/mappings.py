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

    2026-09-15 补 FHIR：原先只认 SOAP、其余一律默认 "DB" —— FHIR 资源名的映射因此被标成 DB，
    既可能误路由，又让 `repository._mapping_identity`（含 target_type）认不出"同一条映射"
    （实测：重跑 AI 推荐时 Encounter→Encounter 被另存为 R2_2 而非复用 R2，映射重复堆积）。
    """
    soap_tables = set()
    fhir_tables = set()
    for tg in repository.list_targets():
        ttype = (tg.get("type") or "").upper()
        if ttype not in ("SOAP", "FHIR"):
            continue
        bucket = soap_tables if ttype == "SOAP" else fhir_tables
        for tb in tg.get("tables") or []:
            for key in ("table", "entity_name", "name"):
                if tb.get(key):
                    bucket.add(str(tb[key]).lower())
    for m in mappings:
        tbl = (m.get("target_table") or "").lower()
        if tbl in soap_tables:
            m["target_type"] = "SOAP"
        elif tbl in fhir_tables:
            m["target_type"] = "FHIR"
        elif not m.get("target_type"):
            m["target_type"] = "DB"


@mappings_bp.post("")
def save_mappings():
    """保存转换关系（用户确认 AI 推荐后）。

    请求体: {"mappings": [{"id": "M1", "source": "Patient", "target_table": "Patient",
                           "field_mappings": [{"source": "name[0].family", "target": "FamilyName"}]}],
             "mode": "merge"|"overwrite"}

    id 规则（见 repository.save_mappings）：同一 (源→目标) 身份重复保存 = 幂等更新（沿用原 id）；
    id 被别的转换关系占用时平台派生新 id（R1 → R1_2）而**不覆盖**——避免跨批次 AI 推荐撞号丢映射。
    mode=overwrite 时按给定 id 就地覆盖（显式的编辑语义）。
    """
    body = request.get_json(silent=True) or {}
    mappings = body.get("mappings") or []
    if not mappings:
        return error("缺少 mappings"), 400
    # 按目标类型补全 target_type（SOAP 目标实体 / DB 目标表）
    _enrich_target_types(mappings)
    overwrite = str(body.get("mode") or "").lower() == "overwrite" or bool(body.get("overwrite"))
    report: dict = {}
    count = repository.save_mappings(mappings, overwrite=overwrite, report=report)
    collapsed = report.get("collapsed") or []
    # 返回落库后的**权威 id**（可能与提交的 AI 推荐 id 不同：撞号时平台已改名）
    msg = "转换关系已保存"
    if collapsed:
        msg = "转换关系已保存（自动归并 %d 条同源同目标的重复记录：%s）" % (
            len(collapsed), "、".join(collapsed))
    return success({"count": count, "ids": [m.get("id") for m in mappings],
                    "collapsed": collapsed, "mappings": repository.list_mappings()}, msg)


@mappings_bp.get("")
def list_mappings():
    """转换关系列表。"""
    return success({"items": repository.list_mappings()})
