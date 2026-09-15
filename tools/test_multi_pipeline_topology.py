# -*- coding: utf-8 -*-
"""单元验证：多管道合并拓扑的「按管道类别命名」（消除名字漂移）+ Category 落地。

不调用 LLM：直接注入 ai_components（等价于 Agent B 已产出），只验证拓扑合并/命名/类别。
"""
from backend.routes.pipelines import build_multi_pipeline_topology

def build(groups_in):
    """用给定组构建拓扑（深拷贝输入，避免上一次调用就地改名的副作用）。"""
    import copy
    gs = copy.deepcopy(groups_in)
    topo = build_multi_pipeline_topology(gs)
    return topo, gs


def _tcn(comp):
    """取组件 settings 里 Host/TargetConfigNames 的值（校验「源 BS → 本管道 BP」）。"""
    for s in comp.get("settings") or []:
        if s.get("name") == "TargetConfigNames":
            return s.get("value")
    return None


groups = [
    {"source_type": "SQL", "target_type": "FHIR", "_category": "sql2fhir-patient-tx",
     "_sql2fhir": True,
     "ai_components": [
         {"type": "SQLService", "name": "SQLService_Patient",
          "className": "EnsLib.SQL.Service.GenericService", "settings": []},
         {"type": "HTTPOperation", "name": "HTTPOperation",
          "className": "EnsLib.HTTP.GenericOperation", "settings": []}]},
    {"source_type": "SQL", "target_type": "SOAP", "_category": "sql2soap",
     "target_config": {"service": "PatientService",
                       "bo_class": "PatientService.demo.soap.bont.PatientPort"},
     "ai_components": [
         {"type": "SQLService", "name": "SQLService_Patient",
          "className": "EnsLib.SQL.Service.GenericService", "settings": []},
         {"type": "SOAPOperation", "name": "SOAPOp_PatientService",
          "className": "PatientService.demo.soap.bont.PatientPort", "settings": []}]},
]

topo, gs = build(groups)
names = [c.get("name") for c in topo["components"]]
print("categories:", topo["categories"])
for c in topo["components"]:
    print(f"  {c.get('type'):16s} {c.get('name'):42s} category={c.get('category')}")

dupes = {n for n in names if names.count(n) > 1}
assert not dupes, f"组件名重复: {dupes}"
assert "SQLService_Patient__sql2fhir_patient_tx" in names, names
assert "SQLService_Patient__sql2soap" in names, names
assert "SQLService_Patient" not in names, "同名源 BS 未按类别改名（名字漂移）"
assert "SOAPOp_PatientService" in names, "SOAP 目标组件缺失"
# 共享基础设施只剩 JavaGateway；转换 BP 属各自管道（一条数据管道一个 BP 实例）
shared = [c for c in topo["components"] if c.get("category") == "shared"]
assert [c["type"] for c in shared] == ["JavaGateway"], shared
_bps = [c for c in topo["components"] if c.get("type") == "TransformProcess"]
assert [c["name"] for c in _bps] == ["TransformProcess__sql2soap"], _bps
assert [c["category"] for c in _bps] == ["sql2soap"], _bps
# 源 BS 的 TargetConfigNames 必须指向**本管道自己的** BP（否则多管道会互相投递）
_src_soap = next(c for c in topo["components"]
                 if c.get("name") == "SQLService_Patient__sql2soap")
assert _tcn(_src_soap) == "TransformProcess__sql2soap", _src_soap
assert gs[1].get("_bp_name") == "TransformProcess__sql2soap", gs[1].get("_bp_name")
assert gs[0]["_src_bn"] == "SQLService_Patient__sql2fhir_patient_tx", gs[0]["_src_bn"]
assert gs[1]["_src_bn"] == "SQLService_Patient__sql2soap", gs[1]["_src_bn"]
# 重复生成：同输入 → 同名字（确定性，消除名字漂移）
topo2, _ = build(groups)
names2 = [c.get("name") for c in topo2["components"]]
assert names2 == names, f"重复生成组件名不稳定:\n  {names}\n  {names2}"

# ------------------ 共享组件「按需携带」：纯 Skill 组不得携带 TransformProcess ------------------
# 自带聚合 BP 的 Skill 组（sql2fhir-patient-tx → SqlFhirPatientTxProcess）不走通用路由，
# 过去无条件携带 TransformProcess 会让"零使用者的 BP"白占 1 个许可单元（社区版仅 8 个）。
pure_skill = [g for g in groups if g["_category"] == "sql2fhir-patient-tx"]
topo3, _ = build(pure_skill)
names3 = [c.get("name") for c in topo3["components"]]
shared3 = [c for c in topo3["components"] if c.get("category") == "shared"]
print("\n[纯 sql2fhir 组] 组件:", names3)
assert "TransformProcess" not in names3, f"Skill 组不应携带通用路由 BP: {names3}"
assert [c["type"] for c in shared3] == ["JavaGateway"], shared3
assert any(c["type"] == "JavaGateway" for c in topo3["components"]), topo3["components"]

# 通用组（注册表规则路径）必须仍然携带 TransformProcess（源 BS 的 TargetConfigNames 指向它）
generic = [{"source_type": "SQL", "target_type": "SOAP", "_category": "sql2soap",
            "source_config": {"dsn": "CLINIC", "query": "SELECT * FROM CLINIC.Patient",
                              "key_field": "MRN"},
            "target_config": {"service": "PatientService", "endpoint": "http://x/mock"}}]
topo4, _ = build(generic)
names4 = [c.get("name") for c in topo4["components"]]
print("[通用 SQL→SOAP 组] 组件:", names4)
assert "TransformProcess__sql2soap" in names4, f"通用组缺自己的转换 BP: {names4}"
_bp4 = next(c for c in topo4["components"] if c.get("type") == "TransformProcess")
assert _bp4.get("category") == "sql2soap", _bp4
_src4 = next(c for c in topo4["components"] if c.get("type") == "SQLService")
assert _tcn(_src4) == _bp4.get("name"), f"源 BS 未指向本管道 BP: {_src4}"
assert any(c["type"] == "JavaGateway" for c in topo4["components"]), topo4["components"]

# ---------------- 同类别两条通用组：BP 各自独立（名字自动加序号，不抢同一个实例） ----------------
same_cat = [
    {"source_type": "SQL", "target_type": "SOAP", "_category": "sql2soap",
     "source_config": {"dsn": "CLINIC", "query": "SELECT * FROM CLINIC.Patient",
                       "key_field": "MRN"},
     "target_config": {"service": "PatientService"}},
    {"source_type": "SQL", "target_type": "SOAP", "_category": "sql2soap",
     "source_config": {"dsn": "CLINIC", "query": "SELECT * FROM CLINIC.PatientVisit",
                       "key_field": "VisitID"},
     "target_config": {"service": "PatientService"}},
]
topo5, gs5 = build(same_cat)
_bps5 = [c.get("name") for c in topo5["components"] if c.get("type") == "TransformProcess"]
print("[同类别两条管道] BP:", _bps5)
assert len(_bps5) == 2 and len(set(_bps5)) == 2, f"同类别两条管道必须各有一个 BP: {_bps5}"
assert all(c.get("category") == "sql2soap" for c in topo5["components"]
           if c.get("type") == "TransformProcess"), topo5["components"]
print("\n✓ 多管道命名/类别/共享基础设施（BP 每管道一个 + 仅 JavaGateway 共享）全部通过"
      "（重复生成名字稳定）")
