# -*- coding: utf-8 -*-
"""生成前强校验冒烟：真实映射应通过；FHIR 属性名误用/必填引用缺标注应判错。"""
from backend.services import repository
from backend.services import transformation_validator as TV

maps = repository.list_mappings() or []
res = TV.validate_mapping_against_constraints(maps)
print("real mappings ok:", res["ok"], "| errors:", res["error_count"])
for i in res["issues"]:
    print("   [", i.get("severity"), "]", i.get("check"), i.get("item"), str(i.get("message"))[:110])

BAD1 = [{"id": "X1", "source": "MedicationOrder", "target_table": "MedicationRequest",
         "target_type": "FHIR", "field_mappings": [
             {"source": "MedicationCode", "target": "medicationCodeableConcept", "transform": "code"},
             {"source": None, "target": "intent", "transform": "constant:order"},
             {"source": "PatientID", "target": "subject", "transform": "reference"},
             {"source": "Status", "target": "status", "transform": "code"}]}]
r1 = TV.validate_mapping_against_constraints(BAD1)
print("use FHIR property name -> ok:", r1["ok"], "| errors:", r1["error_count"],
      "|", [i["message"][:80] for i in r1["issues"] if i["severity"] == "error"])

BAD2 = [{"id": "X2", "source": "MedicationOrder", "target_table": "MedicationRequest",
         "target_type": "FHIR", "field_mappings": [
             {"source": "MedicationCode", "target": "medication", "transform": "code"},
             {"source": None, "target": "intent", "transform": "constant:order"},
             {"source": "PatientID", "target": "subject", "transform": None},
             {"source": "Status", "target": "status", "transform": "code"}]}]
r2 = TV.validate_mapping_against_constraints(BAD2)
print("required reference without tag -> ok:", r2["ok"], "| errors:", r2["error_count"],
      "|", [i["message"][:80] for i in r2["issues"] if i["severity"] == "error"])
print("MAPPING-CONSTRAINTS-SMOKE-DONE")
