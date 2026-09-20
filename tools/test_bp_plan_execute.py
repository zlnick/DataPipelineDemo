# -*- coding: utf-8 -*-
"""离线回归：**Plan → Execute** 链（计划 schema / 骨架 / 方法级准入 / 断点续跑 / 失败显式）。

覆盖（2026-09-19 新增，配合"整类生成太慢"的改造）：
  A 计划 schema 校验（结构/命名唯一/父类 API 存在/OnRequest/规模上限）
  B 骨架生成（类头 + 全部签名 + 占位实现）
  C 单方法静态准入（多做方法/类头/Try 内带参 Quit/未定义助手/response 类型）
  D 编排：全部方法成功 / 断点续跑跳过已 ok / 静态准入失败重试后显式失败 / 单测介入
  E 单测汇总与 skipped 语义
用法（容器内）：docker exec -i dataflow-backend python - < tools/test_bp_plan_execute.py
"""
import sys

sys.path.insert(0, "/app")

from backend.services import bp_method_tests as BT  # noqa: E402
from backend.services import bp_planner as PL  # noqa: E402
from backend.services import generated_bp as GB  # noqa: E402
from backend.services import llm_client as LC  # noqa: E402

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


PARENT = {"GetMappingFms", "BuildFHIRResource", "TermCoding", "SetPath"}


def plan_ok():
    return {"schema": 1, "class_name": GB.BP_CLASS_NAME, "parent": GB.BP_PARENT_CLASS,
            "summary": "编排：入口 → 逐层派发 → 组装 → 发送",
            "methods": [
                {"name": "OnRequest", "purpose": "入口",
                 "signature": "Method OnRequest(request As %Library.Persistent, "
                              "Output response As %Library.Persistent) As %Status",
                 "calls_parent": ["GetMappingFms"], "calls_self": ["ProcessPatient"],
                 "verification": ["compile"], "unit": None},
                {"name": "ProcessPatient", "purpose": "处理单个患者 Bundle",
                 "signature": "Method ProcessPatient(pId As %String) As %Status",
                 "calls_parent": ["BuildFHIRResource"], "calls_self": [],
                 "verification": ["compile", "runtime"], "unit": None},
            ],
            "verification": [{"step": "compile", "tool": "OBJ.Load", "expect": "ok"}],
            "risks": ["子表无数据"], "fallback": "整类生成（显式标注）"}


print("==== A. 计划 schema 校验 ====")
check(PL.plan_schema_issues(plan_ok(), bp_class=GB.BP_CLASS_NAME, parent_api=PARENT) == [],
      "A1 合规计划 → 无问题")
p = plan_ok(); p["methods"] = []
check(any("methods" in x for x in PL.plan_schema_issues(p, bp_class=GB.BP_CLASS_NAME, parent_api=PARENT)),
      "A2 无方法 → 报错")
p = plan_ok(); p["methods"].append(dict(p["methods"][0]))
check(any("重复" in x for x in PL.plan_schema_issues(p, bp_class=GB.BP_CLASS_NAME, parent_api=PARENT)),
      "A3 方法名重复 → 报错")
p = plan_ok(); p["methods"] = [m for m in p["methods"] if m["name"] != "OnRequest"]
check(any("OnRequest" in x for x in PL.plan_schema_issues(p, bp_class=GB.BP_CLASS_NAME, parent_api=PARENT)),
      "A4 缺 OnRequest → 报错")
p = plan_ok(); p["methods"][0]["calls_parent"] = ["NoSuchApi"]
check(any("父类" in x for x in PL.plan_schema_issues(p, bp_class=GB.BP_CLASS_NAME, parent_api=PARENT)),
      "A5 calls_parent 引用不存在的父类 API → 报错（不许发明）")
p = plan_ok(); p["methods"][0]["calls_self"] = ["Ghost"]
check(any("本类方法" in x for x in PL.plan_schema_issues(p, bp_class=GB.BP_CLASS_NAME, parent_api=PARENT)),
      "A6 calls_self 引用清单外方法 → 报错")
p = plan_ok(); p["class_name"] = "demo.Other"
check(any("class_name" in x for x in PL.plan_schema_issues(p, bp_class=GB.BP_CLASS_NAME, parent_api=PARENT)),
      "A7 class_name 不符 → 报错")
p = plan_ok()
p["methods"] = [{"name": f"M{i}", "purpose": "x", "signature": f"Method M{i}() As %Status"}
                for i in range(PL.PLAN_MAX_METHODS + 1)]
