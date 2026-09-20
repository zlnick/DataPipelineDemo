"""AI 智能推荐 API（Agent A：数据转换 + Agent C1：转换验证-修复）。"""

import logging

from flask import Blueprint, request

from backend.services import (db_target_keys, domain_models, llm_client, repository,
                              transformation_validator)
from backend.utils import error, success

logger = logging.getLogger(__name__)

ai_bp = Blueprint("ai", __name__, url_prefix="/api/ai")


def _enrich_ai_semantics(assets: list[dict], targets: list[dict]) -> tuple[list[dict], list[dict]]:
    """把接口分析 Agent 的 AI 语义结论（资产/目标）补充进 Agent A 的上下文。

    只做上下文增强（assets/targets 载荷结构保持不变，缺省字段补全），不改变 LLM 决策。
    """
    asset_by_name = {}
    for a in repository.list_assets() or []:
        if a.get("name"):
            asset_by_name[str(a["name"]).lower()] = a
    assets = [dict(a) for a in assets]
    for a in assets:
        rec = asset_by_name.get(str(a.get("name", "")).lower())
        if not rec:
            continue
        a.setdefault("description", rec.get("ai_semantics") or rec.get("description") or "")
        a.setdefault("ai_semantics", rec.get("ai_semantics") or "")
        a.setdefault("key_hint", rec.get("key_hint") or "")
        if rec.get("field_terms"):
            a.setdefault("field_terms", rec["field_terms"])

    # 目标（表/实体）语义与字段术语：来自目标记录（接口分析 Agent 写回 + 模型列结构化元数据）
    tg_by_table = {}
    ft_by_table: dict[str, dict] = {}
    for tg in repository.list_targets() or []:
        for tb in tg.get("tables") or []:
            nm = tb.get("entity_name") or tb.get("table") or ""
            if not nm:
                continue
            key = str(nm).lower()
            if tb.get("ai_semantics") or tb.get("direction"):
                tg_by_table[key] = tb
            cols = tb.get("columns") or []
            if cols and key not in ft_by_table:
                ft = {}
                for c in cols:
                    if not isinstance(c, dict) or not c.get("name"):
                        continue
                    ft[c["name"]] = {
                        "type": c.get("type") or "",
                        "path": c.get("path") or "",
                        "system": c.get("system") or "",
                        "note": c.get("note") or "",
                    }
                if ft:
                    ft_by_table[key] = ft
    targets = [dict(t) for t in targets]
    for t in targets:
        nm = t.get("table") or t.get("entity_name") or t.get("name") or ""
        key = str(nm).lower()
        rec = tg_by_table.get(key)
        if rec:
            t.setdefault("ai_semantics", rec.get("ai_semantics") or "")
            t.setdefault("direction", rec.get("direction") or "")
            t.setdefault("ai_reason", rec.get("ai_reason") or "")
        if ft_by_table.get(key):
            t.setdefault("field_terms", ft_by_table[key])
    # 目标表主键事实（DB 目标）：IRIS 的 INSERT 要求主键非空，主键列是 Agent A 必须覆盖的硬约束
    # （2026-09-17 实测缺陷：Agent A 把源主键 ID 映射到 MRN 却漏掉目标主键 ID → 运行期 "ID is required"）。
    # 事实来源：已登记记录的 key_columns → JDBC 元数据现场探查（并回写登记），拿不到则不注入。
    for t in targets:
        if t.get("key_columns"):
            continue
        keys = db_target_keys.key_columns_of(
            str(t.get("table") or t.get("entity_name") or t.get("name") or ""),
            str(t.get("type") or t.get("target_type") or "DB"),
            str(t.get("schema") or ""))
        if keys:
            t["key_columns"] = keys
    return assets, targets


def _inherit_keys(target_models: list[dict], targets: list[dict]) -> list[dict]:
    """把 targets 上的 key_columns 同步到同名的 target_models（Agent A 同时读两者）。

    只补缺省值，不覆盖调用方已给出的主键事实（参数化 + 完整性补齐，不改变 LLM 决策）。
    """
    by_name: dict[str, list[str]] = {}
    for t in targets or []:
        nm = str(t.get("table") or t.get("entity_name") or t.get("name") or "").lower()
        if nm and t.get("key_columns"):
            by_name.setdefault(nm, t["key_columns"])
    out: list[dict] = []
    for m in target_models or []:
        rec = dict(m)
        nm = str(rec.get("table") or rec.get("entity_name") or rec.get("name") or "").lower()
        if not rec.get("key_columns") and by_name.get(nm):
            rec["key_columns"] = by_name[nm]
        out.append(rec)
    return out


