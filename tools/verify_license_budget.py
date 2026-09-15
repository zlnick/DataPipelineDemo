# -*- coding: utf-8 -*-
"""验证「许可预算」：先后生成两条管道，每次都让旧管道组件让路，确保生成通畅。

背景：IRIS 社区版 KeyLicenseUnits=8，每个 Ens 业务主机常驻占 1 个许可单元；
多管道 Production 整体启动会耗尽许可 → 后端连不上 IRIS → 生成必失败。
生成流程现在会自动做许可预算（禁用非本次组件），本脚本验证该行为。
"""
import json
import urllib.error
import urllib.request

BASE = "http://localhost:5001/api"


def call(method, path, body=None, timeout=1800):
    """调用后端 API（HTTP 错误时打印响应体，便于定位）。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"[HTTP {exc.code}] {method} {path}\n{detail[:1500]}")


maps = (call("GET", "/mappings") or {}).get("data", {}).get("items", [])
soap_maps = [m for m in maps if m.get("target_type") == "SOAP"]
fhir_maps = [m for m in maps if m.get("target_type") == "FHIR"]

# 数据源 / 目标按类型动态解析（不写死 ID：重置环境后 ID 会变，写死会让 source_id 失效）
dss = (call("GET", "/datasources") or {}).get("data", {}).get("items", [])
tgs = (call("GET", "/targets") or {}).get("data", {}).get("items", [])


def pick(items, kind, pred=None):
    """按类型（可选附加条件）取第一个登记项，缺失显式报错。返回 (id, 记录)。"""
    for it in items:
        if it.get("type") == kind and (pred is None or pred(it)):
            rid = it.get("id") or it.get("target_id")
            if rid:
                return rid, it
    raise SystemExit(f"登记缺失: 找不到 type={kind} 的数据源/目标")


def _source_for(sm):
    """按映射源表反查所属 SQL 数据源（与后端 _datasource_for_mappings 同思路）。"""
    names = {str(m.get("source") or "").split(".")[0] for m in sm}
    for ds in dss:
        if ds.get("type") != "SQL":
            continue
        assets = (call("GET", f"/datasources/{ds['id']}/assets")
                  or {}).get("data", {}).get("items", [])
        if names & {str(a.get("name") or "") for a in assets}:
            return ds.get("id"), ds
    return pick(dss, "SQL")


sql_ds_id, sql_ds = _source_for(fhir_maps or soap_maps)
fhir_id, fhir_row = pick(tgs, "FHIR")
soap_id, soap_row = pick(tgs, "SOAP")
print(f"数据源: {sql_ds_id} {sql_ds.get('name')} | FHIR 目标: {fhir_id} "
      f"{fhir_row.get('target_name')} | SOAP 目标: {soap_id} {soap_row.get('target_name')}")


def run(label, group):
    """提交一组管道并打印许可预算与校验结果。"""
    print(f"\n=== {label} ===")
    resp = call("POST", "/pipelines/generate", {"pipelines": [group]})
    print("code:", resp.get("code"), "| message:", resp.get("message"))
    data = resp.get("data") or {}
    lb = data.get("license_budget") or {}
    print("result:", data.get("result"))
    print("许可预算: units=", lb.get("units"), "| active=", len(lb.get("active") or []),
          "| disabled=", lb.get("disabled"), "| over=", lb.get("over_capacity"))
    if lb.get("note"):
        print("note:", lb["note"])
    v = data.get("validation") or {}
    print("validation ok:", v.get("ok"), "| checks:", list((v.get("results") or {}).keys()))
    for i in (v.get("issues") or [])[:4]:
        print("  issue:", i.get("check"), i.get("severity"), str(i.get("message"))[:110])


if fhir_maps:
    run("管道1: SQL→FHIR", {
        "source_type": "SQL", "source_id": sql_ds_id,
        "target_type": "FHIR", "target_id": fhir_id, "mappings": fhir_maps})

if soap_maps:
    run("管道2: SQL→SOAP（追加，旧管道让路）", {
        "source_type": "SQL", "source_id": sql_ds_id,
        "target_type": "SOAP", "target_id": soap_id, "mappings": soap_maps})

items = (call("GET", "/pipelines/items") or {}).get("data") or {}
print("\n=== 最终组件状态 ===")
print("units=", items.get("units"), "enabled=", items.get("enabled_count"))
for it in items.get("items") or []:
    print("  ", "ON " if int(it.get("enabled") or 0) == 1 else "off", it["name"],
          "|", it["className"], "| 类别:", it.get("category") or "-")

# 管道实体（受管理持久对象）：类别 / 组件 / 状态
inst = (call("GET", "/pipelines/instances") or {}).get("data") or {}
print("\n=== 数据管道实体 ===")
print("instances=", len(inst.get("items") or []), "| active=", inst.get("active_count"))
for g in inst.get("groups") or []:
    print(f"  [{g['category']}] 活动={g['active']} 组件 {g['enabled_count']}/{g['component_count']}")
    for r in g.get("items") or []:
        print(f"     {r['id']}  {r['source_type']}→{r['target_type']}  "
              f"skill={r.get('design_skill')} status={r.get('status')} "
              f"gen={r.get('generation_count')} 组件={r.get('component_names')}")
