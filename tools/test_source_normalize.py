# -*- coding: utf-8 -*-
"""单元验证（2026-09-17 Round 2 缺陷 P 的**上游收口**）：映射 source 口径归一。

规范形态：SQL 源 `Patient.FamilyName`（不带 schema 前缀）/ FHIR 路径 `name[0].family` /
concat 表达式（参数逐个归一）；`constant:<值>`（source=null）不动。

本测试锁死：**只改写法、不改字段语义**（剥 schema 前缀、递归 concat、幂等、不误剥资产名/路径）。
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


from backend.services import mapping_source_norm as N  # noqa: E402

ASSETS = [{"name": "Patient", "structure": {"schema": "SQLUser", "table": "Patient"}},
          {"name": "Encounter", "structure": {"schema": "SQLUser"}}]
P = N.schema_prefixes(ASSETS)

print("==== A. 单表达式归一 ====")
check(N.normalize_expr("SQLUser.Patient.ID", P) == "Patient.ID",
      "A1 三段 schema.表.列 → 表.列（原样输出给运行期的元凶）")
check(N.normalize_expr("CLINIC.Patient.ID", P) == "Patient.ID", "A2 目标库 schema 前缀同样剥掉")
check(N.normalize_expr("USER.SQLUser.Patient.ID", P) == "Patient.ID", "A3 多层前缀逐级剥")
check(N.normalize_expr("Patient.FamilyName", P) == "Patient.FamilyName",
      "A4 规范形态保持不变（未误剥资产名）")
check(N.normalize_expr("name[0].family", P) == "name[0].family", "A5 FHIR 路径保持不变")
check(N.normalize_expr("Patient.name[0].family", P) == "Patient.name[0].family",
      "A6 FHIR 路径带资产名前缀保持不变")
check(N.normalize_expr("' '", P) == "' '", "A7 字面量保持")
check(N.normalize_expr("", P) == "", "A8 空串保持")

print("\n==== B. concat 表达式（参数递归归一） ====")
check(N.normalize_expr("concat(SQLUser.Patient.FamilyName, ' ', SQLUser.Patient.GivenName)", P)
      == "concat(Patient.FamilyName, ' ', Patient.GivenName)",
      "B1 concat 每个参数都剥掉 schema 前缀，引号内空格不受影响")
check(N.normalize_expr("concat(Patient.FamilyName, '-', Patient.GivenName)", P)
      == "concat(Patient.FamilyName, '-', Patient.GivenName)", "B2 已规范的 concat 不变")

print("\n==== C. mappings 级（计数 + 语义不变 + 幂等） ====")
maps = [{"id": "R1", "source": "Patient", "target_table": "Patient", "target_type": "DB",
         "field_mappings": [
             {"source": "SQLUser.Patient.ID", "target": "MRN"},
             {"source": "concat(SQLUser.Patient.FamilyName, ' ', SQLUser.Patient.GivenName)",
              "target": "FullName"},
             {"source": None, "target": "ID", "transform": "constant:SRC-1"},
             {"source": "SQLUser.Patient.ID", "target": "ID"}]}]
before_targets = [fm["target"] for fm in maps[0]["field_mappings"]]
n = N.normalize_mappings(maps, ASSETS)
fms = maps[0]["field_mappings"]
check(n == 3, "C1 改动计数 = 3（两条路径 + 一条 concat）")
check(fms[0]["source"] == "Patient.ID" and fms[3]["source"] == "Patient.ID", "C2 路径已归一")
check(fms[1]["source"] == "concat(Patient.FamilyName, ' ', Patient.GivenName)", "C3 concat 已归一")
check(fms[2]["source"] is None and fms[2]["transform"] == "constant:SRC-1",
      "C4 constant 指令（source=null）不动")
check([fm["target"] for fm in fms] == before_targets,
      "C5 **目标字段与顺序完全不变**（只改写法，不改语义：%s）" % (before_targets,))
check(N.normalize_mappings(maps, ASSETS) == 0, "C6 幂等：再跑一次 0 改动")

print("\n==== D. 前缀集合来源（资产登记 structure.schema 参与） ====")
check("sqluser" in N.schema_prefixes(ASSETS), "D1 资产登记的 schema（SQLUser）被纳入前缀集合")
check(N.normalize_expr("CLINIC.Encounter.Dept", N.schema_prefixes([])) == "Encounter.Dept",
      "D2 默认表覆盖常见 schema（CLINIC）")

print("\n==== 结果: %d PASS / %d FAIL ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
