# -*- coding: utf-8 -*-
"""离线回归：**FHIR 源字段发现**的规范层（无数据也能知道字段）。

覆盖（2026-09-18 新增，配合「FHIR 是标准」的字段发现改造）：
  A. 平台规范快照字段路径（fhir_target_model.source_field_paths）：已建模有路径、未建模为空
  B. 采样计划 `_fhir_sample_order`：规范/AI 暂定字段会被**重新采样**（数据优先长期成立）
  C. AI 规范字段的清洗 `_clean_field_paths`：合法/非法/去重/限量/结构元素过滤
  D. AI 规范字段补全 `complete_fhir_resource_fields`：正常 / 限量 / 空输出 → AgentError
  E. 路由接线（静态断言）：analyze 调用补全 + 回写 + provenance 审计 + 告警

用法（容器内）：docker exec -i dataflow-backend python - < tools/test_fhir_spec_fields.py
"""
import json
import os
import sys

sys.path.insert(0, "/app")

from backend.services import connection_profiler as CP  # noqa: E402
from backend.services import fhir_target_model as FTM  # noqa: E402
from backend.services import interface_analyzer as IA  # noqa: E402

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


print("==== A. 平台规范快照（无数据也能知道字段） ====")
p = FTM.source_field_paths("Patient")
print("     Patient 规范字段: %s" % p)
check(len(p) >= 6, "A1 Patient 有 ≥6 个规范元素路径（%d）" % len(p))
check("name[0].family" in p and "identifier[0]" in p, "A2 含 name[0].family / identifier[0]（数组以 [0] 表示）")
check(not any(str(x).startswith("meta") for x in p), "A3 不含纯结构元素（meta.*）")
check(FTM.source_field_paths("Account") == [], "A4 未建模类型（Account）→ 空（转 AI 规范推断）")
allp = FTM.source_field_paths_all()
check(len(allp) == len(FTM.DEFAULT_RESOURCE_TYPES) and "Encounter" in allp,
      "A5 source_field_paths_all 覆盖全部 %d 个已建模类型" % len(FTM.DEFAULT_RESOURCE_TYPES))
check(CP._fhir_spec_model_fields("Condition") == FTM.source_field_paths("Condition"),
      "A6 connection_profiler 规范兜底与模型口径一致")
check(CP._fhir_spec_model_fields("NotAResource") == [], "A7 未知类型不报错、返回空")

print("\n==== B. 采样顺序：暂定字段会被重新采样（数据优先） ====")
assets = [{"name": "Patient", "fields": ["name[0].family"]},   # 规范/AI 暂定
          {"name": "Encounter", "fields": ["status"]},          # 采样得来（真实）
          {"name": "Foo", "fields": []}]                        # 无字段
plan, skipped = CP._fhir_sample_order(assets, 10)
check([a["name"] for a in plan] == ["Foo"], "B1 默认（无 re_sample）只采样字段为空的类型（%s）"
      % [a["name"] for a in plan])
plan2, _ = CP._fhir_sample_order(assets, 10, {"Patient"})
check(sorted(a["name"] for a in plan2) == ["Foo", "Patient"],
      "B2 provenance=spec_model/ai_spec 的类型被重新采样（%s）" % sorted(a["name"] for a in plan2))
check("Encounter" not in [a["name"] for a in plan2], "B3 真实采样过的类型不重复采样（省时）")
plan3, sk3 = CP._fhir_sample_order(assets, 1, {"Patient"})
check(len(plan3) == 1 and sk3 >= 1, "B4 类型上限生效并回报 skipped（%d/%d）" % (len(plan3), sk3))

print("\n==== C. AI 规范字段清洗 ====")
clean = IA._clean_field_paths(["identifier[0]", " name[0].family ", "identifier[0]",
                               "code.coding[0].code", "meta.profile", "text", "", "--x",
                               "subject", "bad path with space"])
print("     清洗后: %s" % clean)
check(clean == ["identifier[0]", "name[0].family", "code.coding[0].code", "subject"],
      "C1 保序去重、去结构元素、剔非法形态（%s）" % clean)
big = IA._clean_field_paths(["f%d" % i for i in range(100)])
check(len(big) == IA.FHIR_AI_FIELDS_MAX_PER_TYPE, "C2 单类型限量 %d 生效" % IA.FHIR_AI_FIELDS_MAX_PER_TYPE)

print("\n==== D. AI 规范字段补全（LLM 打桩） ====")
_orig = IA._call_llm


def _stub(payload):
    IA._call_llm = lambda *a, **k: payload


try:
    _stub({"items": [{"name": "Account", "fields": ["identifier[0]", "status", "subject"],
                      "note": "账号"}]})
    got = IA.complete_fhir_resource_fields(["Account"], {"fhir_version": "4.0.1"})
    check(got.get("Account", {}).get("fields") == ["identifier[0]", "status", "subject"],
          "D1 正常输出 → {类型: fields}（%s）" % json.dumps(got, ensure_ascii=False)[:80])

    _stub({"items": [{"name": "Account", "fields": ["identifier[0]"]},
                     {"name": "B", "fields": []}]})
    got2 = IA.complete_fhir_resource_fields(["Account", "B"])
    check(list(got2) == ["Account"], "D2 无有效字段的类型被跳过（%s）" % list(got2))

    _stub({"items": [{"name": "T%d" % i, "fields": ["status"]} for i in range(50)]})
    got3 = IA.complete_fhir_resource_fields(["T%d" % i for i in range(50)], max_types=3)
    check(len(got3) == 3, "D3 类型上限生效（max_types=3 → %d）" % len(got3))

    for payload, label in (({"foo": 1}, "缺 items"), ({"items": []}, "空 items"),
                           ({"items": [{"name": "X", "fields": []}]}, "全无有效字段")):
        _stub(payload)
        try:
            IA.complete_fhir_resource_fields(["X"])
            check(False, "D4 %s 应抛 AgentError" % label)
        except IA.AgentError as exc:
            check(True, "D4 %s → AgentError（%s）" % (label, str(exc)[:30]))
    check(IA.complete_fhir_resource_fields([]) == {}, "D5 空请求 → 空结果、不调 LLM")
finally:
    IA._call_llm = _orig

print("\n==== E. 路由接线（静态断言） ====")
_route = open("/app/backend/routes/datasources.py", encoding="utf-8").read()
check("complete_fhir_resource_fields" in _route and "backfill_fhir_fields" in _route,
      "E1 analyze 调用 AI 规范补全并回写资产")
check('"provenance"' in _route or "provenance" in _route, "E2 字段来源写入 provenance（可审计）")
check('"ai_spec_missing"' in _route and '_warnings.append' in _route,
      "E3 未补到的类型**显式告警**（不静默）")
check("field_sources" in _route, "E4 响应带 field_sources（前端可展示来源）")
_cp = open("/app/backend/services/connection_profiler.py", encoding="utf-8").read()
check("spec_model" in _cp and "pending_ai_types" in _cp and "_re_sample" in _cp,
      "E5 事实层三层兜底 + 待 AI 清单 + 暂定字段复采")

print("\n==== 结果: %d PASS / %d FAIL ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
