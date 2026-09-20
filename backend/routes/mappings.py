"""转换关系（Mapping）API。"""

import logging

from flask import Blueprint, request

from backend.services import mapping_source_norm, repository
from backend.utils import error, success

logger = logging.getLogger(__name__)

mappings_bp = Blueprint("mappings", __name__, url_prefix="/api/mappings")


def _asset_source_index() -> tuple[dict[str, str], dict[str, list[str]]]:
    """资产 ID / 资产名 / 末段 → 所属数据源 id（键统一小写）；**重名跨源则不进索引**。

    2026-09-17 修缺陷 A：映射身份需要**数据源维度**，`source_id` 由资产反查回填。

    2026-09-19 修「同名表跨库被静默错归属」（本轮实测）：两个数据源都有 `SQLUser.Patient`
    时，原实现用 `setdefault`（first-wins）—— 名字 `patient` 在索引里只剩**第一个**数据源，
    于是一条没带 `source_id` 的 `Patient` 映射会被**静默**回填成另一个源的 id（实测：应属
    模拟HIS(Clinic) 的 Patient 被填成测试SQLDB(USER)）→ 该组缺患者主表 → sql2fhir 布局推导
    500。现改为：**一个名字对应多个数据源时该键不参与反查**（不猜、不静默），歧义清单单独
    返回给调用方显式报告；唯一命名的资产照旧回填（无回归）。
    """
    cands: dict[str, set[str]] = {}
    try:
        for ds in repository.list_datasources() or []:
            dsid = str(ds.get("id") or "").strip()
            if not dsid:
                continue
            for a in repository.list_assets(dsid) or []:
                if not isinstance(a, dict):
                    continue
                for k in (a.get("id"), a.get("name"),
                          str(a.get("id") or "").split(".")[-1],
                          str(a.get("name") or "").split(".")[-1]):
                    key = str(k or "").strip().lower()
                    if key:
                        cands.setdefault(key, set()).add(dsid)
    except Exception as exc:  # noqa: BLE001 - 索引构建失败仅导致少回填 source_id
        logger.warning("资产→数据源索引构建失败: %s", exc)
    idx = {k: next(iter(v)) for k, v in cands.items() if len(v) == 1}
    ambiguous = {k: sorted(v) for k, v in cands.items() if len(v) > 1}
    return idx, ambiguous


def _backfill_source_ids(mappings: list[dict]) -> tuple[int, list[str], dict[str, list[str]]]:
    """给**未声明数据源**的映射回填 `source_id`，返回 (回填条数, 未解析的源名, 歧义源名)。

    缺陷 A（2026-09-17）：3 个源有同名资产（`Patient`）时，身份必须含数据源维度才不会互相
    覆盖。前端/脚本提交的映射通常会带 `source_id`（`/ai/recommend` 已回填），此入口对
    "直接调 API 存映射"（历史脚本、手工编辑）的情况兜底：按 `source`（资产 id/名/末段）
    反查数据源。

    2026-09-19：反查不到**不猜测、不静默**——名字唯一时回填；**重名跨源**时**留空**并进
    `ambiguous`（附候选数据源），由响应/日志显式回报调用方，避免"静默错归属"（本轮实测：
    Clinic 的 `Patient` 被静默填成 USER 源 → 生成 500）。
    """
    idx, amb_idx = _asset_source_index()
    filled = 0
    unresolved: list[str] = []
    ambiguous: dict[str, list[str]] = {}
    for m in mappings:
        if not isinstance(m, dict):
            continue
        if any(str(m.get(k) or "").strip() for k in ("source_id", "datasource_id")):
            continue
        raw = str(m.get("source") or m.get("asset") or m.get("source_table") or "").strip()
        if not raw:
            continue
        dsid = idx.get(raw.lower()) or idx.get(raw.split(".")[-1].lower()) or ""
        if dsid:
            m["source_id"] = dsid
            filled += 1
            continue
        cands = (amb_idx.get(raw.lower()) or amb_idx.get(raw.split(".")[-1].lower()))
        if cands:
            # 重名跨源：不猜。留空（保存后该映射按"未声明数据源"口径），并显式回报候选
            ambiguous[raw] = cands
        else:
            unresolved.append(raw)
    return filled, unresolved, ambiguous


def _target_name_sets() -> tuple[set[str], set[str], set[str]]:
    """按已登记目标归集名字集合：(SOAP 实体, FHIR 实体, DB 表) —— 全部小写。"""
    soap: set[str] = set()
    fhir: set[str] = set()
    db: set[str] = set()
    for tg in repository.list_targets():
        ttype = (tg.get("type") or "").upper()
        if ttype not in ("SOAP", "FHIR", "DB"):
            continue
        bucket = soap if ttype == "SOAP" else (fhir if ttype == "FHIR" else db)
        for tb in tg.get("tables") or []:
            # FHIR 目标行用 entity_name/table，DB 目标行用 table —— 三个键都认
            for key in ("table", "entity_name", "name"):
                if tb.get(key):
                    bucket.add(str(tb[key]).lower())
    return soap, fhir, db


