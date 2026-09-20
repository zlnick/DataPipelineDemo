# -*- coding: utf-8 -*-
"""FHIR 目标（US Core 声明式）资源字段模型——平台常量，供转换 Agent 作为"目标实体"。

目标类型 FHIR 的"表/实体"即 US Core 资源类型；字段给出扁平名 + FHIRPath + 必填/MS + 语义，
帮助 LLM 生成"源表列 → FHIR 字段"映射。模型来自 US Core R4 StructureDefinition（6.1 通用要素），
字段为演示最小集（不追求全量 US Core）。
"""
# profile 基准 URL（版本不带 → 服务器声明式可接受）
US_CORE_BASE = "http://hl7.org/fhir/us/core/StructureDefinition"

US_CORE_RESOURCE_MODELS = {
    "Patient": {
        "entity_name": "Patient",
        "table": "Patient",
        "schema": "FHIR",
        "profile": f"{US_CORE_BASE}/us-core-patient",
        "columns": [
            {"name": "identifier", "type": "identifier", "required": True,
             "note": "identifier[].system=MRN 体系；可映射源 MRN"},
            {"name": "family", "type": "string", "required": True, "note": "name[0].family"},
            {"name": "given", "type": "string", "required": True, "note": "name[0].given[]"},
            {"name": "gender", "type": "code", "required": True,
             "note": "administrative-gender: male|female|other/unknown"},
            {"name": "birthDate", "type": "date", "required": True},
            {"name": "phone", "type": "string", "required": False, "note": "telecom[](phone)"},
            {"name": "address_line", "type": "string", "required": False, "note": "address[].line"},
            {"name": "city", "type": "string", "required": False, "note": "address[].city"},
        ],
    },
    "Encounter": {
        "entity_name": "Encounter",
        "table": "Encounter",
        "schema": "FHIR",
        "profile": f"{US_CORE_BASE}/us-core-encounter",
        "columns": [
            {"name": "status", "type": "code", "required": True,
             "note": "planned|arrived|triaged|in-progress|finished|cancelled"},
            {"name": "class_code", "type": "code", "required": True,
             "note": "v3.ActCode: IMP(住院)/AMB(门诊)/EMER(急诊)…"},
            {"name": "type", "type": "codeableConcept", "required": False,
             "note": "US Core MS，可空或 SNOMED 就诊类型；**中文/自由文本请用 type_text**"},
            {"name": "type_text", "type": "string", "required": False,
             "note": "就诊类型文本（type[0].text；源为中文明文时落这里，不要把明文塞进 coding.code）"},
            {"name": "subject", "type": "Reference(Patient)", "required": True},
            {"name": "period_start", "type": "dateTime", "required": True},
            {"name": "period_end", "type": "dateTime", "required": False},
            {"name": "reason_code", "type": "codeableConcept", "required": False,
             "note": "可沿用国标 ICD-10 主诊断码（CodeSystem=urn:cn-nhsa:icd10-gbt2016）；"
                     "中文/自由文本请用 reason_text"},
            {"name": "reason_text", "type": "string", "required": False,
             "note": "就诊原因文本（reasonCode[0].text；源为中文明文时落这里）"},
        ],
    },
    "Condition": {
        "entity_name": "Condition",
        "table": "Condition",
        "schema": "FHIR",
        "profile": f"{US_CORE_BASE}/us-core-condition-encounter-diagnosis",
        "columns": [
            {"name": "clinicalStatus", "type": "codeableConcept", "required": True,
             "note": "condition-clinical: active|recurrence|relapse|inactive|remission|resolved"},
            {"name": "verificationStatus", "type": "codeableConcept", "required": True,
             "note": "confirmed|provisional|refuted|entered-in-error"},
            {"name": "category", "type": "codeableConcept", "required": True,
             "note": "condition-category: encounter-diagnosis（就诊诊断）"},
            {"name": "code", "type": "codeableConcept", "required": True,
             "note": "建议双 coding：源国标 ICD-10 原码 + SNOMED(经术语对照)；CodeSystem 见源数据"},
            {"name": "subject", "type": "Reference(Patient)", "required": True},
            {"name": "encounter", "type": "Reference(Encounter)", "required": False},
            {"name": "onset", "type": "dateTime", "required": False, "note": "OnsetDate"},
            {"name": "recordedDate", "type": "dateTime", "required": False,
             "note": "缺省可用就诊日期"},
        ],
    },
    "MedicationRequest": {
        "entity_name": "MedicationRequest",
        "table": "MedicationRequest",
        "schema": "FHIR",
        "profile": f"{US_CORE_BASE}/us-core-medicationrequest",
        "columns": [
            {"name": "status", "type": "code", "required": True,
             "note": "active|completed|stopped…"},
            {"name": "intent", "type": "code", "required": True, "note": "order（医嘱）"},
            {"name": "medication", "type": "codeableConcept", "required": True,
             "note": "建议双 coding：中文药码(urn:cn-nhsa:drug-nrdl) + RxNorm(经术语对照)"},
            {"name": "subject", "type": "Reference(Patient)", "required": True},
            {"name": "authoredOn", "type": "dateTime", "required": False,
             "note": "可映射 StartDate"},
            {"name": "requester", "type": "Reference(Practitioner)", "required": False,
             "note": "US Core MS；源无开嘱人时可给演示 Practitioner 引用或省略"},
            {"name": "dose", "type": "quantity", "required": False, "note": "dosage[].doseAndRate[].dose"},
            {"name": "route", "type": "codeableConcept", "required": False,
             "note": "SNOMED route 编码；**中文/自由文本（如“口服”）请用 route_text**"},
            {"name": "route_text", "type": "string", "required": False,
             "note": "给药途径文本（dosageInstruction[0].route.text；源为中文明文时落这里）"},
            {"name": "frequency", "type": "string", "required": False,
             "note": "dosage[].timing 简写"},
        ],
    },
    "Observation": {
        "entity_name": "Observation",
        "table": "Observation",
        "schema": "FHIR",
        "profile": f"{US_CORE_BASE}/us-core-observation-lab",
        "columns": [
            {"name": "status", "type": "code", "required": True,
             "note": "registered|preliminary|final|amended|corrected|cancelled"},
            {"name": "category", "type": "codeableConcept", "required": True,
             "note": "vital-signs / laboratory，观察类别"},
            {"name": "code", "type": "codeableConcept", "required": True,
             "note": "LOINC 检验/体征代码（源为中文检验名时可双 coding 保留原文）"},
            {"name": "subject", "type": "Reference(Patient)", "required": True},
            {"name": "effectiveDateTime", "type": "dateTime", "required": False,
             "note": "测量/检验时间，可映射源时间列"},
            {"name": "value", "type": "quantity", "required": False,
             "note": "valueQuantity.value + unit（数值结果）"},
            {"name": "interpretation", "type": "codeableConcept", "required": False,
             "note": "异常标志（SNOMED/abnormal）"},
        ],
    },
    "AllergyIntolerance": {
        "entity_name": "AllergyIntolerance",
        "table": "AllergyIntolerance",
        "schema": "FHIR",
        "profile": f"{US_CORE_BASE}/us-core-allergyintolerance",
        "columns": [
            {"name": "clinicalStatus", "type": "codeableConcept", "required": True,
             "note": "allergyintolerance-clinical: active|inactive|resolved"},
            {"name": "verificationStatus", "type": "codeableConcept", "required": True,
             "note": "unconfirmed|confirmed|refuted"},
            {"name": "code", "type": "codeableConcept", "required": True,
             "note": "过敏原：SNOMED/文本 + 中文编码系统对照"},
            {"name": "patient", "type": "Reference(Patient)", "required": True},
            {"name": "category", "type": "code", "required": False,
             "note": "food|medication|environment|biologic"},
            {"name": "onset", "type": "dateTime", "required": False, "note": "发生时间"},
        ],
    },
    "Procedure": {
        "entity_name": "Procedure",
        "table": "Procedure",
        "schema": "FHIR",
        "profile": f"{US_CORE_BASE}/us-core-procedure",
        "columns": [
            {"name": "status", "type": "code", "required": True,
             "note": "preparation|in-progress|not-done|on-hold|stopped|completed|entered-in-error|unknown"},
            {"name": "code", "type": "codeableConcept", "required": True,
             "note": "操作/手术 SNOMED 代码或中文文本"},
            {"name": "subject", "type": "Reference(Patient)", "required": True},
            {"name": "performedDateTime", "type": "dateTime", "required": True},
            {"name": "encounter", "type": "Reference(Encounter)", "required": False},
            {"name": "reasonCode", "type": "codeableConcept", "required": False,
             "note": "可沿用国标 ICD-10 主诊断（双 coding）"},
        ],
    },
    "DiagnosticReport": {
        "entity_name": "DiagnosticReport",
        "table": "DiagnosticReport",
        "schema": "FHIR",
        "profile": f"{US_CORE_BASE}/us-core-diagnosticreport-note",
        "columns": [
            {"name": "status", "type": "code", "required": True,
             "note": "registered|preliminary|final|amended|corrected|cancelled"},
            {"name": "category", "type": "codeableConcept", "required": True,
             "note": "LAB/RAD 报告类别"},
            {"name": "code", "type": "codeableConcept", "required": True,
             "note": "报告类型 LOINC 代码"},
            {"name": "subject", "type": "Reference(Patient)", "required": True},
            {"name": "effectiveDateTime", "type": "dateTime", "required": False},
            {"name": "conclusion", "type": "string", "required": False,
             "note": "诊断报告结论文本"},
        ],
    },
    "Immunization": {
        "entity_name": "Immunization",
        "table": "Immunization",
        "schema": "FHIR",
        "profile": f"{US_CORE_BASE}/us-core-immunization",
        "columns": [
            {"name": "status", "type": "code", "required": True,
             "note": "completed|entered-in-error|not-done"},
            {"name": "vaccineCode", "type": "codeableConcept", "required": True,
             "note": "疫苗代码（CVX/中文疫苗名 + 编码系统对照）"},
            {"name": "patient", "type": "Reference(Patient)", "required": True},
            {"name": "occurrenceDateTime", "type": "dateTime", "required": True},
            {"name": "lotNumber", "type": "string", "required": False},
        ],
    },
    "Organization": {
        "entity_name": "Organization",
        "table": "Organization",
        "schema": "FHIR",
        "profile": f"{US_CORE_BASE}/us-core-organization",
        "columns": [
            {"name": "identifier", "type": "identifier", "required": False,
             "note": "identifier[].value 机构代码（映射源医院编码）"},
            {"name": "name", "type": "string", "required": True},
            {"name": "active", "type": "boolean", "required": False},
            {"name": "address_line", "type": "string", "required": False,
             "note": "address[].line"},
            {"name": "city", "type": "string", "required": False},
        ],
    },
    "Practitioner": {
        "entity_name": "Practitioner",
        "table": "Practitioner",
        "schema": "FHIR",
        "profile": f"{US_CORE_BASE}/us-core-practitioner",
        "columns": [
            {"name": "identifier", "type": "identifier", "required": False,
             "note": "执业证号/NPI"},
            {"name": "family", "type": "string", "required": True, "note": "name[0].family"},
            {"name": "given", "type": "string", "required": True, "note": "name[0].given[]"},
            {"name": "telecom", "type": "string", "required": False, "note": "telecom[].value"},
        ],
    },
}




