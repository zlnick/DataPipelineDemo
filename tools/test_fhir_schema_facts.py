# -*- coding: utf-8 -*-
"""离线回归（2026-09-19）：FHIR 组装 schema 的**事实注入** + 「明文落 coding」检查。

实测缺陷（用户 SQL→FHIR 报文的实际观测）：
  · `Encounter.reasonCode`（国标 ICD-10 码 E11.900）**缺 system**，而同一国标码在 `Condition.code`
    上带 `urn:cn-nhsa:icd10-gbt2016` → 口径不一致；
  · `dosageInstruction.route` = `{system: urn:cn-nhsa:drug-nrdl, code: "口服"}` —— 「口服」是中文文本
    却被挂上**药品目录**体系；`Encounter.type` = `{code:"门诊"}`（明文塞进 coding，无体系）。
根因：引擎 `LeafValue` 对**任何** CodeableConcept 列都硬编码取源行 `CodeSystem` 列 ——
      而该列是**整行共享**的（MedicationOrder 行 = 药品体系）；Encounter 表又没有该列。

修复：平台按事实（映射源列 + 资产 field_terms + 术语 Skill 的源体系 + 模型静态体系）计算
`system` / `coded` / `system_from_row` 注入组装 schema；引擎只读 schema；另给模型补 `*_text`
落点并新增 C1 事实检查（明文落 CodeableConcept → 用 `update_mapping` 改成文本列）。

本测试纯离线（不连 IRIS、不调 LLM）：
  A 事实计算（column_facts）        B 注入（apply_facts，含"不覆盖模型静态体系"）
  C C1「明文落 coding」检查           D 接线与代码事实静态断言
用法（容器内）：docker exec -i dataflow-backend python - < tools/test_fhir_schema_facts.py
"""
import os
import sys

PASS = 0
FAIL = 0

ROOT = os.environ.get("REPO_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
for _p in (ROOT, "/app"):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % msg)
    else:
        FAIL += 1
        print("  ✗ %s" % msg)


from backend.services import fhir_schema_facts as FSF           # noqa: E402
from backend.services import fhir_target_model as FTM           # noqa: E402
from backend.services import transformation_validator as TV     # noqa: E402

# ---- 事实夹具：与 CLINIC「模拟HIS」源的真实 field_terms 同形 ----
TERMS = {
    "diagnosis": {"terms": {"Code": {"kind": "coded_value"}, "Name": {"kind": "display_name"},
                            "CodeSystem": {"kind": "term_uri"}},
                  "has_codesystem": True},
    "encounter": {"terms": {"ClassCode": {"kind": "coded_value"},
                            "ClassDisplay": {"kind": "display_name"},
                            "ReasonCode": {"kind": "coded_value"}},
                  "has_codesystem": False},
    "medicationorder": {"terms": {"MedicationCode": {"kind": "coded_value"},
                                  "CodeSystem": {"kind": "term_uri"}},
                        "has_codesystem": True},
}
M_FHIR = [
    {"id": "R1", "source": "Diagnosis", "target_table": "Condition", "target_type": "FHIR",
     "field_mappings": [{"source": "Diagnosis.Code", "target": "code",
                         "transform": "term_map:cn2snomed"}]},
    {"id": "R2", "source": "Encounter", "target_table": "Encounter", "target_type": "FHIR",
     "field_mappings": [{"source": "Encounter.ReasonCode", "target": "reason_code",
                         "transform": "term_map:cn2snomed"},
                        {"source": "Encounter.ClassDisplay", "target": "type", "transform": "code"},
                        {"source": "Encounter.ClassCode", "target": "class_code", "transform": "code"},
                        {"source": None, "target": "verificationStatus",
                         "transform": "constant:confirmed"}]},
    {"id": "R3", "source": "MedicationOrder", "target_table": "MedicationRequest",
     "target_type": "FHIR",
     "field_mappings": [{"source": "MedicationOrder.MedicationCode", "target": "medication",
                         "transform": "term_map:cn2rx"},
                        {"source": "MedicationOrder.Route", "target": "route", "transform": "code"}]},
]

print("==== A. 事实计算（column_facts）====")
_TS = {"DS15819": TERMS}                     # 单源：terms_by_source
fc = FSF.column_facts("Condition", M_FHIR, _TS, default_source_id="DS15819")
check(fc.get("code") == {"coded": True, "system": None, "system_from_row": "CodeSystem"},
      "A1 Condition.code（源列 coded_value + 源表有 CodeSystem 列）→ 声明取源行 CodeSystem（%s）"
      % fc.get("code"))
