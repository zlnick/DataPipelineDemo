# -*- coding: utf-8 -*-
"""端到端模拟 UI 操作流程：SQL 源登记 → 连通 → 选表分析 → seed → FHIR 目标 → AI 匹配 → 保存映射 → 生成管道。

每步都打印关键结果；任何一步失败即中止并打印服务端 message。
"""
import json
import urllib.error
import urllib.request

BASE = "http://localhost:5001/api"
CLINIC_JDBC = "jdbc:IRIS://iris:1972/CLINIC"
FHIR_BASE = "http://iris:52773/csp/healthshare/fhirserver/fhir/r4"
TIMEOUT = 900


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()
        raise SystemExit(f"[FAIL] {method} {path} -> HTTP {exc.code}\n{detail[:800]}")
    if payload.get("code") != 0:
        raise SystemExit(f"[FAIL] {method} {path} -> {payload.get('message')}")
    return payload.get("data")


def step(msg: str):
    print("\n==== " + msg + " ====", flush=True)


# 1. 登记 CLINIC SQL 数据源
step("① 数据源管理：新建 CLINIC SQL 源")
ds = call("POST", "/datasources", {
    "name": "CLINIC 演示源", "type": "SQL",
    "config": {"jdbc_url": CLINIC_JDBC, "driver_class": "com.intersystems.jdbc.IRISDriver",
               "username": "superuser", "password": "SYS"}})
ds_id = ds["id"]
print("datasource:", ds_id)

step("② 连通测试")
print(call("POST", f"/datasources/{ds_id}/test"))

step("③ 选择 schema")
schemas = call("GET", f"/datasources/{ds_id}/schemas")
names = [s.get("schema") if isinstance(s, dict) else s for s in (schemas.get("items") or schemas or [])]
schema = "SQLUser" if "SQLUser" in names else (names[0] if names else "")
print("schemas:", names, "→ 使用", schema)

step("④ 选择数据表（4 张）")
tables = call("GET", f"/datasources/{ds_id}/tables?schema={schema}")
tbl_names = [t.get("table") for t in (tables.get("items") or [])]
print("available tables:", tbl_names)
wanted = [t for t in ("Patient", "Encounter", "Diagnosis", "MedicationOrder") if t in tbl_names]
picked = call("POST", f"/datasources/{ds_id}/tables",
              {"tables": [{"schema": schema, "table": t} for t in wanted]})
assets = picked.get("assets") or picked.get("items") or []
print("assets:", [(a.get("name"), len(a.get("fields") or [])) for a in assets])

step("⑤ 生成演示数据（3 位患者）")
print(call("POST", f"/datasources/{ds_id}/seed", {"patients": 3}))

step("⑥ 读取源资产（供 AI 匹配）")
asset_items = call("GET", f"/datasources/{ds_id}/assets")
assets = asset_items.get("items") or []
print("assets:", [(a.get("name"), len(a.get("fields") or [])) for a in assets])

step("⑦ 转换目标管理：新建 FHIR 目标")
tg = call("POST", "/targets", {
    "name": "FHIR 演示库", "type": "FHIR",
    "connection": {"base_url": FHIR_BASE, "username": "superuser", "password": "SYS"}})
target_id = tg["id"]
print("target:", target_id, "| entities:", len(tg.get("target", {}).get("tables") or []))

step("⑧ 读取目标实体（供 AI 匹配，UI 默认只勾已建模类型）")
MODELED = {"Patient", "Encounter", "Condition", "MedicationRequest", "Observation",
           "AllergyIntolerance", "Procedure", "DiagnosticReport", "Immunization",
           "Organization", "Practitioner"}
target_rows = (call("GET", "/targets").get("items") or [])
targets = [t for t in target_rows
           if (t.get("type") or "").upper() == "FHIR" and t.get("table") in MODELED]
print("target entities:", [t.get("table") for t in targets])

step("⑨ AI 智能匹配（Agent A + C1）")
rec = call("POST", "/ai/recommend", {"assets": assets, "targets": targets})
recs = rec.get("recommendations") or []
print("recommendations:", [(r.get("source"), r.get("target_table"),
                            len(r.get("field_mappings") or [])) for r in recs])
print("validation:", (rec.get("validation") or {}).get("ok"),
      "| issues:", len((rec.get("validation") or {}).get("issues") or []))

step("⑩ 保存转换关系")
saved = call("POST", "/mappings", {"mappings": recs})
mappings = saved.get("mappings") or []
print("saved mappings:", len(mappings))

step("⑪ 生成数据管道")
gen = call("POST", "/pipelines/generate", {
    "mappings": mappings, "source_id": ds_id, "target_id": target_id,
    "source_type": "SQL", "target_type": "FHIR"})
v = gen.get("validation") or {}
print("result:", gen.get("result"), "| production:", gen.get("production"))
print("validation ok:", v.get("ok"), "| errors:", v.get("error_count"),
      "| checks:", list((v.get("results") or {}).keys()))
print("ai:", json.dumps(gen.get("ai"), ensure_ascii=False)[:300])

summary = {"source_id": ds_id, "target_id": target_id,
           "mappings": len(mappings), "generate": {"result": gen.get("result"),
                                                   "validation_ok": v.get("ok")}}
open("/tmp/e2e_summary.json", "w").write(json.dumps(summary, ensure_ascii=False, indent=2))
print("\nE2E-GENERATE-DONE")