# FHIR 目标的「已建模」候选资源类型（平台内置字段模型，映射质量高、可直接给出字段级映射）。
# 注册 FHIR 目标时候选集合 = 服务器 CapabilityStatement 支持的全部资源类型
# （默认全量，不再写死四类）：已建模类型带 profile + FHIRPath 字段结构；
# 其余未建模但服务器支持的类型按「开放候选」提供（open=True，无列结构），
# 其映射字段由 Agent（LLM）在 AI 智能匹配时依据 FHIR R4 / US Core 规范自定。
DEFAULT_RESOURCE_TYPES = [
    "Patient", "Encounter", "Condition", "MedicationRequest",
    "Observation", "AllergyIntolerance", "Procedure",
    "DiagnosticReport", "Immunization", "Organization", "Practitioner",
]


def open_entity(rt: str) -> dict:
    """为未建模但服务器支持的资源类型构建开放候选实体（无列结构，交由 LLM 自定字段）。"""
    return {
        "entity_name": rt,
        "table": rt,
        "schema": "FHIR",
        "profile": "",
        "modeled": False,
        "open": True,
        "columns": [],
        "fields": [],
        "note": "服务器支持此 FHIR R4 资源类型；平台未内置字段模型，"
                "映射字段由 Agent 依 FHIR R4/US Core 规范自定",
    }


