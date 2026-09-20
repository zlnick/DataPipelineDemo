# -*- coding: utf-8 -*-
"""术语门禁**默认降级 + 严格开关 + 待办清单**回归（2026-09-18，纯离线：不调 LLM、不碰 IRIS）。

锁定不变量：
A. 无 term_map 决策 → `_term_precheck` 报 skipped（完全不触发术语服务器）；
B. **缺映射（默认口径）** → `ok=True`（**放行**）+ `todo` 清单 + 说明含"默认降级放行"与补录命令 → 生成不中止；
C. **严格模式**（`strict=True`）→ `ok=False` + 说明含"严格模式" → 调用方 400（TERM_MAP_INCOMPLETE）；
D. 盘点异常（服务器不可达）→ 默认口径仍放行（运行期会降级）；严格模式则失败；
E. `_term_summary`（生成后只读复核）→ 有 todo 时并入 validation warning（`check=term_map_todo`）且**不判失败**；
F. 补录指引口径：含 `term_map_build.py` 且写明"补录后无需重新生成"。
"""
import sys

sys.path.insert(0, "/app")

from backend.routes import pipelines as P  # noqa: E402
from backend.services import term_precheck as TP  # noqa: E402

PASS = FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ " + msg)
    else:
        FAIL += 1
        print("  ✗ " + msg)


MAPPINGS = [{"id": "R1", "source": "Diagnosis", "target_table": "Condition", "target_type": "FHIR",
             "field_mappings": [{"source": "Diagnosis.Code", "target": "code",
                                 "transform": "term_map:cn2snomed"}]}]
_GATE_WITH_TODO = {"ok": False, "skipped": "", "jobs": ["Diagnosis:cn2snomed"],
                   "pairs": [{"skill": "cn2snomed", "catalogOk": True}], "catalogOk": True,
                   "covered": 2, "negative": 1, "missing": ["cn2snomed:ZZZ.999"],
                   "todo": ["cn2snomed:ZZZ.999"], "pending": [], "unresolved": [],
                   "namespaces": {"CLINIC": [("Diagnosis", "cn2snomed")]}, "notes": [],
                   "items": {}, "sources": {}, "blocking": 1, "error": ""}
_GATE_NO_JOB = {"ok": True, "skipped": "无 term_map 术语映射指令", "jobs": [], "pairs": [],
                "catalogOk": True, "covered": 0, "negative": 0, "missing": [], "todo": [],
                "pending": [], "unresolved": [], "namespaces": {}, "notes": [], "items": {},
                "sources": {}, "blocking": 0, "error": ""}

_real = TP.precheck


def _stub(gate):
    TP.precheck = lambda mappings=None: dict(gate)


print("==== A. 无 term_map 决策 → skipped（不触发术语服务器） ====")
_stub(_GATE_NO_JOB)
g = P._term_precheck(MAPPINGS)
check(g["ok"] and g["skipped"], "A1 ok=True 且 skipped（%s）" % g.get("skipped"))
check(g.get("todo") == [], "A2 todo 为空")
check(g.get("message") == "", "A3 无提示信息（不打扰）")

print("\n==== B. 缺映射：**默认放行** + 待办清单 ====")
_stub(_GATE_WITH_TODO)
g = P._term_precheck(MAPPINGS)
check(g["ok"] is True, "B1 ok=True（**不中止生成**）")
check(g["strict"] is False, "B2 strict=False（默认口径）")
check(g["todo"] == ["cn2snomed:ZZZ.999"], "B3 待办清单 = %s" % g["todo"])
check("默认降级放行" in g["message"], "B4 说明含『默认降级放行』")
check("meta.tag" in g["message"], "B5 说明含降级形态（meta.tag）")
check("term_map_build.py" in g["message"], "B6 说明含补录命令")

print("\n==== C. 严格模式：缺映射 → 中止（TERM_MAP_INCOMPLETE） ====")
g = P._term_precheck(MAPPINGS, strict=True)
check(g["ok"] is False, "C1 ok=False（严格模式中止）")
check(g["strict"] is True, "C2 strict=True")
check("严格模式" in g["message"], "C3 说明含『严格模式』")
check("term_map_build.py" in g["message"], "C4 仍给出补录指引")

print("\n==== D. 盘点异常：默认放行 / 严格失败 ====")


def _boom(mappings=None):
    raise RuntimeError("术语服务器不可达（stub）")


TP.precheck = _boom
g1 = P._term_precheck(MAPPINGS)
check(g1["ok"] is True, "D1 默认口径：盘点异常仍放行（运行期降级）")
check("默认放行" in g1["message"], "D2 说明显式标注『默认放行』（不静默）")
g2 = P._term_precheck(MAPPINGS, strict=True)
check(g2["ok"] is False, "D3 严格模式：盘点异常 → 不通过")

print("\n==== E. 生成后复核 _term_summary：todo → validation warning（不判失败） ====")
TP.precheck = lambda mappings=None: dict(_GATE_WITH_TODO)
validation = {"ok": True, "issues": []}
s = P._term_summary(MAPPINGS, {}, validation)
check(s["todo"] == ["cn2snomed:ZZZ.999"], "E1 summary.todo = %s" % s["todo"])
check(s["ok"] is False, "E2 summary.ok=False（仅表示有 todo）")
check(any(i.get("check") == "term_map_todo" for i in validation["issues"]),
      "E3 validation 并入 term_map_todo warning（%s）" % [i.get("check") for i in validation["issues"]])
check(all(i.get("severity") == "warning" for i in validation["issues"]),
      "E4 只告警不判失败（不做 error）")
check("共享 BO" in s["note"], "E5 note 说明运行期由共享 BO 实时查询")

print("\n==== F. 补录指引口径 ====")
h = TP.hint()
check("term_map_build.py" in h, "F1 hint 含补录命令")
check("无需重新生成" in h, "F2 hint 写明『补录后无需重新生成』")
TP.precheck = _real
print("\n==== 结果: %d PASS / %d FAIL ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
