# -*- coding: utf-8 -*-
"""C2 新增检查冒烟：目标落地效果（FHIR 计数）+ 运行期错误分类。"""
import json

import iris

from backend.services import iris_connector as C
from backend.services import pipeline_validator as PV

conn = C.get_connection()
native = iris.createIRIS(conn)
topo_json = native.get("^demo.Config", "pipeline", "topology")
conn.close()
topo = json.loads(topo_json) if isinstance(topo_json, str) and topo_json else {}

eff = PV.check_target_effect(["FHIR"], {"Patient": 10, "Encounter": 13, "Condition": 18,
                                        "MedicationRequest": 18},
                             PV._fhir_base_from_topology(topo))
print("target_effect ok:", eff["ok"], "| counts:", eff["counts"])
for i in eff["issues"]:
    print("   issue:", i["severity"], i["item"], str(i["message"])[:120])

rerr = PV.collect_runtime_errors(5, minutes=15)
print("runtime errors (last 15min):", len(rerr))
for e in rerr[:3]:
    print("   ", e["config"], "|", e["kind"], "|", e["text"][:120])

val = PV.run_pipeline_validation(topo, source_type="SQL", target_type="FHIR",
                                 expect_targets={"Patient": 10})
print("validation ok:", val["ok"], "| error_count:", val["error_count"],
      "| checks:", list(val["results"].keys()))
print("C2-EFFECT-SMOKE-DONE")