# ===== FHIR 组装元数据（平台结构知识，供通用打包器/AI 上下文使用，非决策）=====
# 说明：字段列已有 name/type；这里补充「值应落入资源 JSON 的路径 path」与部分静态
# coding system。打包器按此元数据把 LLM 决策的行 JSON 通用序列化成 FHIR 资源，
# 不再为每个资源类型手写组装器。
_COLUMN_PATHS: dict[str, dict[str, str]] = {
    "Patient": {
        "identifier": "identifier[0]",
        "family": "name[0].family",
        "given": "name[0].given[0]",
        "gender": "gender",
        "birthDate": "birthDate",
        "phone": "telecom[0].value",
        "address_line": "address[0].line[0]",
        "city": "address[0].city",
    },
    "Encounter": {
        "status": "status",
        "class_code": "class",
        "type": "type[0]",
        "subject": "subject",
        "period_start": "period.start",
        "period_end": "period.end",
        "reason_code": "reasonCode[0]",
        "reason_text": "reasonCode[0].text",
        "type_text": "type[0].text",
    },
    "Condition": {
        "clinicalStatus": "clinicalStatus",
        "verificationStatus": "verificationStatus",
        "category": "category[0]",
        "code": "code",
        "subject": "subject",
        "encounter": "encounter",
        "onset": "onsetDateTime",
        "recordedDate": "recordedDate",
    },
    "MedicationRequest": {
        "status": "status",
        "intent": "intent",
        "medication": "medicationCodeableConcept",
        "subject": "subject",
        "authoredOn": "authoredOn",
        "requester": "requester",
        "dose": "dosageInstruction[0].doseAndRate[0].doseQuantity",
        "route": "dosageInstruction[0].route",
        "route_text": "dosageInstruction[0].route.text",
        "frequency": "dosageInstruction[0].text",
    },
}