check(any("上限" in x for x in PL.plan_schema_issues(p, bp_class=GB.BP_CLASS_NAME, parent_api=PARENT)),
      "A8 方法数超上限 → 报错（计划不该变成一大坨代码）")

print("\n==== B. 骨架 ====")
skel = GB.build_skeleton(plan_ok(), GB.BP_CLASS_NAME)
check(skel.startswith("Class demo.SqlFhirPatientTxProcess Extends demo.TransformProcess"),
      "B1 类头正确（继承父类）")
check("Method OnRequest(" in skel and "Method ProcessPatient(" in skel, "B2 含全部方法签名")
check(skel.count("Quit $$$OK") >= 2 and skel.rstrip().endswith("}"), "B3 占位实现 + 类闭合")
check("GetMappingFms" in GB.signature_list(plan_ok()) or "ProcessPatient" in GB.signature_list(plan_ok()),
      "B4 签名清单含方法与用途说明")

print("\n==== C. 单方法静态准入 ====")
GOOD = ("Method ProcessPatient(pId As %String) As %Status\n{\n"
        "    Set tSC=..GetMappingFms(pId)\n    Quit tSC\n}\n")
check(GB.method_source_issues("ProcessPatient", GOOD, known_methods={"OnRequest", "ProcessPatient"},
                              parent_api=PARENT) == [], "C1 合法单方法 → 通过")
BAD1 = GOOD + "Method Other() As %Status\n{\n    Quit $$$OK\n}\n"
check(any("只能输出一个方法" in x for x in GB.method_source_issues(
    "ProcessPatient", BAD1, known_methods={"ProcessPatient"}, parent_api=PARENT)),
    "C2 多做方法 → 拒")
check(any("不要输出类头" in x for x in GB.method_source_issues(
    "ProcessPatient", "Class demo.X\n{\n" + GOOD, known_methods={"ProcessPatient"}, parent_api=PARENT)),
    "C3 夹带类头 → 拒")
BAD3 = ("Method ProcessPatient(pId As %String) As %Status\n{\n"
        "    Try {\n        Quit $$$OK\n    } Catch e { Set tSC=$$$OK }\n    Quit tSC\n}\n")
check(any("#1043" in x for x in GB.method_source_issues(
    "ProcessPatient", BAD3, known_methods={"ProcessPatient"}, parent_api=PARENT)),
    "C4 Try 内带参 Quit → 拒")
BAD4 = ("Method ProcessPatient(pId As %String) As %Status\n{\n    Do ..NoSuchHelper(pId)\n    Quit $$$OK\n}\n")
check(any("未定义的助手" in x for x in GB.method_source_issues(
    "ProcessPatient", BAD4, known_methods={"ProcessPatient"}, parent_api=PARENT)),
    "C5 调用未定义助手 → 拒（MPP5376 前置）")
BAD5 = ("Method ProcessPatient(pId As %String, Output response As %Library.Persistent) As %Status\n{\n"
        "    Set response=##class(%DynamicObject).%New()\n    Quit $$$OK\n}\n")
check(any("持久消息类" in x for x in GB.method_source_issues(
    "ProcessPatient", BAD5, known_methods={"ProcessPatient"}, parent_api=PARENT)),
    "C6 response 赋 %DynamicObject → 拒（运行期 ErrBPTerminated 前置）")



print("\n==== D. 编排（LLM/IRIS 全打桩） ====")
_orig = {"write": GB._write_and_compile, "apply": GB.apply_method_updates,
         "src": GB.get_generated_source, "save": GB.save_last_good,
         "load_p": PL.load_method_progress, "save_p": PL.save_method_progress,
         "clear_p": PL.clear_method_progress, "unit": BT.run_unit,
         "gen": LC.generate_bp_method}
_state = {"progress": {}, "calls": []}


def _stub():
    _state["progress"].clear()
    _state["calls"].clear()
    _state.pop("unit_first", None)
    GB._write_and_compile = lambda cls, src: {"ok": True, "message": "stub"}
    GB.apply_method_updates = lambda ups, cls=GB.BP_CLASS_NAME: {
        "ok": True, "applied": [u["name"] for u in ups]}
    GB.get_generated_source = lambda cls=GB.BP_CLASS_NAME: "Class stub {}"
    GB.save_last_good = lambda src: "stub"
    PL.load_method_progress = lambda cls: dict(_state["progress"])
    PL.save_method_progress = lambda cls, m, st, note="": _state["progress"].__setitem__(m, st)
    PL.clear_method_progress = lambda cls: _state["progress"].clear()
    BT.run_unit = lambda cls, m, u: {"ok": True, "skipped": True, "message": "stub"}

    def _gen(*, plan_step, **kw):
        _state["calls"].append(plan_step["name"])
        return {"method": {"name": plan_step["name"],
                           "source": f"Method {plan_step['name']}(pArg As %String) As %Status\n"
                                     "{\n    Quit $$$OK\n}\n"}}

    LC.generate_bp_method = _gen


