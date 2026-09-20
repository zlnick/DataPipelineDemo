# -*- coding: utf-8 -*-
"""单元验证（缺陷 J 回归）：多管道合并拓扑里 **FHIR 源的共享生产者 FHIRSyncService** 必须

1. 在合并后的组件清单里**恰好出现一次**（全局 1 个实例）；
2. `category == "shared"`（不参与许可调度、不被按管道类别让路停用）；
3. 不掺进任何一条管道的组件清单（否则会被整类让路带走 → 切换管道后新组零输入，静默失败）；
4. JavaGateway 同样只出现一次（原有行为不回归）。

背景（2026-09-16 实测）：把 FHIRSyncService 归为 infra 后，若忘记「全局追加一次」，
它会整个从 Production 消失 → FHIR 源不入队 → 所有 FHIR 管道零消息、校验全 FAIL。
本测试即锁死这条装配链。不调用 LLM（直接注入 ai_components）。
"""
from backend.routes.pipelines import (
    build_multi_pipeline_topology, _is_infra_component, _topology_groups)

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % msg)
    else:
        FAIL += 1
        print("  ✗ %s" % msg)


FHIR_SRC = {"type": "FHIRService", "name": "FHIRService", "className": "demo.FHIRService",
            "settings": [{"target": "Host", "name": "TargetConfigNames", "value": ""}]}
SYNC = {"type": "FHIRSyncService", "name": "FHIRSyncService", "className": "demo.FHIRSyncService",
        "settings": []}
ROUTER = {"type": "TransformProcess", "name": "TransformProcess",
          "className": "demo.TransformProcess", "settings": []}
SQLOP = {"type": "SQLOperation", "name": "SQLOp_Patient",
         "className": "EnsLib.SQL.Operation.GenericOperation", "settings": []}
HTTPOP = {"type": "HTTPOperation", "name": "HTTPOp_Patient",
          "className": "EnsLib.HTTP.GenericOperation", "settings": []}
JG = {"type": "JavaGateway", "name": "EnsLib.JavaGateway.Service",
      "className": "EnsLib.JavaGateway.Service", "settings": []}

groups = [
    {"source_type": "FHIR", "target_type": "DB", "_category": "fhir2db",
     "ai_components": [SYNC, FHIR_SRC, ROUTER, SQLOP, JG]},
    {"source_type": "FHIR", "target_type": "FHIR", "_category": "fhir2fhir",
     "ai_components": [SYNC, FHIR_SRC, ROUTER, HTTPOP, JG]},
]

topo = build_multi_pipeline_topology(groups)
comps = topo["components"]
names = [c.get("name") for c in comps]
by_name = {c.get("name"): c for c in comps}

print("\n[合并组件] %s" % names)
check(names.count("FHIRSyncService") == 1, "FHIRSyncService 恰好 1 个（全局共享实例）")
check(names.count("EnsLib.JavaGateway.Service") == 1, "JavaGateway 恰好 1 个")
check(str(by_name.get("FHIRSyncService", {}).get("category")) == "shared",
      "FHIRSyncService category=shared（不被按类别让路/不参与调度）")
check(_is_infra_component(by_name.get("FHIRSyncService", {})), "FHIRSyncService 判为 infra")
check(not _is_infra_component(by_name.get("TransformProcess__fhir2db", {})),
      "转换 BP 仍属本管道（不是 infra）")

cats = [str(c.get("category")) for c in comps]
check(cats.count("fhir2db") == 3 and cats.count("fhir2fhir") == 3,
      "两条管道各 3 个自有组件（源 BS/BP/目标 BO）")

plan_groups = {g["category"]: g["items"] for g in _topology_groups(topo)}
check("shared" not in plan_groups, "许可调度分组里没有 shared（不参与预算）")
check(sorted(plan_groups) == ["fhir2db", "fhir2fhir"], "调度分组 = 两条管道类别")
check(all("FHIRSyncService" not in v for v in plan_groups.values()),
      "FHIRSyncService 不在任何调度分组内（切换管道不会把它停掉）")

print("\n%s (%d PASS / %d FAIL)" % ("✓ 全部通过" if not FAIL else "✗ 存在失败", PASS, FAIL))
raise SystemExit(1 if FAIL else 0)
