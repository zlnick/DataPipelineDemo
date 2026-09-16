# -*- coding: utf-8 -*-
"""sql2fhir-patient-tx Skill executor：数据驱动推导患者级事务布局。
输入=已确认 mapping + 源资产列/外键/key_hint；推导=患者主表、查询 BO 布局、聚合顺序与引用。
规则见 pipeline_design_skills.sql2fhir-patient-tx.derivation；推导不出即抛 Sql2FhirDeriveError。"""
import re

from backend.services import fhir_target_model

_PATIENT_FK = {"patientid", "patient_id", "mrn"}
_ENCOUNTER_FK = {"encounterid", "encounter_id", "visitid", "visit_id", "admissionid"}
_PK_HINTS = {"id", "mrn", "pid"}


class Sql2FhirDeriveError(ValueError):
    """布局推导失败（输入不足/不一致）。"""


def model_reference_map() -> dict[str, list[tuple[str, str]]]:
    """扫描已建模模型 Reference(X) 列 → {资源: [(字段, 目标资源)]}（US Core 口径）。"""
    out = {}
    for rt, model in fhir_target_model.US_CORE_RESOURCE_MODELS.items():
        refs = []
        for c in model.get("columns", []):
            ty = c.get("type", "")
            if isinstance(ty, str) and ty.startswith("Reference("):
                refs.append((c.get("name", ""), ty[len("Reference("):-1]))
        if refs:
            out[rt] = refs
    return out


def _canonical(name: str, known: set[str]) -> str | None:
    if not name:
        return None
    low = name.strip().lower()
    for k in known:
        if k.lower() == low:
            return k
    return None


def _table_from_source(source: str) -> str:
    return (source or "").strip().split(".")[-1]


def _column_names(meta) -> set[str]:
    cols = []
    if isinstance(meta, dict):
        cols = meta.get("columns") or []
    elif isinstance(meta, list):
        cols = meta
    return {c if isinstance(c, str) else str((c or {}).get("name", "")) for c in cols if c}


def normalize_mappings(mappings: list[dict]) -> list[dict]:
    """规整 mapping → [{source_table, target_resource}]（仅已建模 FHIR 资源）。

    同一 (源表, 目标资源) 只保留一条：映射登记可能因多轮 AI 推荐产生**重复身份**的条目
    （实测：Encounters→Encounter 被另存为 R2 与 R3_2 两条），若原样传入布局推导会生成
    重复的查询 BO 与重复的 Bundle entry（resource_order 出现 Encounter ×2），
    子表被查两遍、条目被投两遍 —— 语义上仍是一条转换关系，故此处去重。
    """
    known = set(fhir_target_model.US_CORE_RESOURCE_MODELS)
    out = []
    seen: set[tuple[str, str]] = set()
    for m in mappings or []:
        src = _table_from_source(str(m.get("source") or ""))
        tgt = _canonical(str(m.get("target_table") or m.get("target") or ""), known)
        if src and tgt and (src, tgt) not in seen:
            seen.add((src, tgt))
            out.append({"source_table": src, "target_resource": tgt})
    return out


