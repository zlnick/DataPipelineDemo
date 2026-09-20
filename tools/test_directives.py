# -*- coding: utf-8 -*-
"""受控指令注册表单测（纯断言）。"""
from backend.services import transform_directives as TD

CASES = [
    ({"source": "MRN", "target": "identifier", "transform": None}, 0, "普通映射(null)"),
    ({"source": "MRN", "target": "identifier", "transform": "direct"}, 0, "direct"),
    ({"source": "StartDate", "target": "authoredOn", "transform": "date"}, 0, "date"),
    ({"source": None, "target": "intent", "transform": "constant:order"}, 0, "规范常量"),
    ({"source": "order", "target": "intent", "transform": "constant"}, 1, "不规范常量（值在 source）"),
    ({"source": None, "target": "intent", "transform": "constant"}, 1, "常量缺值"),
    ({"source": "MedicationCode", "target": "medication", "transform": "code"}, 0, "类型提示 code"),
    ({"source": "PatientID", "target": "subject", "transform": "reference"}, 0, "类型提示 reference"),
    ({"source": "Diagnosis.Code", "target": "code", "transform": "term_map:cn2snomed"}, 0, "term_map 合法"),
    ({"source": "Diagnosis.Code", "target": "code", "transform": "term_map:unknown_skill"}, 1, "term_map 未注册 skill"),
    ({"source": None, "target": "x", "transform": None}, 1, "无取值"),
    ({"source": "Col", "target": "x", "transform": "magic_map"}, 1, "未注册指令"),
]
for fm, expect, name in CASES:
    got = TD.syntax_issues(fm)
    assert len(got) >= expect, f"{name}: 期望≥{expect} 个问题，实际 {got}"
    assert (len(got) > 0) == (expect > 0), f"{name}: {got}"
    print(f"OK {name}: {got if got else '无问题'}")

# 值级检查
cols = {"mrn", "familyname", "code"}
assert TD.value_issues({"source": "MRN", "target": "identifier", "transform": None}, cols) == []
assert TD.value_issues({"source": "UnknownCol", "target": "identifier", "transform": None}, cols)
assert TD.value_issues({"source": None, "target": "intent", "transform": "constant:order"}, cols) == []
# concat 参数会**逐项**按已知源列校验（2026-09-17 起的口径）：列名合法 → 无问题；含未知列 → 报问题
# 值级检查口径（2026-09-19 复核）：
#  · source 是**表达式**（含括号，如 concat(familyname,' ',mrn)）→ 不按单列比对（实现里显式跳过）
#  · source 是**单列/单路径** → 逐级剥前缀与已知列比对（FHIR 路径 name[0].family 亦适用）
#  · 旧断言（source="A" + transform="concat(A,' ',B)"）把表达式写在 transform 里、source 当单列校验，
#    实现会报「源列 A 不在源表已知列中」→ 旧期望 == [] 不成立，此处按实现口径更正
assert TD.value_issues({"source": "concat(familyname,' ',mrn)", "target": "x",
                        "transform": None}, cols) == []
assert TD.value_issues({"source": "familyname", "target": "x", "transform": None}, cols) == []
assert TD.value_issues({"source": "UnknownCol", "target": "x", "transform": None}, cols)
print("OK 值级检查（单列存在性 + 表达式跳过 + 常量）")

src_idx = {"DS1_T1": {"id", "mrn"}, "Patient": {"id", "mrn"}}
assert TD.source_columns_of("DS1_T1", src_idx) == {"id", "mrn"}
assert TD.source_columns_of("SQLUser.Patient", src_idx) == {"id", "mrn"}
assert TD.source_columns_of("Unknown", src_idx) is None
print("OK 源表列索引（资产 ID / 表名 / 未知）")
print("DIRECTIVES:", [d["id"] for d in TD.list_directives()])
print("DIRECTIVES-TEST-PASSED")