# 静态 coding/system 提示（仅作用于真正序列化为 Coding/CodeableConcept 的列；原始 code 字段不加）
_COLUMN_SYSTEMS: dict[str, dict[str, str]] = {
    "Patient": {"identifier": "http://demo/mrn"},
    "Encounter": {"class_code": "http://terminology.hl7.org/CodeSystem/v3-ActCode"},
    "Condition": {
        "clinicalStatus": "http://terminology.hl7.org/CodeSystem/condition-clinical",
        "verificationStatus": "http://terminology.hl7.org/CodeSystem/condition-ver-status",
        "category": "http://terminology.hl7.org/CodeSystem/condition-category",
    },
}


def _column_constraints(rt: str, name: str, col: dict) -> dict:
    """从列元数据（type/path/name）推导机器可读的 FHIR 结构约束。

    供 C1 校验/AI 提示/打包器共用，避免依赖自然语言 note：
      - reference_target：Reference(X) → X（引用目标资源类型）
      - choice_property：FHIR choice 元素的真实属性名（如 medication → medicationCodeableConcept）
      - value_landing：值最终落入的 JSON 形态（scalar / object / array）
      - array_element：路径末端是否数组元素（如 address[0].line[0] → True）
    """
    path = str(col.get("path") or name)
    ty = str(col.get("type") or "")
    segs = [s for s in path.split(".") if s]
    last = segs[-1] if segs else path
    is_array_elem = "[" in last
    base_last = last.split("[")[0]
    # 值形态：数组元素 + 标量类型 → array；对象类型（concept/identifier/reference/quantity）→ object
    if "concept" in ty.lower() or ty.lower() in ("identifier", "quantity") or ty.startswith("Reference"):
        landing = "object"
    elif is_array_elem:
        landing = "array"
    else:
        landing = "scalar"
    ref_target = None
    if ty.startswith("Reference(") and ty.endswith(")"):
        ref_target = ty[len("Reference("):-1]
    return {
        "fh_property": base_last,
        "value_landing": landing,
        "array_element": is_array_elem,
        "path_in_array": "[" in path,
        "reference_target": ref_target,
        "choice_property": base_last if base_last != name else None,
    }