fe = FSF.column_facts("Encounter", M_FHIR, _TS, default_source_id="DS15819")
check(fe.get("reason_code", {}).get("system") == "urn:cn-nhsa:icd10-gbt2016",
      "A2 Encounter.reason_code 补上国标体系（原先无 system）→ %s" % fe.get("reason_code"))
check(fe.get("reason_code", {}).get("system_from_row") is None,
      "A3 reason_code 不取源行（Encounter 无 CodeSystem 列）→ system 来自术语 Skill 的 source_system")
check(fe.get("type", {}).get("coded") is False and not fe.get("type", {}).get("system"),
      "A4 Encounter.type ← ClassDisplay(display_name) → 非编码列、不挂体系（%s）" % fe.get("type"))
check(fe.get("class_code", {}).get("coded") is True and fe.get("class_code", {}).get("system") is None,
      "A5 class_code ← ClassCode(coded_value)、无 Skill → coded=True 且不指定体系（保留模型静态 ActCode）")
check("verificationStatus" not in fe,
      "A6 常量指令（source 为空）→ 不产生事实（保留模型静态体系）")
fm = FSF.column_facts("MedicationRequest", M_FHIR, _TS, default_source_id="DS15819")
check(fm.get("medication", {}).get("system_from_row") == "CodeSystem",
      "A7 MedicationRequest.medication → 取源行 CodeSystem（药品体系，正确）")
check(fm.get("route", {}).get("coded") is False and not fm.get("route", {}).get("system"),
      "A8 MedicationRequest.route ← Route（无 field_terms）→ 非编码列、**不再误挂药品体系**（%s）"
      % fm.get("route"))
check(FSF.column_facts("Patient", M_FHIR, _TS, default_source_id="DS15819") == {},
      "A9 无该资源映射 → 无事实（保留模型原样）")

# A10-A12：**多源共存回归**（2026-09-19 实测缺陷：只用一个 source_id 会让别组映射拿错源的事实）
_TS2 = {"DS_S1": {"patient": {"terms": {"Gender": {"kind": "coded_value"}},
                              "has_codesystem": False}},
        "DS_S2": {"patient": {"terms": {}, "has_codesystem": True}}}
_MM = [{"id": "R1", "source": "Patient", "source_id": "DS_S1", "target_table": "Patient",
        "target_type": "FHIR", "field_mappings": [{"source": "Patient.Gender", "target": "gender"}]},
       {"id": "R2", "source": "Patient", "source_id": "DS_S2", "target_table": "Patient",
        "target_type": "FHIR", "field_mappings": [{"source": "Patient.ID", "target": "identifier"}]}]
_f2 = FSF.column_facts("Patient", _MM, _TS2)
check(_f2.get("gender", {}).get("coded") is True,
      "A10 每条映射按**自己的 source_id** 取事实（DS_S1 的 Gender 判为编码列 → coded=True）")
check(_f2.get("identifier", {}).get("coded") is False,
      "A11 另一源（DS_S2 无 field_terms）判为非编码列 → coded=False（两源事实互不串用）")
_f3 = FSF.column_facts("Patient", _MM, _TS2, default_source_id="DS_S1")
check(_f3.get("gender", {}).get("coded") is True and _f3.get("identifier", {}).get("coded") is False,
      "A12 映射自带 source_id 时优先于 default_source_id（多组一次提交不再拿错源）")


print("\n==== B. 注入（apply_facts）====")
sch = FTM.model_schema("Encounter") or {}
orig = [dict(c) for c in sch.get("columns") or []]
out = FSF.apply_facts(sch, fe)
by = {c["name"]: c for c in out.get("columns") or []}
check(by["reason_code"].get("system") == "urn:cn-nhsa:icd10-gbt2016"
      and by["reason_code"].get("coded") is True, "B1 reason_code 注入了体系与 coded")
check(by["class_code"].get("system") == "http://terminology.hl7.org/CodeSystem/v3-ActCode",
      "B2 模型静态体系（class_code=ActCode）**不被覆盖**（%s）" % by["class_code"].get("system"))
check(by["type"].get("coded") is False and not by["type"].get("system"),
      "B3 type 标记为非编码列且不挂体系")
check([dict(c) for c in sch.get("columns") or []] == orig, "B4 不改动原 schema（深拷贝）")
check(FSF.apply_facts(sch, {}) is sch, "B5 无事实 → 原样返回（不做无谓复制/改动）")
check(all("coded" in c for c in by.values() if c["name"] in ("reason_code", "type", "class_code")),
      "B6 列级 coded 标记写入（引擎据此决定是否可用行内体系）")

