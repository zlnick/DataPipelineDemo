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
assert TD.value_issues({"source": "A", "target": "x", "transform": "concat(A,' ',B)"}, cols) == []
print("OK 值级检查（源列存在/不存在、常量、concat）")

src_idx = {"DS1_T1": {"id", "mrn"}, "Patient": {"id", "mrn"}}
assert TD.source_columns_of("DS1_T1", src_idx) == {"id", "mrn"}
assert TD.source_columns_of("SQLUser.Patient", src_idx) == {"id", "mrn"}
assert TD.source_columns_of("Unknown", src_idx) is None
print("OK 源表列索引（资产 ID / 表名 / 未知）")
print("DIRECTIVES:", [d["id"] for d in TD.list_directives()])
print("DIRECTIVES-TEST-PASSED")