def _enrich_target_types(mappings: list[dict]) -> None:
    """按目标类型自动补全 mapping.target_type（就地修改）。

    背景：前端确认 AI 推荐时 mapping 不带 target_type，schemas 默认成 "DB"，
    导致 SOAP 目标的转换关系在 TransformProcess 路由时误走 DB 分支
    （向不存在的 SQLOp_{表} 投递，报 ErrBusinessDispatchNameNotRegistered）。
    此处依据已登记的 SOAP 目标的实体/表名匹配目标类型并回填。

    2026-09-15 补 FHIR：原先只认 SOAP、其余一律默认 "DB" —— FHIR 资源名的映射因此被标成 DB，
    既可能误路由，又让 `repository._mapping_identity`（含 target_type）认不出"同一条映射"
    （实测：重跑 AI 推荐时 Encounter→Encounter 被另存为 R2_2 而非复用 R2，映射重复堆积）。

    2026-09-16 修 F（同名实体跨类型 → 类型被改判 + 映射被静默覆盖）：实体名是**跨类型重名**的
    —— `Patient` 既是 FHIR 目标的实体名，也是 DB 目标（CLINIC.Patient）的表名。原实现无条件按名字
    推断并**覆盖**已声明类型，于是 DB 目标的映射被改判成 FHIR，进而与 FHIR 目标的 `Patient→Patient`
    推荐**身份相同**（源末段/目标末段/类型一致）→ 保存时后者覆盖前者，DB 映射内容被静默丢弃
    （实测：`R1` 落库成 FHIR 的 8 个字段、DB 的 9 个字段消失）。
    现规则：① 已声明类型（平台 `/ai/recommend` 归一结果或前端显式选择）→ **尊重**，仅在
    "声明 DB 但目标库里没有这张表、且名字命中 SOAP/FHIR 实体"这类**明显矛盾**时纠正；
    ② 未声明类型 → 按 SOAP → FHIR → DB 顺序推断（保持历史口径）。
    """
    soap_tables, fhir_tables, db_tables = _target_name_sets()
    for m in mappings:
        tbl = (m.get("target_table") or "").lower()
        # 带 schema 前缀（SOAP.PatientEntity / FHIR.Patient）时按末段再匹配一次：
        # Agent A 的输出可能带前缀，而注册实体名是裸名（2026-09-16 实测）。
        tail = tbl.split(".")[-1]
        names = {tbl, tail} - {""}
        in_soap = bool(names & soap_tables)
        in_fhir = bool(names & fhir_tables)
        in_db = bool(names & db_tables)
        claimed = str(m.get("target_type") or "").upper()
        if claimed:
            # 明显矛盾才纠正：声明 DB，但没有任何 DB 目标存在这张表，而名字命中 SOAP/FHIR 实体
            if claimed == "DB" and not in_db and (in_soap or in_fhir):
                m["target_type"] = "SOAP" if in_soap else "FHIR"
            continue
        if in_soap:
            m["target_type"] = "SOAP"
        elif in_fhir:
            m["target_type"] = "FHIR"
        else:
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
    # 数据源维度回填（缺陷 A）：身份 = (数据源, 源, 目标, 类型) —— 未声明数据源时按资产反查补上，
    # 否则 3 个源的同名资产（Patient）会被判成同一条转换关系而互相覆盖（9 条只剩 3 条）。
    # ⚠ 2026-09-19：**重名跨源时不猜**（留空 + 显式回报 source_ambiguous），避免静默错归属。
    source_filled, source_unresolved, source_ambiguous = _backfill_source_ids(mappings)
    if source_unresolved:
        logger.warning("以下映射无法解析数据源（source_id 留空，将按\"未声明数据源\"口径保存）: %s",
                       "、".join(source_unresolved))
    if source_ambiguous:
        logger.warning("以下映射的源名在多个数据源间重名，**放弃猜测**（source_id 留空；"
                       "请在提交时显式带 source_id，或在对应源下重新做 AI 匹配）: %s", source_ambiguous)
    # 字段路径口径归一（入口收口）：把 source 里的 schema/库前缀剥掉（`SQLUser.Patient.ID` →
    # `Patient.ID`），concat 参数递归处理——只规整写法，**不改动映射了哪个字段**（AI 决策不变）。
    try:
        _src_assets = repository.list_source_assets()
    except Exception as exc:  # noqa: BLE001 - 资产读取失败不影响保存（前缀集合退化为默认表）
        logger.warning("读取源资产失败（口径归一按默认前缀）: %s", exc)
        _src_assets = []
    norm_cnt = mapping_source_norm.normalize_mappings(mappings, _src_assets)
    overwrite = str(body.get("mode") or "").lower() == "overwrite" or bool(body.get("overwrite"))
    report: dict = {}
    count = repository.save_mappings(mappings, overwrite=overwrite, report=report)
    collapsed = report.get("collapsed") or []
    # 返回落库后的**权威 id**（可能与提交的 AI 推荐 id 不同：撞号时平台已改名）
    msg = "转换关系已保存"
    if norm_cnt:
        msg += "（已归一 %d 条字段路径写法：去掉 schema 前缀）" % norm_cnt
    if collapsed:
        msg = "转换关系已保存（自动归并 %d 条同源同目标的重复记录：%s）" % (
            len(collapsed), "、".join(collapsed))
    if source_ambiguous:
        msg += "（%d 条映射的源名在多个数据源间重名，未猜测其数据源，请显式指定）" % len(source_ambiguous)
    return success({"count": count, "ids": [m.get("id") for m in mappings],
                    "source_normalized": norm_cnt,
                    "source_backfilled": source_filled,
                    "source_unresolved": source_unresolved,
                    # 重名跨源 → 未回填 source_id 的映射（名字 → 候选数据源），供调用方显式补正
                    "source_ambiguous": source_ambiguous,
                    "collapsed": collapsed, "mappings": repository.list_mappings()}, msg)


@mappings_bp.get("")
def list_mappings():
    """转换关系列表。"""
    return success({"items": repository.list_mappings()})