def _asset_name_index(assets: list[dict]) -> dict[str, str]:
    """建立 资产 ID / 资产名 / 末段表名 → **规范资产名** 的索引（键统一小写）。

    2026-09-16 缺陷 N11：`mapping.source` 有两种口径 —— Agent A 可能回填**资产 ID**
    （`DS71120_TSQLUser.Patient`）也可能回填**资产名**（`Patient`），而下游（前端按资产名选源
    数据源、`_resolve_sql_source_tables`/sql2fhir 布局按表名取列与 key_hint）都按资产名匹配，
    口径不一致会让管道组 source_id 丢失，最终 sql2fhir 布局推导 500「患者主表未能判定」。
    """
    idx: dict[str, str] = {}
    for a in assets or []:
        nm = str(a.get("name") or "").strip()
        if not nm:
            continue
        for k in (a.get("id"), a.get("name"), str(a.get("id") or "").split(".")[-1]):
            key = str(k or "").strip().lower()
            if key:
                idx.setdefault(key, nm)
    return idx


def _canonical_source(idx: dict[str, str], raw: str) -> str:
    """把映射 source 归一到**资产名**（命中不了则原样返回，保持参数化宽容）。"""
    s = str(raw or "").strip()
    if not s:
        return s
    return idx.get(s.lower()) or idx.get(s.split(".")[-1].lower()) or s


def _asset_source_index(assets: list[dict], registry_fallback: bool = False,
                        report: dict | None = None) -> dict[str, str]:
    """建立 资产 ID / 资产名 / 末段表名 → **所属数据源 id** 的索引（键统一小写）。

    2026-09-17 修缺陷 A：映射身份必须带**数据源维度**，否则 3 个源的同名资产（`Patient`）
    会被判成同一条转换关系而互相覆盖。资产载荷（UI `Recommend.vue` / 落库资产记录）里
    带 `source_id`，此处把它按 id/name/末段三键索引出来，供归一化时回填到推荐结果上。

    registry_fallback=True 时补查**资产登记**（`^demo.SourceAsset`）——覆盖"调用方给的资产
    没带 source_id"的情况。

    2026-09-19 修「同名表跨库被静默错归属」（本轮实测：两个数据源都有 `SQLUser.Patient`，
    名字 `patient` 在索引里被 `setdefault` 静默压成**第一个**源 → 属于模拟HIS(Clinic) 的
    Patient 映射被填成测试SQLDB(USER) → 该组缺患者主表 → sql2fhir 布局推导 500）。规则：
      ① **请求资产优先**（= 用户在界面选中的那个源，权威）；② 登记兜底**只补请求没覆盖的键**；
      ③ 任一层的键若对应**多个不同数据源** → **该键不进索引**（不猜、不静默），歧义清单经
      `report["ambiguous"]` 回给调用方并在日志说明。唯一命名的键照旧回填（无回归）。
    """

    def _collect(rows, cands: dict[str, set[str]]) -> None:
        for a in rows or []:
            if not isinstance(a, dict):
                continue
            ds = str(a.get("source_id") or a.get("datasource_id") or "").strip()
            if not ds:
                continue
            for k in (a.get("id"), a.get("name"),
                      str(a.get("id") or "").split(".")[-1],
                      str(a.get("name") or "").split(".")[-1]):
                key = str(k or "").strip().lower()
                if key:
                    cands.setdefault(key, set()).add(ds)

    req_cands: dict[str, set[str]] = {}
    reg_cands: dict[str, set[str]] = {}
    _collect(assets, req_cands)
    if registry_fallback:
        try:
            _collect(repository.list_source_assets() or [], reg_cands)
        except Exception as exc:  # noqa: BLE001 - 兜底索引失败不影响主流程
            logger.debug("资产登记索引补查失败: %s", exc)

    idx: dict[str, str] = {}
    ambiguous: dict[str, list[str]] = {}
    for k, v in req_cands.items():            # ① 请求资产优先
        if len(v) == 1:
            idx[k] = next(iter(v))
        else:
            ambiguous[k] = sorted(v)
    for k, v in reg_cands.items():            # ② 登记兜底：只补空缺，重名跨源不猜
        if k in idx:
            continue
        if len(v) == 1:
            idx[k] = next(iter(v))
        elif k not in ambiguous:
            ambiguous[k] = sorted(v)
    if ambiguous:
        logger.info("资产名在多个数据源间重名，已放弃猜测（不再静默取第一个）: %s", ambiguous)
        if report is not None:
            report.setdefault("ambiguous", {}).update(ambiguous)
    return idx


