# -*- coding: utf-8 -*-
"""多管道**组件实例隔离**单测（缺陷 A4：同类别多组共用聚合 BP/BO → 串线）。

背景（2026-09-17，3 源 × 3 目标）：两个 SQL 源（USER / CLINIC）都选了 `sql2fhir-patient-tx`，
平台把它们的源 BS 按类别改名了，但 **Skill 自带的聚合 BP 及其 BO**（`SqlFhirPatientTxProcess` /
`HTTPOperation` / `SQLQueryOp_*`）没有 → 两个组共用同一个 BP 实例，而该 BP 的布局取自实例级
配置 `^demo.Config("sql2fhir","layout",<BP名>)` → 一条管道被另一条的布局驱动。

本单测不依赖 IRIS/LLM：直接构造两组 ai_components 走 `build_multi_pipeline_topology`，
断言「组件名唯一 / 组内引用自洽 / 每组聚合 BP 名可被上层拿去写实例级布局」。

用法（宿主或 backend 容器内均可）：
  python3 tools/test_pipeline_component_isolation.py
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else "/app/tools"
sys.path.insert(0, os.path.dirname(_HERE))

from backend.routes.pipelines import build_multi_pipeline_topology  # noqa: E402

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✅ %s %s" % (name, extra))
    else:
        FAIL += 1
        print("  ❌ %s %s" % (name, extra))


def _sql2fhir_components(patient="Patient", dsn="USER", key_col="ID"):
    """模拟 sql2fhir Skill executor 的组件（BS + 查询 BO + 聚合 BP + HTTP BO）。"""
    return [
        {"type": "SQLService", "name": "SQLService_%s" % patient,
         "className": "EnsLib.SQL.Service.GenericService",
         "settings": [{"target": "Adapter", "name": "DSN", "value": dsn},
                      {"target": "Adapter", "name": "Query",
                       "value": "SELECT * FROM SQLUser.%s" % patient},
                      {"target": "Adapter", "name": "KeyFieldName", "value": key_col},
                      {"target": "Adapter", "name": "JGService",
                       "value": "EnsLib.JavaGateway.Service"},
                      {"target": "Host", "name": "TargetConfigNames",
                       "value": "SqlFhirPatientTxProcess"}]},
        {"type": "SQLOperation", "name": "SQLQueryOp_Encounter",
         "className": "EnsLib.SQL.Operation.GenericOperation",
         "settings": [{"target": "Adapter", "name": "DSN", "value": dsn}]},
        {"type": "PatientTxProcess", "name": "SqlFhirPatientTxProcess",
         "className": "demo.SqlFhirPatientTxProcess", "settings": []},
        {"type": "HTTPOperation", "name": "HTTPOperation",
         "className": "EnsLib.HTTP.GenericOperation",
         "settings": [{"target": "Adapter", "name": "HTTPServer", "value": "localhost"}]},
    ]


def _group(source_id, components, source_type="SQL", target_type="FHIR",
           skill="sql2fhir-patient-tx", target_id="TG_FHIR",
           mapping=None, layout=None):
    g = {"source_id": source_id, "source_type": source_type, "target_type": target_type,
         "target_id": target_id, "design_skill": skill,
         "_category": skill, "ai_components": components,
         "mappings": [mapping or {"id": "M1", "source": "Patient",
                                  "target_table": "Patient", "target_type": target_type,
                                  "field_mappings": []}]}
    if skill == "sql2fhir-patient-tx":
        g["_sql2fhir"] = True          # Skill 组：拓扑直接采用 executor 产出的组件（不经注册表补全）
    if layout is not None:
        g["_layout"] = layout
    return g



print("=== A 两组 sql2fhir（不同源）→ 聚合 BP / BO 必须实例化隔离 ===")
topo = build_multi_pipeline_topology([
    _group("DS_USER", _sql2fhir_components("Patient", "USER", "ID")),
    _group("DS_CLINIC", _sql2fhir_components("Patient", "CLINIC", "MRN")),
])
comps = topo["components"]
names = [str(c.get("name")) for c in comps]
check("A1 组件名全局唯一", len(names) == len(set(names)),
      "count=%d unique=%d" % (len(names), len(set(names))))
bps = [str(c.get("name")) for c in comps if str(c.get("type")) == "PatientTxProcess"]
check("A2 聚合 BP 一管道一实例", len(bps) == 2 and len(set(bps)) == 2, "bps=%s" % bps)
bss = [c for c in comps if str(c.get("type")) == "SQLService"]
refs = [next((s.get("value") for s in (c.get("settings") or [])
              if s.get("name") == "TargetConfigNames"), "") for c in bss]
check("A3 源 BS 指向**自己的** BP", sorted(refs) == sorted(bps), "refs=%s" % refs)
dsns = {str(c.get("name")): next((s.get("value") for s in (c.get("settings") or [])
                                  if s.get("name") == "DSN"), "") for c in bss}
check("A4 源 BS 的 DSN 与各自源一致", sorted(dsns.values()) == ["CLINIC", "USER"],
      json.dumps(dsns, ensure_ascii=False))
htt = [str(c.get("name")) for c in comps if str(c.get("type")) == "HTTPOperation"]
check("A5 HTTP BO 也按实例隔离", len(htt) == 2, "http=%s" % htt)
qops = [str(c.get("name")) for c in comps if str(c.get("type")) == "SQLOperation"]
check("A6 查询 BO 也按实例隔离", len(qops) == 2 and len(set(qops)) == 2, "qops=%s" % qops)
check("A7 布局可按实例写入（每组 BP 名唯一且非空）", all(bps) and len(set(bps)) == len(bps))
# _own_names 由 build_multi_pipeline_topology 写回**入参 groups**（不是返回值）→ 用入参对象校验
_gs = [_group("DS_USER", _sql2fhir_components("Patient", "USER", "ID")),
       _group("DS_CLINIC", _sql2fhir_components("Patient", "CLINIC", "MRN"))]
topo_o = build_multi_pipeline_topology(_gs)
own_sets = [set(g.get("_own_names") or []) for g in _gs]
check("A8 每组记录专属组件名（互不相同、非空）",
      all(own_sets) and own_sets[0] != own_sets[1],
      "own0=%s own1=%s" % (sorted(own_sets[0]), sorted(own_sets[1])))
_prod_names = {str(c.get("name")) for c in topo_o["components"]}
check("A9 专属组件名都在拓扑里（供管道实体精确归属）",
      all(n in _prod_names for s in own_sets for n in s))

print("\n=== B 单组 sql2fhir → 名字保持原样（不引入无谓后缀） ===")
topo1 = build_multi_pipeline_topology([_group("DS_USER", _sql2fhir_components())])
n1 = [str(c.get("name")) for c in topo1["components"]]
check("B1 单组时聚合 BP 名不变", "SqlFhirPatientTxProcess" in n1, "names=%s" % n1[:5])
check("B2 单组时源 BS 名不变", "SQLService_Patient" in n1)
check("B3 单组时源 BS 仍指向该 BP",
      any(str(c.get("name")) == "SQLService_Patient"
          and next((s.get("value") for s in (c.get("settings") or [])
                    if s.get("name") == "TargetConfigNames"), "") == "SqlFhirPatientTxProcess"
          for c in topo1["components"]))



print("\n=== C 两组通用组（同类别 sql2db，不同源）→ 目标 BO 亦隔离 ===")


def _generic_group(source_id, dsn):
    comps = [
        {"type": "SQLService", "name": "SQLService_Patient",
         "className": "EnsLib.SQL.Service.GenericService",
         "settings": [{"target": "Adapter", "name": "DSN", "value": dsn},
                      {"target": "Adapter", "name": "Query",
                       "value": "SELECT * FROM SQLUser.Patient"},
                      {"target": "Host", "name": "TargetConfigNames",
                       "value": "TransformProcess__sql2db"}]},
        {"type": "SQLOperation", "name": "SQLOp_PatientSource",
         "className": "EnsLib.SQL.Operation.GenericOperation",
         "settings": [{"target": "Adapter", "name": "DSN", "value": dsn}]},
    ]
    return _group(source_id, comps, source_type="SQL", target_type="DB",
                  skill="sql2db", target_id="TG_DB",
                  mapping={"id": "M2", "source": "Patient", "target_table": "PatientSource",
                           "target_type": "DB", "field_mappings": []})


topo2 = build_multi_pipeline_topology([_generic_group("DS_USER", "USER"),
                                       _generic_group("DS_CLINIC", "CLINIC")])
n2 = [str(c.get("name")) for c in topo2["components"]]
check("C1 组件名唯一", len(n2) == len(set(n2)), "names=%s" % n2)
check("C2 目标 BO 按实例隔离（SQLOp_PatientSource 不再共用）",
      len([x for x in n2 if x.startswith("SQLOp_PatientSource")]) == 2,
      "ops=%s" % [x for x in n2 if x.startswith("SQLOp_PatientSource")])
check("C3 每组一个路由 BP",
      len([x for x in n2 if x.startswith("TransformProcess__sql2db")]) == 2,
      "bps=%s" % [x for x in n2 if x.startswith("TransformProcess__sql2db")])

print("\n=== D 重复生成确定性（同输入两次结果一致） ===")
again = [str(c.get("name")) for c in build_multi_pipeline_topology([
    _generic_group("DS_USER", "USER"), _generic_group("DS_CLINIC", "CLINIC")])["components"]]
check("D1 命名确定（与本次一致）", again == n2, "again=%s" % again)

print("\n=== E 显式派发目标（缺陷 A6：改名后 BP 引用必须跟着走） ===")


def _layout_probe(patient="Patient"):
    """模拟 executor 产出的布局（含冲突的子表查询 BO 与 FHIR Operation 名）。"""
    return {"design_skill": "sql2fhir-patient-tx", "patient_table": patient,
            "patient_id_col": "ID",
            "query_bos": [{"source_table": "Encounter", "target_resource": "Encounter",
                           "bo_name": "SQLQueryOp_Encounter", "fk_col": "PatientID",
                           "depends_on": patient, "depth": 1}],
            "bundle": {"resource_order": [patient, "Encounter"], "entries": []},
            "http_bo": "HTTPOperation"}


_gs2 = [_group("DS_USER", _sql2fhir_components("Patient", "USER", "ID"), layout=_layout_probe()),
        _group("DS_CLINIC", _sql2fhir_components("Patient", "CLINIC", "MRN"), layout=_layout_probe())]
topo_e = build_multi_pipeline_topology(_gs2)
_names_e = {str(c.get("name")) for c in topo_e["components"]}
_e_items = [dict(g.get("_bp_items") or {}) for g in _gs2]
check("E1 FHIR 组有显式 Operation 目标（item）且 items 为字典",
      all(g.get("_bp_item") for g in _gs2)
      and all(isinstance(g.get("_bp_items"), dict) for g in _gs2),
      "_bp_item=%s" % [g.get("_bp_item") for g in _gs2])
_gs3 = [_generic_group("DS_USER", "USER"), _generic_group("DS_CLINIC", "CLINIC")]
build_multi_pipeline_topology(_gs3)
_db_items = [dict(g.get("_bp_items") or {}) for g in _gs3]
check("E1b DB 组的派发表键=目标表名、值=本组实例名",
      [v for m in _db_items for v in m.values()]
      == ["SQLOp_PatientSource__sql2db", "SQLOp_PatientSource__sql2db_2"]
      and all(list(m.keys()) == ["PatientSource"] for m in _db_items),
      "%s" % _db_items)
check("E2 派发目标都在拓扑里（不悬空）",
      all(v in _names_e for m in _e_items for v in m.values()), "names=%s" % sorted(_names_e))
check("E3 两组的 FHIR Operation 目标互不相同",
      len({str(g.get("_bp_item")) for g in _gs2}) == 2,
      "items=%s" % [g.get("_bp_item") for g in _gs2])
_e_lays = [g.get("_layout") for g in _gs2]
check("E4 布局 query_bos 的 bo_name 已对齐最终组件名",
      all(str(b.get("bo_name")) in _names_e for lay in _e_lays
          for b in ((lay or {}).get("query_bos") or [])),
      "%s" % [[b.get("bo_name") for b in (lay or {}).get("query_bos", [])] for lay in _e_lays])
check("E5 布局 http_bo 已对齐本组 FHIR Operation 名",
      [str((lay or {}).get("http_bo")) for lay in _e_lays] == [str(g.get("_bp_item")) for g in _gs2],
      "%s" % [str((lay or {}).get("http_bo")) for lay in _e_lays])

from backend.routes.pipelines import _assert_dispatch_targets, _dispatch_targets  # noqa: E402

_named = [{"type": "SQLOperation", "name": "WritePatient",
           "settings": [{"target": "Host", "name": "Query",
                         "value": "INSERT OR UPDATE INTO PatientSource (ID) VALUES (?)"}]}]
_it, _one = _dispatch_targets(_named)
check("E6 BO 非 SQLOp_{表} 约定名时，键从 Query 的 INTO 解析",
      _it == {"PatientSource": "WritePatient"} and _one == "WritePatient",
      "items=%s single=%s" % (_it, _one))
_querybo = [{"type": "SQLOperation", "name": "SQLQueryOp_Encounter",
             "settings": [{"target": "Host", "name": "Query",
                           "value": "SELECT * FROM SQLUser.Encounter WHERE EncounterID = ?"}]}]
check("E7 查询 BO（SELECT）不进派发表（不是路由 BP 的目标）",
      _dispatch_targets(_querybo) == ({}, ""), "%s" % (_dispatch_targets(_querybo),))
try:
    _assert_dispatch_targets([{"_category": "sql2db", "_bp_items": {"PatientSource": "SQLOp_Missing"},
                               "_bp_item": "SQLOp_Missing"}],
                             [{"name": "SQLOp_PatientSource__sql2db"}])
    _e8 = False
except ValueError as _exc:
    _e8 = "SQLOp_Missing" in str(_exc)
check("E8 派发目标悬空 → 生成期显式失败（不再静默零落地）", _e8)
check("E9 派发目标存在 → 不抛错", (lambda: (  # noqa: E731
    _assert_dispatch_targets([{"_category": "sql2db", "_bp_items": {"PatientSource": "SQLOp_OK"},
                               "_bp_item": "SQLOp_OK"}], [{"name": "SQLOp_OK"}]),
    True)[1])())

print("\n结论: %d PASS / %d FAIL" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
