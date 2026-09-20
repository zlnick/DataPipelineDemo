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
# ------------- 死配置根治：无转换 BP 的组不得登记 bp / bp_target（2026-09-17） -------------
# 背景：原实现无条件给每组登记 `_bp_name`（生成时无条件写 `bp[TransformProcess__<类别>]` +
# `bp_target[<源BS>]`）→ 自带聚合 BP 的 Skill 组（sql2fhir）没有该主机，登记了没有任何读者
# （实测残留 4 条死配置：诊断误读「登记数 ≠ 组件数」+ 悬空 `bp_target` 一旦被读到就是
# A6 式静默派发失败）。现按「本组是否真的有 TransformProcess 主机」登记（_has_router）。
print("\n[死配置根治] 组级登记标志:")
_, gs_pure = build(pure_skill)
print("  sql2fhir 组 _has_router=%s 有_bp_name=%s _bp_item=%s（仅内存派发目标，不写 global）" % (
    gs_pure[0].get("_has_router"), "_bp_name" in gs_pure[0], gs_pure[0].get("_bp_item")))
assert gs_pure[0].get("_has_router") is False, gs_pure[0].get("_has_router")
# 关键断言：`_bp_name` 不登记（它同时是生成端「写 bp[<名>]」与「收敛清理 keep 名单」的输入，
# 登记了不存在的名字 → 既写出死配置，又让清理器把死键保留下来）。
assert "_bp_name" not in gs_pure[0], "无转换 BP 的组不得登记 _bp_name（会写出死配置 + 污染清理 keep 名单）"
# 而 `_bp_items` / `_bp_item` 仍要登记（生成期悬空自检 + 布局 http_bo 兜底的输入，不写 global）：
assert isinstance(gs_pure[0].get("_bp_items"), dict), "派发目标表需保留（自检/布局输入）"
assert gs_pure[0].get("_bp_item"), "FHIR 派发名需保留（布局 http_bo 兜底）"
for _n, _g in enumerate(gs5):
    print("  通用组%d %s _has_router=%s _bp_name=%s" % (
        _n, _g.get("_category"), _g.get("_has_router"), _g.get("_bp_name")))
assert all(g.get("_has_router") for g in gs5), "通用组必须登记 _has_router=True"
assert all(g.get("_bp_name") for g in gs5), "通用组必须登记自己的 BP 名"
# 通用组：派发目标表仍在（BP 按**显式主机名**派发，缺陷 A6 的修复不能丢）
assert gs5[0].get("_bp_items"), "通用组必须写派发目标表（items）"
assert all(v for v in gs5[0]["_bp_items"].values()), gs5[0]["_bp_items"]

print("\n✓ 多管道命名/类别/共享基础设施（BP 每管道一个 + 仅 JavaGateway 共享）全部通过"
      "（重复生成名字稳定 + 无转换 BP 的组不产生死配置）")
