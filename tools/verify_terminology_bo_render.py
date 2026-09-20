# -*- coding: utf-8 -*-
"""零破坏验证：共享术语 BO 在 Production 渲染文本里的形态（不 Load、不启动、不写状态）。

1. 用平台新代码构建含 term_map 的拓扑（纯函数）；
2. 调 demo.PipelineGenerator.RenderProduction（**只渲染文本**，不落盘不启动）→ 检查
   `TerminologyOperation` Item / className / Category=shared / 三个 Host Settings 都在。
"""
import json
import sys

sys.path.insert(0, "/app")

from backend.routes import pipelines as P  # noqa: E402
from backend.services import iris_connector  # noqa: E402

TERM_MAP = [{"id": "R1", "source": "Diagnosis", "target_table": "Condition", "target_type": "FHIR",
             "field_mappings": [{"source": "Diagnosis.Code", "target": "code",
                                 "transform": "term_map:cn2snomed"}]}]
PLAIN = [{"id": "R2", "source": "Patient", "target_table": "Patient", "target_type": "FHIR",
          "field_mappings": [{"source": "Patient.id", "target": "id", "transform": None}]}]


def grp(maps, cat, tgt_type="FHIR"):
    return {"mappings": maps, "source_type": "SQL", "target_type": tgt_type,
            "source_config": {"dsn": "CLINIC"}, "target_config": {},
            "design_skill": cat, "_category": cat}


topo = P.build_multi_pipeline_topology([grp(TERM_MAP, "sql2fhir-patient-tx"), grp(PLAIN, "sql2fhir-2")])
PASS = FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ " + msg)
    else:
        FAIL += 1
        print("  ✗ " + msg)


topo_render = dict(topo)
topo_render["production"] = "demo.TmpTermBoVerify"      # 仅渲染文本，不 Load
src = iris_connector.class_method_value("demo.PipelineGenerator", "RenderProduction",
                                        json.dumps(topo_render, ensure_ascii=False))
print("渲染文本长度:", len(src or ""))
check(bool(src), "RenderProduction 返回非空")
check("TerminologyOperation" in (src or ""), "1 渲染文本含 TerminologyOperation Item")
check("demo.TerminologyOperation" in (src or ""), "2 className = demo.TerminologyOperation")
seg = ""
if src:
    i = src.find("TerminologyOperation")
    seg = src[max(0, i - 400): i + 1200]
    print("---- 渲染片段 ----")
    print(seg)
    print("------------------")
check("Category" in seg and "shared" in seg, "3 Category=shared（不参与许可调度/不被让路）")
for st in ("TermServer", "TermPort", "Timeout"):
    check(st in seg, "4 设置 %s 已渲染" % st)
check((src or "").count("TerminologyOperation") >= 1 and
      (src or "").count('Name="TerminologyOperation"') == 1,
      "5 全局仅 1 个实例（Name 出现 1 次）")

print("\n==== 结果: %d PASS / %d FAIL ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
