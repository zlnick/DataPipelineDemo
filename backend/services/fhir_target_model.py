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
             "note": "US Core MS，可空或 SNOMED 就诊类型"},
            {"name": "subject", "type": "Reference(Patient)", "required": True},
            {"name": "period_start", "type": "dateTime", "required": True},
            {"name": "period_end", "type": "dateTime", "required": False},
            {"name": "reason_code", "type": "codeableConcept", "required": False,
             "note": "可沿用国标 ICD-10 主诊断码（CodeSystem=urn:cn-nhsa:icd10-gbt2016）"},
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
             "note": "SNOMED route 或文本"},
            {"name": "frequency", "type": "string", "required": False,
             "note": "dosage[].timing 简写"},
        ],
    },
}

# FHIR 目标的默认资源类型（UI 新建时可多选/全选）
DEFAULT_RESOURCE_TYPES = ["Patient", "Encounter", "Condition", "MedicationRequest"]


def build_entities(resource_types: list[str] | None = None) -> list[dict]:
    """按资源类型清单构建目标实体（顺序稳定）。"""
    types = resource_types or DEFAULT_RESOURCE_TYPES
    out = []
    for rt in types:
        model = US_CORE_RESOURCE_MODELS.get(rt)
        if model:
            out.append(model)
    return out
