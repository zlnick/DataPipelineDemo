# -*- coding: utf-8 -*-
"""共享术语 BO 注册回归（2026-09-18）：术语转换能力做成 IRIS 共享 BO 后的**平台侧不变量**。

纯离线（不调 LLM、不连 IRIS、不写数据）——只跑拓扑构建/分类/校验这些纯函数。

锁定：
A. 注册表口径：TerminologyOperation 是 role=infra（**不进** Agent 可选枚举，但**进** get_common_components
   → 拓扑校验放行）；
B. 有 term_map 决策 → 单管道拓扑自动挂上共享术语 BO（category=shared）；
C. 无 term_map 决策 → 不挂（不白占 1 个许可单元）；
D. 多管道：任一组有 term_map 就全局 1 个实例（两组都有仍只有 1 个，跨管道共享）；
E. 基础设施判定与许可口径：_is_infra_component=True、不进 _active_items_from_topology、
   不进 _topology_groups（不参与许可调度、不被按类别让路停用）；
F. 拓扑校验放行（不报未知组件类型）；
G. 组件 settings 是 Host 级（TermServer/TermPort/Timeout），可被 Production 覆盖。
"""
import sys

sys.path.insert(0, "/app")

from backend.routes import pipelines as P  # noqa: E402
from backend.services import pipeline_validator as PV  # noqa: E402
from backend.services import type_registry as T  # noqa: E402

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ " + msg)
    else:
        FAIL += 1
        print("  ✗ " + msg)


TERM_MAP = [{"id": "R1", "source": "Diagnosis", "target_table": "Condition", "target_type": "FHIR",
             "field_mappings": [{"source": "Diagnosis.Code", "target": "code",
                                 "transform": "term_map:cn2snomed"}]}]
PLAIN = [{"id": "R1", "source": "Patient", "target_table": "Patient", "target_type": "FHIR",
          "field_mappings": [{"source": "Patient.id", "target": "id", "transform": None}]}]

print("==== A. 注册表口径（infra vs AI 可选） ====")
avail = [c["type"] for c in T.get_available_components()]
check("TerminologyOperation" not in avail, "A1 不进 Agent 可选枚举（available_components）")
check("TerminologyOperation" in [c["type"] for c in T.get_common_components()],
      "A2 进 get_common_components（拓扑校验放行）")
tpl = next(c for c in T.get_common_components() if c["type"] == "TerminologyOperation")
check(tpl["className"] == "demo.TerminologyOperation" and tpl.get("role") == "infra",
      "A3 className=demo.TerminologyOperation、role=infra")
check("JavaGateway" in avail, "A4 JavaGateway 仍是 AI 可选（未误伤既有口径）")

print("\n==== B/C. 单管道拓扑：按 term_map 决策挂/不挂 ====")
topo_term = P.build_pipeline_topology(TERM_MAP, source_type="SQL", target_type="FHIR")
names = [c["name"] for c in topo_term["components"]]
check("TerminologyOperation" in names, "B1 有 term_map → 拓扑含共享术语 BO（%s）" % names)
tcomp = next(c for c in topo_term["components"] if c["name"] == "TerminologyOperation")
check(tcomp.get("category") == "shared" or P._is_infra_component(tcomp),
      "B2 归 shared（category=%s）" % tcomp.get("category"))
topo_plain = P.build_pipeline_topology(PLAIN, source_type="SQL", target_type="FHIR")
check("TerminologyOperation" not in [c["name"] for c in topo_plain["components"]],
      "C1 无 term_map → 不挂（省 1 个许可单元）")

print("\n==== D. 多管道：跨管道共享 1 个实例 ====")


def _grp(maps, cat):
    return {"mappings": maps, "source_type": "SQL", "target_type": "FHIR",
            "source_config": {}, "target_config": {}, "design_skill": cat, "_category": cat}


mt = P.build_multi_pipeline_topology([_grp(TERM_MAP, "g1"), _grp(TERM_MAP, "g2")])
mnames = [c["name"] for c in mt["components"]]
check(mnames.count("TerminologyOperation") == 1,
      "D1 两组都有 term_map → 全局仍 1 个实例（计数=%d）" % mnames.count("TerminologyOperation"))
mt2 = P.build_multi_pipeline_topology([_grp(TERM_MAP, "g1"), _grp(PLAIN, "g2")])
check([c["name"] for c in mt2["components"]].count("TerminologyOperation") == 1,
      "D2 仅一组有 → 1 个（任一组需要即挂）")
mt3 = P.build_multi_pipeline_topology([_grp(PLAIN, "g1")])
check("TerminologyOperation" not in [c["name"] for c in mt3["components"]],
      "D3 都没有 → 不挂")

print("\n==== E. 基础设施与许可口径 ====")
check(P._is_infra_component(tcomp), "E1 _is_infra_component=True（与 JavaGateway 同级）")
check(P._is_shared_component(tcomp), "E2 _is_shared_component=True")
check("TerminologyOperation" not in P._active_items_from_topology(topo_term),
      "E3 不进 _active_items_from_topology（许可预算不计它）")
_grp_items = [n for g in P._topology_groups(topo_term) for n in g["items"]]
check("TerminologyOperation" not in _grp_items,
      "E4 不进 _topology_groups（不参与许可调度，%s）" % _grp_items)
susp = P._mark_suspended_components(topo_term, {"suspended": ["shared", "unknown", "sql2fhir"]})
check("TerminologyOperation" not in susp, "E5 不会被许可调度停用（%s）" % susp)
keep = P._pipeline_categories("sql2fhir-patient-tx")
check(keep == ["sql2fhir-patient-tx"], "E6 许可 keep 名单不含 shared（%s）" % keep)

print("\n==== F. 拓扑校验放行 ====")
tp = PV.check_pipeline_topology(topo_term, source_type="SQL", target_type="FHIR")
_tp_issues = tp.get("issues") if isinstance(tp, dict) else tp
bad = [i for i in (_tp_issues or []) if "TerminologyOperation" in str(i)]
check(not bad, "F1 check_pipeline_topology 不报该组件（issues=%s）" % (_tp_issues or [])[:2])
check(tp.get("ok") if isinstance(tp, dict) else True,
      "F2 拓扑校验整体通过（ok=%s）" % (tp.get("ok") if isinstance(tp, dict) else "n/a"))

print("\n==== G. 组件 settings ====")
settings = {s["name"]: s for s in tcomp.get("settings") or []}
check(set(settings) >= {"TermServer", "TermPort", "Timeout"},
      "G1 含 TermServer/TermPort/Timeout（%s）" % sorted(settings))
check(all(s.get("target") == "Host" for s in settings.values()),
      "G2 均为 Host 级设置（Production 可覆盖）")

print("\n==== 结果: %d PASS / %d FAIL ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
