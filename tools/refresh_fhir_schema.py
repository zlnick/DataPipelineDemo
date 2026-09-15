# -*- coding: utf-8 -*-
"""按最新 fhir_target_model 刷新 ^demo.Config("fhir","schema",<资源>)（平台参数化）。"""
import json

import iris

from backend.services import fhir_target_model as FTM
from backend.services import iris_connector as C

conn = C.get_connection()
native = iris.createIRIS(conn)
n = 0
for rt in FTM.DEFAULT_RESOURCE_TYPES:
    schema = FTM.model_schema(rt)
    if schema:
        native.set(json.dumps(schema, ensure_ascii=False), "^demo.Config", "fhir", "schema", rt)
        n += 1
print("schema refreshed for", n, "resources")
p = FTM.model_schema("Patient")
line = next((c for c in p["columns"] if c["name"] == "address_line"), None)
print("Patient.address_line path =", line and line.get("path"))
conn.close()