try:
    _stub()
    r = GB.generate_from_plan(plan=plan_ok(), layout={}, mappings=[], components=[],
                              parent_api=PARENT, bp_class=GB.BP_CLASS_NAME)
    check(r["ok"] and sorted(_state["calls"]) == ["OnRequest", "ProcessPatient"],
          "D1 全部方法成功（调用 %s）" % _state["calls"])
    check(all(v["status"] == "ok" for v in r["methods"].values()), "D2 每方法状态 ok")
    check(_state["progress"] == {"OnRequest": "ok", "ProcessPatient": "ok"}, "D3 进度落库（可续跑）")

    _stub()
    _state["progress"]["OnRequest"] = "ok"        # 模拟上一轮已完成
    r = GB.generate_from_plan(plan=plan_ok(), layout={}, mappings=[], components=[],
                              parent_api=PARENT, bp_class=GB.BP_CLASS_NAME)
    check(r["ok"] and _state["calls"] == ["ProcessPatient"],
          "D4 断点续跑：跳过已 ok 的方法（本轮只跑 %s）" % _state["calls"])
    check(r["methods"]["OnRequest"]["status"] == "resumed", "D5 续跑项显式标 resumed")

    _stub()
    LC.generate_bp_method = lambda *, plan_step, **kw: {
        "method": {"name": plan_step["name"],
                   "source": f"Method {plan_step['name']}() As %Status\n{{\n    Quit $$$OK\n}}\n"
                             "Method Extra() As %Status\n{\n    Quit $$$OK\n}\n"}}
    r = GB.generate_from_plan(plan=plan_ok(), layout={}, mappings=[], components=[],
                              parent_api=PARENT, bp_class=GB.BP_CLASS_NAME)
    check((not r["ok"]) and "静态准入未通过" in r["message"], "D6 静态准入失败 → 显式失败（含原因）")
    check(_state["progress"].get("OnRequest") == "failed", "D7 失败方法进度记 failed（可诊断）")

    _stub()

    def _unit(cls, m, u):
        if not u:
            return {"ok": True, "skipped": True, "message": "无样例"}
        if not _state.get("unit_first"):
            _state["unit_first"] = True
            return {"ok": False, "message": "断言失败：期望包含 X"}
        return {"ok": True, "message": "单测通过"}

    BT.run_unit = _unit
    plan_u = plan_ok()
    plan_u["methods"][0]["unit"] = {"args": ["x"], "expect_contains": "X"}
    r = GB.generate_from_plan(plan=plan_u, layout={}, mappings=[], components=[],
                              parent_api=PARENT, bp_class=GB.BP_CLASS_NAME)
    check(r["ok"] and _state["calls"].count("OnRequest") == 2,
          "D8 单测失败 → 带断言回喂重试一次后成功（OnRequest 调用 %d 次）"
          % _state["calls"].count("OnRequest"))
finally:
    GB._write_and_compile = _orig["write"]
    GB.apply_method_updates = _orig["apply"]
    GB.get_generated_source = _orig["src"]
    GB.save_last_good = _orig["save"]
    PL.load_method_progress = _orig["load_p"]
    PL.save_method_progress = _orig["save_p"]
    PL.clear_method_progress = _orig["clear_p"]
    BT.run_unit = _orig["unit"]
    LC.generate_bp_method = _orig["gen"]

print("\n==== E. 单测汇总与 skipped 语义 ====")
check(BT.run_unit("demo.X", "M", None).get("skipped") is True, "E1 无 unit 样例 → 显式 skipped")
s = BT.units_summary({"A": {"unit": {"ok": True, "skipped": True}},
                      "B": {"unit": {"ok": True}}, "C": {"unit": {"ok": False}}})
check(s["unit_skipped"] == ["A"] and s["unit_passed"] == ["B"] and s["unit_failed"] == ["C"],
      "E2 汇总三态正确（%s）" % s)

print("\n==== 结果: %d PASS / %d FAIL ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
