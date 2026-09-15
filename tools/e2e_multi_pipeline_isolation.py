# -*- coding: utf-8 -*-
"""e2e 验证「一条数据管道一个转换 BP」的隔离与消息分流。

场景（两条管道、同一 SQL 源、不同目标）：
  · 管道 A：CLINIC.Patient → SOAP（mock AddPatient → SQLUser.PatientEntity）
  · 管道 B：CLINIC.Patient → DB（USER.Patient，JDBC UPSERT）
期望：
  1. 两条管道各有**自己的**转换 BP（`TransformProcess__sql2soap` / `__sql2db`），
     源 BS 也按类别命名（`SQLService_Patient__sql2soap` / `__sql2db`）→ 各自独立扫描凭证、互不顶掉；
  2. `shared` 类别只剩 JavaGateway；BP 属各自管道（随管道启停、许可随管道释放）；
  3. 消息分流：同一批源数据，SOAP 侧落 `SQLUser.PatientEntity`、DB 侧落 `SQLUser.Patient`，互不串线。

用法（宿主直接跑；会调用 LLM 设计管道，耗时数分钟）：
    python3 tools/e2e_multi_pipeline_isolation.py [--patients 2] [--skip-seed]
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://localhost:5001/api"
CLINIC_JDBC = "jdbc:IRIS://iris:1972/CLINIC"
LOCAL_JDBC = "jdbc:IRIS://iris:1972/USER"
DRIVER = "com.intersystems.jdbc.IRISDriver"


def call(method, path, body=None, timeout=2400):
    """调用后端 API；HTTP 错误直接抛错并带响应体（便于定位）。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"[FAIL] {method} {path} -> HTTP {exc.code}\n{detail[:1200]}")
    if isinstance(payload, dict) and payload.get("code") not in (0, None):
        raise SystemExit(f"[FAIL] {method} {path} -> {payload.get('message')}")
    return payload.get("data") if isinstance(payload, dict) else payload


def step(msg: str) -> None:
    """打印阶段标题（flush 便于后台跟踪）。"""
    print("\n==== " + msg + " ====", flush=True)


def items(path, key="items"):
    """取列表接口的 items（兼容 data.items / data 直出列表）。"""
    data = call("GET", path) or {}
    if isinstance(data, dict):
        return data.get(key) or []
    return data or []