def derive_sql2fhir_layout(mappings: list[dict], source_meta: dict) -> dict:
    """推导 sql2fhir 布局。source_meta: {表: {columns:[], key_hint:str?}}"""
    norm = normalize_mappings(mappings)
    if not norm:
        raise Sql2FhirDeriveError(
            "没有可推导的 mapping：请确认源表已映射到 FHIR 已建模目标资源"
            "（Patient/Encounter/Condition/MedicationRequest 等）")
    ref_map = model_reference_map()

    def cols(t):
        meta = (source_meta or {}).get(t)
        if meta is None:
            raise Sql2FhirDeriveError(f"源资产缺少表 {t} 的列结构，无法推导 BO 查询参数")
        return _column_names(meta)

    def hint(t):
        h = ((source_meta or {}).get(t) or {}).get("key_hint")
        return h.strip() if isinstance(h, str) and h.strip() else None

    def real(t, low):
        return next(c for c in cols(t) if c.lower() == low)

    cands = [n for n in norm if n["target_resource"] == "Patient"]
    if not cands:
        raise Sql2FhirDeriveError(
            "mapping 中没有目标为 Patient 的源表，无法确定患者主表（sql2fhir 以患者为起始）")
    patient_table = patient_id_col = None
    for cand in cands:
        t = cand["source_table"]
        lows = {c.lower() for c in cols(t)}
        h = hint(t)
        picked = (h.lower() if h and h.lower() in lows
                  else next((p for p in _PK_HINTS if p in lows), None))
        if picked:
            patient_table, patient_id_col = t, picked
            break
    if not patient_table:
        raise Sql2FhirDeriveError(
            "患者主表未能判定：请为患者表补充 key_hint（接口分析/手动）或含 ID/MRN 主键列")
    patient_col = real(patient_table, patient_id_col)
    poll_key_col = patient_col          # BS 轮询凭证（可用接口分析建议的 MRN 等）
    others = [n for n in norm if n["source_table"] != patient_table]
    # 关联键与轮询键分离：患者关联键需与子表外键（如 PatientID → 患者表 ID）匹配，
    # 否则用轮询键（MRN 等）去查子表会取不到行。
    fk_cands: list[str] = []
    for o in others:
        lows_o = {c.lower() for c in cols(o["source_table"])}
        fk_cands += [c for c in lows_o if c in _PATIENT_FK]
    for fk in fk_cands:
        cand = re.sub(r"^(patient|pat|p)", "", fk) or "id"
        m = next((c for c in cols(patient_table) if c.lower() == cand), None)
        if m:
            patient_col = m
            break
    if not others:
        out = _layout(patient_table, patient_col, [], {
            "entries": [{"source_table": patient_table, "target_resource": "Patient",
                         "id_col": patient_col, "parent_kind": "root"}], "refs": []})
        out["poll_key_col"] = poll_key_col
        return out
    enc_on = any(o["target_resource"] == "Encounter" for o in others)
    bos, entries, refs = [], [], []
    for item in others:
        t, res = item["source_table"], item["target_resource"]
        lows = {c.lower() for c in cols(t)}
        if res not in ref_map:
            raise Sql2FhirDeriveError(f"目标资源 {res} 不在已建模引用图内（开放类型暂不推导）")
        enc_col = next((c for c in lows if c in _ENCOUNTER_FK), None)
        pat_col = next((c for c in lows if c in _PATIENT_FK), None)
        if enc_col and enc_on:
            kind, fk = "encounter", enc_col
        elif enc_col:
            kind, fk = "patient", pat_col or enc_col      # Encounter 未选：退化为按患者拉取
        elif pat_col:
            kind, fk = "patient", pat_col
        else:
            raise Sql2FhirDeriveError(
                f"子表 {t} 缺少指向患者/就诊的引用列（PatientID/EncounterID 等），"
                "无法推导查询 BO——请补充外键列或由接口分析判定")
        bos.append({"source_table": t, "target_resource": res, "bo_name": f"SQLQueryOp_{t}",
                    "parent_key": "encounter_id" if kind == "encounter" else "patient_id",
                    "fk_col": real(t, fk), "depth": 2 if kind == "encounter" else 1,
                    "depends_on": "Encounter" if kind == "encounter" else "Patient"})
        h = hint(t)
        pk = (h.lower() if h and h.lower() in lows else next((p for p in _PK_HINTS if p in lows), None))
        if not pk:
            raise Sql2FhirDeriveError(f"子表 {t} 无主键列（ID/…），无法派生确定性 UUID")
        entries.append({"source_table": t, "target_resource": res,
                        "id_col": real(t, pk), "parent_kind": kind})
        for field, parent in ref_map[res]:
            if parent == "Patient":
                refs.append({"target_resource": res, "parent_resource": "Patient",
                             "field": field, "to": "patient_entry"})
            elif parent == "Encounter" and enc_on:
                refs.append({"target_resource": res, "parent_resource": "Encounter",
                             "field": field, "to": "encounter_entry"})
    bos.sort(key=lambda b: b["depth"])   # 稳定排序：患者层(1)先于就诊层(2)，同层保持输入序
    entries = ([{"source_table": patient_table, "target_resource": "Patient",
                 "id_col": patient_col, "parent_kind": "root"}]
               + [e for e in entries if e["parent_kind"] == "patient"]
               + [e for e in entries if e["parent_kind"] == "encounter"])
    out = _layout(patient_table, patient_col, bos, {"entries": entries, "refs": refs})
    out["poll_key_col"] = poll_key_col
    return out


def _layout(patient_table, patient_id_col, bos, bundle):
    order = [e["target_resource"] for e in bundle.get("entries", [])]
    return {"design_skill": "sql2fhir-patient-tx",
            "patient_table": patient_table, "patient_id_col": patient_id_col,
            "query_bos": bos, "bundle": {"resource_order": order, **bundle},
            "derivation_rule": "参与表=mapping 涉及集合；BO 数=|参与子表数|；聚合按引用图+外键推导"}



# ---------------- 布局 → 组件拓扑（executor 确定性产出，供生成端接线） ----------------

def enrich_layout_with_mappings(layout: dict, mappings: list[dict]) -> dict:
    """把每资源 entry 与源表匹配的 mapping_id 注入布局（生成端持久化用）。

    mappings 为 pipeline 原 mapping 条目（含 id/source/target_table）；仅注入目标=FHIR 且
    source 表匹配的 mapping（同表多 mapping 取第一个，并在 note 里说明）。
    """
    by_src: dict[str, str] = {}
    for m in mappings or []:
        if not str(m.get("target_type") or "").upper().startswith("FHIR"):
            continue
        src = _table_from_source(str(m.get("source") or ""))
        if src and src not in by_src:
            by_src[src] = m.get("id") or ""
    entries = []
    for e in layout.get("bundle", {}).get("entries", []):
        e = dict(e)
        e["mapping_id"] = by_src.get(e.get("source_table"), "") or None
        entries.append(e)
    out = dict(layout)
    out["bundle"] = dict(out.get("bundle", {}))
    out["bundle"]["entries"] = entries
    return out