print("\n==== C. C1「明文落 coding」检查（coded_text_issues）====")
assets = [{"name": "Encounter", "fields": ["ID", "ClassCode", "ClassDisplay", "ReasonCode"],
           "field_terms": TERMS["encounter"]["terms"]},
          {"name": "MedicationOrder", "fields": ["ID", "MedicationCode", "Route"],
           "field_terms": TERMS["medicationorder"]["terms"]},
          {"name": "Diagnosis", "fields": ["ID", "Code", "CodeSystem"],
           "field_terms": TERMS["diagnosis"]["terms"]}]
issues = TV._coded_text_issues(M_FHIR, assets)
got = {(i["item"].split(".")[-1]): i for i in issues}
check("type" in got and "route" in got, "C1 报出 Encounter.type 与 MedicationRequest.route（%s）"
      % sorted(got))
check(got.get("route", {}).get("check") == "coded_text_gap"
      and "route_text" in got.get("route", {}).get("message", ""),
      "C2 route 的修法指向文本列 route_text（有落点，可被 LLM 修复）")
check("type_text" in got.get("type", {}).get("message", ""), "C3 type 的修法指向 type_text")
check(not ({"code", "reason_code", "medication"} & set(got)),
      "C4 真编码列（coded_value + term_map）不报（无误报）")
check(not ({"clinicalStatus", "class_code", "subject"} & set(got)),
      "C5 模型静态体系列 / 引用列不报")
no_facts = TV._coded_text_issues(M_FHIR, [{"name": "Encounter", "fields": ["ClassDisplay"]}])
check(no_facts == [], "C6 无 field_terms 事实 → fail-open（不误报）")
check(TV._coded_text_issues([], assets) == [], "C7 空映射 → 无检查项")
check(TV._text_sibling({"type": {}, "type_text": {}}, "type") == "type_text"
      and TV._text_sibling({"type": {}}, "type") == "", "C8 文本列兄弟探测正确")
check(all(i.get("severity") == "error" for i in issues),
      "C9 级别为 error（C1 才会调用 LLM 做增量修复）")

print("\n==== D. 接线与代码事实（静态断言）====")


def _src(rel):
    """读仓库文件（容器只挂载 backend/ → 未挂载的文件返回 None，由 skip 处理）。"""
    for base in (os.environ.get("REPO_ROOT"), ROOT, os.getcwd(), "/app", "/"):
        if not base:
            continue
        p = os.path.join(base, rel)
        if os.path.isfile(p):
            return open(p, encoding="utf-8").read()
    return None


def check_file(rel, cond, msg):
    """跨文件静态断言：文件未挂载到容器时**显式跳过**（宿主侧由 check_engine_source.py 覆盖）。"""
    text = _src(rel)
    if text is None:
        print("  – 跳过 %s（%s 未挂载到容器；宿主侧由 tools/check_engine_source.py 覆盖）" % (msg, rel))
        return
    check(cond(text), msg)


cls = _src("iris/src/demo/TransformProcess.cls") or ""
pipe = _src("backend/routes/pipelines.py") or ""
tv = _src("backend/services/transformation_validator.py") or ""

check_file("iris/src/demo/TransformProcess.cls",
           lambda t: "system_from_row" in t and 'pSrc.%Get("CodeSystem")' not in t,
           "D1 引擎用 schema 声明的 system_from_row，且**不再**硬编码取源行 CodeSystem")
check_file("iris/src/demo/TransformProcess.cls", lambda t: "tFromRow" in t,
           "D2 引擎实现里 tFromRow 变量在位")
check("mappings=mappings_effective, source_id=source_id" in pipe
      and pipe.count("fhir_schema_facts") >= 2, "D3 单/多管道生成都传 mappings+source_id")
check("field_terms" in pipe and "_c1_assets" in pipe, "D4 C1 资产输入带 field_terms 事实")
check("coded_text_gap" in tv and "_text" in tv, "D5 C1 提示词含 coded_text_gap 修法（文本列）")
for col in ("type_text", "reason_text", "route_text"):
    check(col in _src("backend/services/fhir_target_model.py"),
          "D6 目标模型提供文本列 %s" % col)
check_file("tools/reset_ui_env.py", lambda t: '"^demo.Trace"' in t,
           "D7 重置白名单已含 ^demo.Trace（跨轮残留不再误导）")
check_file("backend/services/repository.py", lambda t: "if raw is not None" not in t,
           "D8 list_keys 不再按值过滤（下标节点也算 key）")

print("\n==== 结果: %d PASS / %d FAIL ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
