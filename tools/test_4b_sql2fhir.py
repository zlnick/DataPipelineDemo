# -*- coding: utf-8 -*-
"""4B 回归：derive → enrich(mapping) → build components → C2 拓扑校验放行。"""
from backend.services import sql2fhir_executor as E
from backend.services import type_registry as T
from backend.services import pipeline_validator as V

M = [
    {"id": "m1", "source": "Patient", "target": "Patient", "target_type": "FHIR", "target_table": "Patient"},
    {"id": "m2", "source": "Encounter", "target": "Encounter", "target_type": "FHIR", "target_table": "Encounter"},
    {"id": "m3", "source": "Diagnosis", "target": "Condition", "target_type": "FHIR", "target_table": "Condition"},
    {"id": "m4", "source": "MedicationOrder", "target": "MedicationRequest",
     "target_type": "FHIR", "target_table": "MedicationRequest"},
]
META = {
    "Patient": {"columns": ["ID", "MRN", "FamilyName", "GivenName", "Gender"], "key_hint": "ID"},
    "Encounter": {"columns": ["ID", "PatientID", "ClassCode", "Status"]},
    "Diagnosis": {"columns": ["ID", "EncounterID", "PatientID", "Code", "Name"]},
    "MedicationOrder": {"columns": ["ID", "EncounterID", "PatientID", "MedicationCode"]},
}

L = E.derive_sql2fhir_layout(M, META)
L = E.enrich_layout_with_mappings(L, M)
print("entries:", [(e["target_resource"], e["mapping_id"]) for e in L["bundle"]["entries"]])
assert all(e["mapping_id"] for e in L["bundle"]["entries"]), "每个 entry 应注入 mapping_id"

comps = E.build_sql2fhir_components(
    L, {"dsn": "CLINIC"},
    {"base_url": "http://iris:52773/csp/healthshare/fhirserver/fhir/r4"})
print("components:", [(c["type"], c["name"], c["className"]) for c in comps])
assert sum(1 for c in comps if c["type"] == "SQLOperation") == 3, "查询 BO 复用现成 SQLOperation ×3"
assert all(c["className"] == "EnsLib.SQL.Operation.GenericOperation"
           for c in comps if c["type"] == "SQLOperation")
qbo = [c for c in comps if c["type"] == "SQLOperation"][0]
assert any(s["name"] == "Query" and "WHERE" in str(s["value"]) for s in qbo["settings"])
assert any(s["name"] == "InputParameters" for s in qbo["settings"])
assert sum(1 for c in comps if c["type"] == "SQLService") == 1
bp = [c for c in comps if c["type"] == "PatientTxProcess"][0]
assert bp["name"] == "SqlFhirPatientTxProcess" and bp["className"] == "demo.SqlFhirPatientTxProcess"
assert "generated_by_agent" in bp["comment"]

print("asset-reg:", [c["type"] for c in T.get_pipeline_asset_components()])
assert T.get_pipeline_asset_components()  # 注册表有 Skill 资产

r = V.check_pipeline_topology({"production": "demo.DataflowProduction", "components": comps},
                              source_type="SQL", target_type="FHIR")
print("check ok:", r["ok"], "| issues:", [i["message"] for i in r["issues"]])
assert r["ok"], "C2 拓扑校验必须放行 sql2fhir 资产组件"

# JavaGateway/处理组件必选逻辑未破坏旧流程回归：FHIR→DB 拓扑仍要求 TransformProcess
old = V.check_pipeline_topology({
    "production": "demo.DataflowProduction",
    "components": [{"type": "FHIRSyncService", "className": "x", "settings": []},
                   {"type": "FHIRService", "className": "x", "settings": []},
                   {"type": "SQLOperation", "className": "x", "settings": []},
                   {"type": "JavaGateway", "className": "x", "settings": []}]},
    source_type="FHIR", target_type="DB")
assert not old["ok"] and any("处理组件" in i["message"] for i in old["issues"]), \
    "旧路径缺 TransformProcess 应报处理组件缺失"
print("4B-OK")
