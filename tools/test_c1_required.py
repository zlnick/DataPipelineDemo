# -*- coding: utf-8 -*-
"""C1 必填校验 + LLM 修复实测：FHIR 目标必填字段补齐。"""
from backend.routes import pipelines as P
from backend.services import repository, transformation_validator as TV, fhir_target_model as FTM

maps = repository.list_mappings() or []
sid = next((d.get("id") for d in (repository.list_datasources() or [])
            if d.get("type") == "SQL"), None)
print("mappings:", [(m.get("id"), m.get("source"), m.get("target_table")) for m in maps])

issues = TV._fhir_required_issues(maps)
print("required-issues:", len(issues))
for i in issues:
    print("  -", i["item"], i["message"])

res = TV.validate_and_fix_transformation(
    maps, assets=P._c1_assets(sid), target_models=P._c1_target_models(maps))
print("status:", res.get("status"), "| rounds:",
      [(r["round"], r["error_count"]) for r in res.get("rounds", [])])
for m in res.get("mappings") or []:
    rt = m.get("target_table")
    req = [c["name"] for c in (FTM.model_schema(rt) or {}).get("columns", [])
           if c.get("required")]
    mapped = [fm.get("target") for fm in m.get("field_mappings", [])]
    missing = [r for r in req if r not in mapped]
    print(f"  {m.get('id')} {m.get('source')}->{rt}: mapped={len(mapped)} "
          f"required_missing={missing}")
print("C1-TEST-DONE")
