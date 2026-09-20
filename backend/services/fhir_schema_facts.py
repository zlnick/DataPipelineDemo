# -*- coding: utf-8 -*-
"""目标 FHIR 组装 schema 的**事实注入**：每列的「是否编码列 / 源体系 / 体系来自源行哪列」。

## 为什么需要（2026-09-19 实测缺陷）
引擎 `demo.TransformProcess.LeafValue` 原先对**任何** CodeableConcept 列都做兜底：
「目标列无 system → 取源行的硬编码 `CodeSystem` 列」。而源行的 `CodeSystem` 是**整行共享**的：
  · `MedicationOrder` 行 = `urn:cn-nhsa:drug-nrdl` → 同一行的 `route`（"口服"，中文文本）
    被挂上了**药品目录**体系 ✗；
  · `Encounter` 表**没有** `CodeSystem` 列 → 国标码 `reasonCode`（E11.900）反而**没有** system ✗
    （与 `Condition.code` 同源国标码却口径不一致）。

## 修法（平台按事实决定、引擎只读）
生成期按四条事实计算每列：
  ① 映射里该目标列对应的**源列**；
  ② 源资产 `field_terms[源列].kind`（接口分析 AI 产出的事实：`coded_value` / `display_name` /
     `term_uri` / `plain`）→ 决定 `coded`；
  ③ 源表是否有 `CodeSystem` 列（`term_uri`）→ 有则 `system_from_row="CodeSystem"`（行值是事实）；
  ④ 若该列用了术语判定 Skill（`transform=term_map:<skill>`）→ `TERM_SKILLS[skill].source_system`
     （如 `cn2snomed → urn:cn-nhsa:icd10-gbt2016`）。
合并进 `^demo.Config("fhir","schema",<资源>)`；引擎仅在 schema 声明 `system_from_row` 时才取行值。
**不做任何 AI 决策**：只把已登记的事实搬到组装 schema 上（与"平台渲染 Production"同构）。
"""
from backend.services import repository, transform_directives as TD

FHIR_TYPES = ("fhir",)
CONCEPT_TYPES = ("codeableconcept", "concept", "coding")


def _tail(text) -> str:
    """取名称末段（`Encounter.ReasonCode` / `FHIR.Condition` → 末段；去引号）。"""
    s = str(text or "").strip().strip('"').strip("[]")
    return s.split(".")[-1] if "." in s else s


def source_field_terms(source_id: str | None) -> dict[str, dict]:
    """源资产的字段事实：{表名小写: {"terms": {列: field_term}, "has_codesystem": bool}}。"""
    out: dict[str, dict] = {}
    if not source_id:
        return out
    try:
        assets = repository.list_assets(str(source_id)) or []
    except Exception:  # noqa: BLE001 - 事实拿不到就不注入（引擎按"不猜"处理）
        return out
    for a in assets:
        if not isinstance(a, dict):
            continue
        name = _tail(a.get("name"))
        if not name:
            continue
        cols = [str((c or {}).get("name") or "") for c in
                ((a.get("structure") or {}).get("columns") or [])]
        out[name.lower()] = {
            "terms": {str(k): v for k, v in (a.get("field_terms") or {}).items()
                      if isinstance(v, dict)},
            "has_codesystem": any(c.lower() == "codesystem" for c in cols),
        }
    return out


def column_facts(resource_type: str, mappings: list[dict] | None,
                 terms_by_source: dict[str, dict] | None = None,
                 default_source_id: str | None = None) -> dict[str, dict]:
    """按映射 + 源字段事实，给出**本资源**每列的组装事实。

    `terms_by_source`：{source_id: 该源资产的字段事实索引}（见 `source_field_terms`）。

    ⚠ 2026-09-19 实测缺陷（**多源共存**）：一次生成会提交多组（不同数据源），若只用调用方传的
    单个 `source_id` 建索引 → 别组的映射会拿**错的源**的 field_terms 比对（实测：Clinic 的
    `MedicationOrder` 去比 FHIR 源的资产 → 全部判成"非编码列" → `coded=False`、system 丢失）。
    故此处**按每条映射自己的 `source_id`** 取事实索引（缺省回退 `default_source_id`）。

    返回 {目标列名: {"coded": bool, "system": str|None, "system_from_row": str|None}}；
    只返回"有判断依据"的列（常量指令 / 未映射列不返回 → 保留模型静态口径）。
    """
    want = _tail(resource_type).lower()
    terms_by_source = terms_by_source or {}
    facts: dict[str, dict] = {}
    for m in mappings or []:
        if not isinstance(m, dict):
            continue
        if _tail(m.get("target_table")).lower() != want:
            continue
        sid = str(m.get("source_id") or default_source_id or "").strip()
        tinfo = (terms_by_source.get(sid) or {}).get(_tail(m.get("source")).lower()) or {}
        terms = tinfo.get("terms") or {}
        for fm in (m.get("field_mappings") or []):
            if not isinstance(fm, dict):
                continue
            src = str(fm.get("source") or "").strip()
            col = _tail(fm.get("target"))
            if not (col and src):            # 常量指令（source 为空）→ 不改
                continue
            # 源列事实（接口分析 AI 产出的 field_terms / 源表是否有 CodeSystem 列 / 术语 Skill）
            term = terms.get(_tail(src)) or terms.get(src) or {}
            kind = str(term.get("kind") or "").strip().lower()
            skill = ""
            tr = str(fm.get("transform") or "").strip()
            if tr.startswith("term_map:"):
                skill = tr.split(":", 1)[1].strip()
            coded = (kind == "coded_value")
            fact = {"coded": coded, "system": None, "system_from_row": None}
            if coded:
                if tinfo.get("has_codesystem"):
                    # 行内 CodeSystem 列是"整行共享"的体系事实：只有平台显式声明本列用它才取行值
                    fact["system_from_row"] = "CodeSystem"
                else:
                    meta = TD.term_skill(skill) if skill else None
                    if meta and meta.get("source_system"):
                        fact["system"] = meta["source_system"]
            facts[col] = fact
    return facts


def apply_facts(schema: dict | None, facts: dict[str, dict] | None) -> dict:
    """把事实合并进模型 schema 的列（返回新 dict，不改原对象）。

    语义：
      · `coded` / `system_from_row` 总是按事实写入（引擎据此决定**是否**可用行内体系）；
      · `system` **只在事实给出值时**覆盖（未给出则保留模型静态体系，如
        `Condition.clinicalStatus` 的 condition-clinical、`Encounter.class_code` 的 v3-ActCode）。
    """
    if not isinstance(schema, dict):
        return schema or {}
    if not facts:
        return schema
    import copy as _copy
    out = _copy.deepcopy(schema)
    for c in out.get("columns") or []:
        if not isinstance(c, dict):
            continue
        f = facts.get(str(c.get("name") or ""))
        if isinstance(f, dict):
            c["coded"] = bool(f.get("coded"))
            c["system_from_row"] = f.get("system_from_row") or None
            if f.get("system"):
                c["system"] = f["system"]
    return out
