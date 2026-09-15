# -*- coding: utf-8 -*-
"""Agent 生成 BP（generated_bp）全链路冒烟：LLM 产出源码 → 静态准入 → IRIS 编译（≤3 轮回 feed）。"""
from backend.services import generated_bp as GB
from backend.services import type_registry

M = [
    {"source": "Patient", "target_table": "Patient", "target_type": "FHIR"},
    {"source": "Encounter", "target_table": "Encounter", "target_type": "FHIR"},
    {"source": "Diagnosis", "target_table": "Condition", "target_type": "FHIR"},
]
res = GB.generate_and_compile_bp(
    mappings=M, source_type="SQL", target_type="FHIR",
    available_components=type_registry.get_available_components(),
    source_runtime={"connection": {"dsn": "CLINIC"}, "poll": {"table": "Patient"}},
    target_runtime={"connection": {"base_url": "http://localhost:52773/csp/healthshare/fhirserver/fhir/r4"}},
    max_rounds=3)
print("attempts:", res.get("attempts"))
print("ok:", res.get("ok"))
print("message:", str(res.get("message"))[:500])
print("AGENT-BP-SMOKE-DONE")
