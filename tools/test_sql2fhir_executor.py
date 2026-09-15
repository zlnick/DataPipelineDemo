# -*- coding: utf-8 -*-
"""sql2fhir-patient-tx executor 推导单测（容器内 python tools/test_sql2fhir_executor.py）。"""
import json

from backend.services import sql2fhir_executor as E

CLINIC = {
    "Patient": {"columns": ["ID", "MRN", "FamilyName", "GivenName", "Gender"],
                "key_hint": "ID"},
    "Encounter": {"columns": ["ID", "PatientID", "ClassCode", "ClassDisplay",
                              "Status", "PeriodStart", "ReasonCode"]},
    "Diagnosis": {"columns": ["ID", "EncounterID", "PatientID", "Code", "Name",
                              "CodeSystem", "Rank", "OnsetDate"]},
    "MedicationOrder": {"columns": ["ID", "EncounterID", "PatientID",
                                    "MedicationCode", "MedicationName"]},
}


def m(source, target, ttype="FHIR"):
    return {"source": source, "target_table": target, "target_type": ttype}


def show(name, layout):
    print(f"[{name}] patient={layout['patient_table']}({layout['patient_id_col']})")
    for b in layout["query_bos"]:
        print(f"   BO {b['bo_name']}: {b['source_table']}->{b['target_resource']} "
              f"by {b['fk_col']} parent={b['parent_key']} depth={b['depth']}")
    print("   order:", layout["bundle"]["resource_order"])
    print("   refs:", [(r["target_resource"], r["field"], r["parent_resource"])
                       for r in layout["bundle"]["refs"]])
    assert layout["bundle"]["resource_order"][0] == "Patient", "患者 entry 必须首位"


# 场景1：只选 Patient + Encounter → 1 个查询 BO
l1 = E.derive_sql2fhir_layout([m("Patient", "Patient"), m("Encounter", "Encounter")], CLINIC)
show("只选 Patient+Encounter", l1)
assert len(l1["query_bos"]) == 1
assert l1["query_bos"][0]["fk_col"] == "PatientID" and l1["query_bos"][0]["parent_key"] == "patient_id"
assert l1["bundle"]["resource_order"] == ["Patient", "Encounter"]
assert len(l1["bundle"]["refs"]) == 1 and l1["bundle"]["refs"][0]["field"] == "subject"

# 场景2：全选四表 → 3 个查询 BO，诊断/药嘱按就诊 key 拉取
l2 = E.derive_sql2fhir_layout([
    m("Patient", "Patient"), m("Encounter", "Encounter"),
    m("Diagnosis", "Condition"), m("MedicationOrder", "MedicationRequest")], CLINIC)
show("四表全选", l2)
assert len(l2["query_bos"]) == 3
depths = [b["depth"] for b in l2["query_bos"]]
assert depths == sorted(depths), "BO 依赖序应患者层先于就诊层"
enc_bo = [b for b in l2["query_bos"] if b["target_resource"] == "Encounter"]
diag_bo = [b for b in l2["query_bos"] if b["source_table"] == "Diagnosis"]
assert enc_bo[0]["parent_key"] == "patient_id" and diag_bo[0]["parent_key"] == "encounter_id"
assert l2["bundle"]["resource_order"] == ["Patient", "Encounter", "Condition", "MedicationRequest"]

# 场景3：只选 Patient+Encounter+Diagnosis（不选药）→ 2 BO；Condition 带 encounter 引用
l3 = E.derive_sql2fhir_layout([
    m("Patient", "Patient"), m("Encounter", "Encounter"), m("Diagnosis", "Condition")], CLINIC)
show("Patient+Encounter+Diagnosis", l3)
assert [b["source_table"] for b in l3["query_bos"]] == ["Encounter", "Diagnosis"]
assert l3["bundle"]["resource_order"] == ["Patient", "Encounter", "Condition"]

# 场景4：无 Encounter 参与（Patient+Diagnosis）→ 1 BO，退化按患者 key，且无 encounter 引用
l4 = E.derive_sql2fhir_layout([m("Patient", "Patient"), m("Diagnosis", "Condition")], CLINIC)
show("Patient+Diagnosis（无 Encounter）", l4)
assert len(l4["query_bos"]) == 1
assert l4["query_bos"][0]["parent_key"] == "patient_id"
assert not [r for r in l4["bundle"]["refs"] if r["parent_resource"] == "Encounter"]

# 场景5：只选 Patient → 0 BO，仅患者
l5 = E.derive_sql2fhir_layout([m("Patient", "Patient")], CLINIC)
assert l5["query_bos"] == [] and l5["bundle"]["resource_order"] == ["Patient"]

# 场景6：缺患者表映射 → 显式报错
try:
    E.derive_sql2fhir_layout([m("Diagnosis", "Condition")], CLINIC)
    raise SystemExit("should raise")
except E.Sql2FhirDeriveError as exc:
    print("[无患者表] 显式报错 ok:", str(exc)[:40])

# 场景7：缺外键列的子表 → 显式报错
try:
    E.derive_sql2fhir_layout([
        m("Patient", "Patient"), m("Encounter", "Encounter"),
        m("Diagnosis", "Condition")],
        {**CLINIC, "Diagnosis": {"columns": ["ID", "Code", "Name"]}})
    raise SystemExit("should raise")
except E.Sql2FhirDeriveError as exc:
    print("[缺外键列] 显式报错 ok:", str(exc)[:40])

# 引用矩阵同口径冒烟
rmap = E.model_reference_map()
assert ("patient", "Patient") in rmap["Immunization"]
assert ("patient", "Patient") in rmap["AllergyIntolerance"]
assert ("subject", "Patient") in rmap["Encounter"]
print("ref-map US Core 口径 ok")
print("ALL-DERIVE-TESTS-PASSED")