def _fhir_http_host_port(base_url: str) -> tuple[str, int]:
    """FHIR base → (host, port)，缺省 http://…:52773。"""
    import re
    url = (base_url or "").strip()
    if not url:
        return "localhost", 52773
    m = re.search(r"https?://([^:/]+)(?::(\d+))?", url)
    if not m:
        return "localhost", 52773
    default = 443 if url.lower().startswith("https") else 52773
    return m.group(1), int(m.group(2) or default)


def build_sql2fhir_components(layout: dict, source_config: dict | None = None,
                              target_config: dict | None = None,
                              category: str = "sql2fhir-patient-tx") -> list[dict]:
    """按布局确定性生成 sql2fhir-patient-tx 组件拓扑（Skill executor 产物）。

    组件构成由 derive 输出决定（患者主表单 BS + 每参与子表一个查询 BO + 聚合 BP +
    FHIR HTTP BO + JavaGateway）；className 与连接参数来自类型注册表/配置，属参数化。
    category = 管道类别（渲染为 Ens 业务主机 Category，供管道分组/按管道启停）；
    共享组件（JavaGateway）固定为 "shared"。
    """
    def s(target: str, name: str, value) -> dict:
        return {"target": target, "name": name, "value": value}

    cfg_src = source_config or {}
    cfg_tgt = target_config or {}
    dsn = cfg_src.get("dsn") or "localTarget"
    bp_name = "SqlFhirPatientTxProcess"   # 由数据管道设计 Agent 生成源码的 BP 组件名（无预置资产）
    comps = []
    patient = layout["patient_table"]
    comps.append({
        "type": "SQLService", "name": f"SQLService_{patient}",
        "className": "EnsLib.SQL.Service.GenericService",
        "category": category,
        "comment": f"患者主表 BS（一 BS 一表，逐患者入队）",
        "settings": [
            s("Adapter", "DSN", dsn),
            s("Adapter", "Query", f"SELECT * FROM SQLUser.{patient}"),
            s("Adapter", "KeyFieldName", layout.get("poll_key_col") or layout["patient_id_col"]),
            s("Adapter", "JGService", "EnsLib.JavaGateway.Service"),
            s("Host", "TargetConfigNames", bp_name),
        ],
    })
    # 查询 BO：复用现成 EnsLib.SQL.Operation.GenericOperation（读语义参数化，bo_contract），无新类
    for bo in layout.get("query_bos", []):
        comps.append({
            "type": "SQLOperation", "name": bo["bo_name"],
            "className": "EnsLib.SQL.Operation.GenericOperation",
            "category": category,
            "comment": f"查询 BO：{bo['source_table']}→{bo['target_resource']}（按 {bo['fk_col']} 读，复用现成组件）",
            "settings": [
                s("Adapter", "DSN", dsn),
                s("Adapter", "JGService", "EnsLib.JavaGateway.Service"),
                s("Host", "Query",
                  f"SELECT * FROM SQLUser.{bo['source_table']} WHERE {bo['fk_col']} = ?"),
                s("Host", "InputParameters", f"*{bo['fk_col']}"),
            ],
        })
    # 患者聚合 BP（控制中心）：类型契约 + 生成类名；类源码由 Agent 在生成管道时产出（平台不预置）
    comps.append({
        "type": "PatientTxProcess", "name": bp_name,
        "className": f"demo.{bp_name}",
        "category": category,
        "comment": "sql2fhir 患者聚合 BP（generated_by_agent）：布局驱动查子表→确定性 UUID→"
                   "全 PUT transaction→POST FHIR BO；源码为 Agent 生成产物，平台不预置",
        "settings": [],
    })
    host, port = _fhir_http_host_port(cfg_tgt.get("base_url") or cfg_tgt.get("endpoint") or "")
    comps.append({
        "type": "HTTPOperation", "name": "HTTPOperation",
        "className": "EnsLib.HTTP.GenericOperation",
        "category": category,
        "comment": "FHIR 目标：POST transaction Bundle",
        "settings": [s("Adapter", "HTTPServer", host), s("Adapter", "HTTPPort", port)],
    })
    comps.append({
        "type": "JavaGateway", "name": "EnsLib.JavaGateway.Service",
        "className": "EnsLib.JavaGateway.Service",
        "category": "shared",
        "comment": "Java 网关（查询/写 BO 共用）",
        "settings": [s("Host", "%gatewayName", "%Java Server")],
    })
    return comps