def _generate_and_verify(ds_id, soap_id, db_id, m_soap, m_db) -> None:
    """一次提交两条管道并复核 BP 隔离（生成 + Production 事实 + 管道实体）。"""
    step("⑦ 一次提交两条管道（sql2soap + sql2db）→ 生成并启动")
    t0 = time.time()
    data = call("POST", "/pipelines/generate", {"pipelines": [
        {"source_type": "SQL", "source_id": ds_id, "target_type": "SOAP",
         "target_id": soap_id, "mappings": [m_soap]},
        {"source_type": "SQL", "source_id": ds_id, "target_type": "DB",
         "target_id": db_id, "mappings": [m_db]},
    ]}) or {}
    lb = data.get("license_budget") or {}
    print(f"result={data.get('result')} 耗时={time.time() - t0:.1f}s", flush=True)
    print("许可预算:", {k: lb.get(k) for k in ("units", "active", "disabled", "over_capacity")}, flush=True)
    v = data.get("validation") or {}
    print("validation ok:", v.get("ok"), "| checks:", list((v.get("results") or {}).keys()), flush=True)
    print("ai:", json.dumps(data.get("ai") or {}, ensure_ascii=False)[:300], flush=True)

    step("⑧ 组件归属与 BP 隔离（Production 事实）")
    prod = items("/pipelines/items")
    for it in prod:
        print(f"  [{it.get('category')}] {it.get('name'):<38} {it.get('className')}", flush=True)
    bp_names = [i["name"] for i in prod
                if str(i.get("className") or "").endswith("TransformProcess")]
    cats = {i.get("category") for i in prod if i.get("name") in bp_names}
    shared = [i["name"] for i in prod if i.get("category") == "shared"]
    print("\n转换 BP 实例:", bp_names, "| 其类别:", sorted(cats), flush=True)
    print("shared 组件:", shared, flush=True)

    step("⑨ 数据管道实体（各自 BP / 组件）")
    for inst in items("/pipelines/instances"):
        print(f"  {inst.get('id')} {inst.get('status')} own={inst.get('component_names')} "
              f"shared={inst.get('shared_component_names')}", flush=True)

    if len(bp_names) != 2 or len(set(bp_names)) != 2 or shared != ["EnsLib.JavaGateway.Service"]:
        raise SystemExit(f"[FAIL] 期望两个独立转换 BP + shared 仅 JavaGateway: {bp_names} / {shared}")
    print("\n✓ 一管道一 BP：两条管道各自 BP、shared 仅 JavaGateway"
          "（消息分流复核见 /api/pipelines/logs、/target-data）", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--patients", type=int, default=2, help="seed 的患者数")
    ap.add_argument("--skip-seed", action="store_true", help="跳过造数（复用既有 CLINIC 数据）")
    ap.add_argument("--reuse", action="store_true",
                    help="复用已登记的数据源/目标/映射（只跑生成+验证，便于反复验证）")
    args = ap.parse_args()

    if args.reuse:
        step("（--reuse）复用已登记：SQL 源 / SOAP 目标 / DB 目标 / 两条映射")
        dss = items("/datasources")
        tgs = items("/targets")
        saved = {m.get("id"): m for m in items("/mappings")}
        ds_id = next((d.get("id") or d.get("datasource_id") for d in dss
                      if d.get("type") == "SQL"), "")
        soap_id = next((t.get("id") or t.get("target_id") for t in tgs
                        if t.get("type") == "SOAP"), "")
        db_id = next((t.get("id") or t.get("target_id") for t in tgs
                      if t.get("type") == "DB"), "")
        m_soap, m_db = saved.get("M_ISO_SOAP"), saved.get("M_ISO_DB")
        if not (ds_id and soap_id and db_id and m_soap and m_db):
            raise SystemExit(f"[FAIL] 复用失败: ds={ds_id} soap={soap_id} db={db_id} "
                             f"maps={sorted(saved)}")
        print(f"source={ds_id} SOAP={soap_id} DB={db_id}", flush=True)
        _generate_and_verify(ds_id, soap_id, db_id, m_soap, m_db)
        return

    step("① 登记 CLINIC SQL 源 + 连通测试")
    ds = call("POST", "/datasources", {
        "name": "CLINIC 演示源", "type": "SQL",
        "config": {"jdbc_url": CLINIC_JDBC, "driver_class": DRIVER,
                   "username": "superuser", "password": "SYS"}})
    ds_id = ds["id"]
    print("datasource:", ds_id, flush=True)
    print(call("POST", f"/datasources/{ds_id}/test"), flush=True)
    schemas = call("GET", f"/datasources/{ds_id}/schemas") or {}
    names = [s.get("schema") if isinstance(s, dict) else s
             for s in (schemas.get("items") or [])] if isinstance(schemas, dict) else list(schemas)
    schema = "SQLUser" if "SQLUser" in names else (names[0] if names else "SQLUser")
    print("schemas:", names, "→", schema, flush=True)

    step("② 选表（Patient）+ 分析列（源资产）")
    picked = call("POST", f"/datasources/{ds_id}/tables",
                  {"tables": [{"schema": schema, "table": "Patient"}]})
    assets = (picked or {}).get("assets") or (picked or {}).get("items") or []
    if not assets:
        assets = items(f"/datasources/{ds_id}/assets")
    asset_name = str((assets[0] or {}).get("name") or "Patient")
    print("assets:", [(a.get("name"), len(a.get("fields") or [])) for a in assets], flush=True)

    if not args.skip_seed:
        step(f"③ 造 CLINIC 演示数据（{args.patients} 位患者）")
        print(call("POST", f"/datasources/{ds_id}/seed", {"patients": args.patients}), flush=True)

    step("④ 登记 DB 目标（USER 库）+ 选表 Patient")
    db = call("POST", "/targets", {
        "name": "本地目标库", "type": "DB",
        "connection": {"jdbc_url": LOCAL_JDBC, "driver_class": DRIVER,
                       "username": "superuser", "password": "SYS"}})
    db_id = db["id"]
    print("db target:", db_id, flush=True)
    print(call("POST", f"/targets/{db_id}/tables",
               {"tables": [{"schema": schema, "table": "Patient"}]}), flush=True)

    step("⑤ 登记 SOAP 目标（WSDL 导入 BO，内置示例 AddPatient）")
    soap = call("POST", "/targets", {
        "name": "HIS PatientService", "type": "SOAP",
        "connection": {"wsdl": "/tmp/patient.wsdl", "service": "PatientService",
                       "packages": "demo.soap.bont"}})
    soap_id = soap["id"]
    soap_entities = soap.get("entities") or []
    entity = str((soap_entities[0] or {}).get("table")
                 or (soap_entities[0] or {}).get("entity_name") or "AddPatientRequest")
    print("soap target:", soap_id, "bo:", soap.get("bo_class"), "entity:", entity, flush=True)

    step("⑥ 保存两条转换关系（SOAP 请求元素 / DB 目标表）")
    maps_payload = [
        {"id": "M_ISO_SOAP", "source": asset_name, "target_table": entity,
         "target_type": "SOAP",
         "field_mappings": [
             {"source": "ID", "target": "PatientNo"},
             {"source": "FamilyName", "target": "FullName"},
             {"source": "Gender", "target": "Gender"}]},
        {"id": "M_ISO_DB", "source": asset_name, "target_table": "Patient",
         "target_type": "DB",
         "field_mappings": [
             {"source": "ID", "target": "ID"},
             {"source": "FamilyName", "target": "FamilyName"},
             {"source": "GivenName", "target": "GivenName"},
             {"source": "Gender", "target": "Gender"},
             {"source": "BirthDate", "target": "BirthDate"},
             {"source": "Phone", "target": "Phone"},
             {"source": "Address", "target": "Address"},
             {"source": "City", "target": "City"}]},
    ]
    call("POST", "/mappings", {"mappings": maps_payload})
    saved = {m.get("id"): m for m in items("/mappings")}
    m_soap, m_db = saved.get("M_ISO_SOAP"), saved.get("M_ISO_DB")
    if not (m_soap and m_db):
        raise SystemExit(f"[FAIL] 映射未保存成功: 已有 {sorted(saved)}")

    _generate_and_verify(ds_id, soap_id, db_id, m_soap, m_db)


if __name__ == "__main__":
    main()