def _target_index(models: list[dict]) -> dict[str, tuple[str, str]]:
    """建立 目标实体名 → (规范实体名, 目标类型) 索引（键统一小写）。

    Agent A（LLM）经常把目标写成**带 schema 前缀**的形式（实测 `SQLUser.Patient` /
    `FHIR.Patient` / `SOAP.PatientEntity`），而平台注册表里实体名是裸名（`Patient` /
    `PatientEntity`）。只按裸名匹配会连环出问题：
      ① 无法回填 `target_type` → 前端 `typeByTable[...] || 'DB'` 与后端 `_enrich_target_types`
         都兜底成 DB → FHIR/SOAP 目标被误路由（曾实测 R1_3/R2_3/R4_2 全标 DB）；
      ② 校验报假错「目标表/实体 X 不存在或列结构为空」；
      ③ 前端 Pipelines 分组 `tgtByTable[target_table]` 匹配不到 → 分组 target_id 缺失。
    故此处按 **<schema>.<entity> 精确键**（优先，可消歧同名实体如 DB.Patient vs FHIR.Patient）
    与 **裸实体名**（唯一时）双键登记，把 AI 输出归一回注册实体名并带出类型。
    """
    idx: dict[str, tuple[str, str]] = {}
    for m in models or []:
        name = str(m.get("table") or m.get("entity_name") or m.get("name") or "").strip()
        if not name:
            continue
        ttype = str(m.get("type") or "").strip().upper() or ("SOAP" if m.get("bo_class") else "")
        schema = str(m.get("schema") or "").strip()
        if schema:
            idx.setdefault(("%s.%s" % (schema, name)).lower(), (name, ttype))
        # 裸名键：同名实体只保留第一个（带 schema 的精确键负责消歧）
        idx.setdefault(name.lower(), (name, ttype))
    return idx


def _canonical_target(idx: dict[str, tuple[str, str]], raw: str,
                      raw_type: str = "") -> tuple[str, str]:
    """把映射 target_table 归一到**注册实体名**，并返回目标类型（命中不了则原样返回）。"""
    s = str(raw or "").strip()
    if not s:
        return s, str(raw_type or "").strip().upper()
    hit = idx.get(s.lower()) or idx.get(s.split(".")[-1].lower())
    if not hit:
        return s, str(raw_type or "").strip().upper()
    name, ttype = hit
    # 目标类型以**注册实体**为准：实体名跨类型重名（DB 的 Patient vs FHIR 的 Patient）时，
    # 只有注册侧知道"这次是哪个目标"；AI 的声明仅作索引未带类型时的兜底（2026-09-16 修 F）。
    return name, (ttype or str(raw_type or "").strip().upper())