def _enrich_columns(rt: str, columns: list[dict]) -> list[dict]:
    """为模型列注入组装元数据（path/system）与结构约束（constraints），不改常量。"""
    paths = _COLUMN_PATHS.get(rt, {})
    systems = _COLUMN_SYSTEMS.get(rt, {})
    out = []
    for c in columns:
        name = c.get("name", "")
        copy = dict(c)
        # path：显式映射优先；未映射时若 name 已含路径语义（带 . / [ ）直接用，否则放根
        if name in paths:
            copy["path"] = paths[name]
        elif any(ch in name for ch in ".["):
            copy["path"] = name
        else:
            copy["path"] = name
        if name in systems:
            copy["system"] = systems[name]
        copy["constraints"] = _column_constraints(rt, name, copy)
        out.append(copy)
    return out


def build_entities(resource_types: list[str] | None = None) -> list[dict]:
    """按资源类型清单构建目标实体（顺序稳定；未建模类型生成开放候选）。

    已建模实体：深拷贝 + 每列注入组装元数据 path/system（不改模块常量）。
    """
    import copy as _copy
    types = resource_types or DEFAULT_RESOURCE_TYPES
    out = []
    for rt in types:
        model = US_CORE_RESOURCE_MODELS.get(rt)
        if model:
            m = _copy.deepcopy(model)
            m["columns"] = _enrich_columns(rt, m.get("columns", []))
            m["fields"] = [c["name"] for c in m["columns"]]
            out.append(m)
        else:
            out.append(open_entity(rt))
    return out


def column_constraints(resource_type: str) -> dict[str, dict]:
    """返回某已建模资源的列结构约束：{列名: {fh_property, value_landing, array_element,
    reference_target, choice_property}}（供校验/提示/API 共用）。"""
    ent = next((e for e in build_entities([resource_type]) if e.get("entity_name")), None)
    if not ent:
        return {}
    return {c["name"]: c.get("constraints", {}) for c in ent.get("columns", [])}


def source_field_paths(resource_type: str) -> list[str]:
    """已建模资源在 R4/US Core 模型里的**可访问字段路径**（供 FHIR 源字段发现兜底）。

    FHIR 是标准：**没有样例数据也能知道该资源有哪些元素**。平台把 US Core 已建模的 11 类资源
    的元素路径（数组以 `[0]` 表示，与转换引擎/AI 映射的访问路径一致，如 `name[0].family`、
    `identifier[0]`、`telecom[0].value`）作为**规范快照**提供，无数据时用它兜底。
    未建模类型返回 `[]`（调用方转 AI 按 FHIR R4 规范推断，或保持为空）。
    """
    ent = next((e for e in build_entities([resource_type])
                if e.get("columns") and not e.get("open")), None)
    if not ent:
        return []
    out: list[str] = []
    for c in ent.get("columns", []):
        path = str(c.get("path") or c.get("name") or "").strip()
        if path and path not in out:
            out.append(path)
    return out


def source_field_paths_all(resource_types: list[str] | None = None) -> dict[str, list[str]]:
    """批量版：{资源类型: 字段路径列表}（仅返回已建模类型）。"""
    out: dict[str, list[str]] = {}
    for rt in (resource_types or DEFAULT_RESOURCE_TYPES):
        paths = source_field_paths(rt)
        if paths:
            out[rt] = paths
    return out


def model_schema(resource_type: str) -> dict | None:
    """返回单个已建模资源的组装 schema（含 path/system 的列），供通用打包器/生成端写入。"""
    ents = build_entities([resource_type])
    if not ents:
        return None
    ent = ents[0]
    return {
        "entity_name": ent.get("entity_name"),
        "table": ent.get("table"),
        "profile": ent.get("profile", ""),
        "modeled": True,
        "columns": ent.get("columns", []),
    }
