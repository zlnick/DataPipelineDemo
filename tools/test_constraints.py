# -*- coding: utf-8 -*-
"""目标模型约束元数据单测（choice/数组/引用目标）。"""
from backend.services import fhir_target_model as FTM

c_pat = FTM.column_constraints("Patient")
assert c_pat["phone"]["value_landing"] == "scalar" and c_pat["phone"]["path_in_array"] is True, c_pat["phone"]
assert c_pat["address_line"]["value_landing"] == "array", c_pat["address_line"]
assert c_pat["identifier"]["value_landing"] == "object"
assert c_pat["family"]["value_landing"] == "scalar" and c_pat["family"]["path_in_array"] is True
assert c_pat["gender"]["value_landing"] == "scalar" and c_pat["gender"]["path_in_array"] is False
print("Patient 约束 OK：phone=scalar@array, address_line=array, identifier=object, family=scalar@array, gender=root")

c_med = FTM.column_constraints("MedicationRequest")
assert c_med["medication"]["choice_property"] == "medicationCodeableConcept", c_med["medication"]
assert c_med["medication"]["fh_property"] == "medicationCodeableConcept"
assert c_med["subject"]["reference_target"] == "Patient", c_med["subject"]
assert c_med["dose"]["value_landing"] == "object", c_med["dose"]
print("MedicationRequest 约束 OK：medication→medicationCodeableConcept(choice), subject→Patient, dose=object")

c_enc = FTM.column_constraints("Encounter")
assert c_enc["subject"]["reference_target"] == "Patient"
assert c_enc["period_end"]["fh_property"] == "end"
print("Encounter 约束 OK：subject→Patient, period_end→end")

assert FTM.column_constraints("UnknownType") == {}
print("CONSTRAINTS-TEST-PASSED")