def _canonicalize_recs(recs: list[dict], assets: list[dict], source_models: list[dict],
                       target_models: list[dict], targets: list[dict],
                       report: dict | None = None) -> list[dict]:
    """把推荐结果的 source/target_table 归一到**注册口径**，并回填 target_type。

    源侧（缺陷 N11）：LLM 可能回填资产 ID（`DS71120_TSQLUser.Patient`）或带 schema 前缀的表名，
    而前端按资产名选源数据源、布局推导按表名取列 → 口径不一致会让 source_id 丢失（500）。
    目标侧（同名跨类型）：LLM 常回带 schema 前缀的目标名（SQLUser.Patient / FHIR.Patient /
    SOAP.PatientEntity），按裸名匹配不到会被兜底成 DB（误路由）→ 归一到注册实体名并带出真实类型。

    ⚠ 本函数须在 C1 验证-修复**之前**调用：C1 的 DB 主键 / FHIR 必填检查都按 `target_type` 分域。
    """
    out: list[dict] = []
    src_name_idx = _asset_name_index([*(assets or []), *(source_models or [])])
    # 数据源维度索引（缺陷 A）：映射必须带 source_id，否则 3 个源的同名资产（Patient）
    # 在保存时被判成同一条转换关系而互相覆盖（9 条只剩 3 条）。
    src_ds_idx = _asset_source_index([*(assets or []), *(source_models or [])],
                                    registry_fallback=True, report=report)
    tgt_idx = _target_index([*(target_models or []), *(targets or [])])
    if not tgt_idx:
        try:
            tgt_idx = _target_index([
                {"table": tb.get("entity_name") or tb.get("table"), "schema": tb.get("schema"),
                 "type": tg.get("type")}
                for tg in repository.list_targets() or [] for tb in (tg.get("tables") or [])])
        except Exception as exc:  # noqa: BLE001 - 索引构建失败不影响主流程
            logger.debug("目标索引构建失败: %s", exc)
    for r in recs or []:
        if not isinstance(r, dict):
            continue
        if r.get("source"):
            r["source"] = _canonical_source(src_name_idx, r["source"])
            # 数据源维度：以**资产登记**为准（缺陷 A）——AI 不需要知道自己来自哪个数据源，
            # 平台按归一后的资产名回填 source_id；命中不了则保持原样（下游保存入口再兜底一次）。
            ds = (src_ds_idx.get(str(r["source"]).lower())
                  or src_ds_idx.get(str(r["source"]).split(".")[-1].lower()) or "")
            if ds:
                r["source_id"] = ds
        if r.get("target_table"):
            _tbl, _ttype = _canonical_target(tgt_idx, r.get("target_table"), r.get("target_type"))
            r["target_table"] = _tbl
            if _ttype:
                r["target_type"] = _ttype
        out.append(r)
    return out


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
        # 主键事实（DB 目标）同步进 target_models —— Agent A 同时读 targets/target_models，
        # 只有两边一致才能保证"必须覆盖主键列"的约束被 LLM 看到（事实注入，不改变决策）
        target_models = _inherit_keys(target_models, targets)
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
    # 数据源维度回填的**歧义审计**（2026-09-19）：重名跨源的名字不参与反查 → 在此收集，
    # 出口回给调用方（UI/脚本）显式提示"这条映射的源数据源需显式指定"。
    _src_report: dict = {}
    # ⚠ 口径归一 **必须在 C1 之前**：C1 的 DB 主键检查（`_db_key_issues`）与 FHIR 必填检查都按
    #   mapping 的 `target_type` 分域——若此时类型还缺失/带 schema 前缀，同名跨类型（DB.Patient vs
    #   FHIR.Patient）会被拿错域的事实校验（实测过这类"按名字匹配"缺陷）。故先归一，再验证-修复。
    normalized = _canonicalize_recs(normalized, assets, source_models,
                                    target_models, targets, report=_src_report)
    trans_fix = transformation_validator.validate_and_fix_transformation(
        normalized, assets, target_models=target_models or targets)
    if trans_fix["status"] == "ok":
        normalized = trans_fix["mappings"]
    else:
        # 采纳 C1 的部分修复（不丢弃已修正的映射，如引用标注 transform=reference），
        # 否则前端保存的会是未修复版本，生成阶段又被 FHIR 校验拒绝
        if trans_fix.get("mappings"):
            normalized = trans_fix["mappings"]
        logger.warning("转换验证-修复未完全解决（已采纳部分修复）: %s", trans_fix["message"])
    # C1 可能改写/追加映射条目（含 target_table/target_type）→ 再归一一次，保证出口口径一致
    src_name_idx = _asset_name_index([*(assets or []), *(source_models or [])])
    # C1 也可能重建映射字典（丢失未知键）→ 出口再回填一次数据源维度（缺陷 A）
    src_ds_idx2 = _asset_source_index([*(assets or []), *(source_models or [])],
                                      registry_fallback=True, report=_src_report)
    for r in normalized:
        if not isinstance(r, dict) or not r.get("source"):
            continue
        r["source"] = _canonical_source(src_name_idx, r["source"])
        ds = (src_ds_idx2.get(str(r["source"]).lower())
              or src_ds_idx2.get(str(r["source"]).split(".")[-1].lower()) or "")
        if ds:
            r["source_id"] = ds
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
        # 重名跨源的名字 → 候选数据源（未猜测；调用方需显式指定 source_id）——审计与 UI 提示用
        "source_ambiguous": _src_report.get("ambiguous") or {},
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
