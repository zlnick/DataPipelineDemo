# -*- coding: utf-8 -*-
"""单元验证（2026-09-17 Round 2 缺陷回归）：**映射 source 限定名解析**（多段前缀 / 末段兜底）。

背景（实测 P0，SQL 源 → DB 目标）：
Agent A 对 SQL 源给出的映射 source 是**三段全限定名** `SQLUser.Patient.ID`（schema.表.列），
而运行期源数据是 SQL 轮询行的**扁平列名**（`ID` / `FamilyName` …）。
`iris/python/transform_handler.py::_get_field` 旧实现只剥**一层**前缀
（`split(".", 1)[1]` → `Patient.ID`）→ 仍取不到 → **整行值全为 null** →
DB 目标 SQLOp 的 `INSERT … VALUES(*ID,…)` 报
`[SQLCODE: <-108>: <Field 'SQLUser.Patient.ID' is required>]`（消息 Status=8、目标 0 落地），
而生成接口此前一路 `code:0`（校验只看列名/指令，不看"运行期能否取到值"的三段限定名）。

同类第二处：`backend/services/term_precheck.py::_jobs_from_mappings` 取首段当表名 → 三段限定名得到
`schema`（`SQLUser`）→ 不在编码列白名单 → term_map 判定任务被**静默丢弃**（双 coding 不生效）。

本测试锁死修复后的不变量（纯函数，不连 IRIS / 不调 LLM / 不写数据；宿主可直接跑）：
A. `_get_field` 三段 / 两段 / 裸列名 / FHIR 路径 / 数组 / dict 归一 / 末段大小写兜底 全部可解析，不存在时仍返回 None；
B. `_eval_expr` 的 concat 参数同样支持限定名；
C. `transform_resource_json` 端到端：三段限定名映射在扁平 SQL 行上产出**非空且含主键**的行 JSON；
D. `term_precheck._jobs_from_mappings` 的三段限定名能正确定位源表（不再静默丢任务）。
"""
import json
import os
import sys

PASS = 0
FAIL = 0

# 仓库根定位：优先环境变量（容器内无 __file__ 时用），否则按脚本位置上溯
ROOT = os.environ.get("REPO_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
for _p in (os.path.join(ROOT, "iris", "python"), os.path.join(ROOT),
           "/shared/python",            # IRIS 容器：Embedded Python 挂载点
           "/app/iris/python", "/app"):  # backend 容器：仓库挂载点
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

TH = None
try:
    import transform_handler as TH  # noqa: E402
except Exception as _exc:  # noqa: BLE001 - IRIS 容器外/未挂载该模块 → 跳过 A/B/C 段
    print("! 无法导入 transform_handler（跳过 A/B/C 段）: %s" % str(_exc)[:140])


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % msg)
    else:
        FAIL += 1
        print("  ✗ %s" % msg)


SQL_ROW = {"ID": "MRN-1001", "FamilyName": "张", "GivenName": "伟芳", "Gender": "M",
           "BirthDate": "1980-01-01", "Phone": "13800000000",
           "Address": "某某路 1 号", "City": "上海"}
FHIR_PATIENT = {"resourceType": "Patient", "id": "p1",
                "name": [{"family": "张", "given": ["伟芳", "娜"]}],
                "gender": "male", "birthDate": "1980-01-01"}

print("==== A. _get_field 限定名解析 ====")
if TH is None:
    print("  ! A/B/C 段跳过（transform_handler 不可导入：请在 IRIS 容器跑，模块在 /shared/python）")
else:
    check(TH._get_field(SQL_ROW, "SQLUser.Patient.ID") == "MRN-1001",
          "A1 三段限定名 schema.表.列 可解析（原实现只剥一层 → None）")
    check(TH._get_field(SQL_ROW, "Patient.FamilyName") == "张", "A2 两段 表.列 可解析（回归保持）")
    check(TH._get_field(SQL_ROW, "City") == "上海", "A3 裸列名可解析（回归保持）")
    check(TH._get_field(SQL_ROW, "Patient.familyname") == "张",
          "A4 末段大小写不敏感兜底（Patient.familyname → FamilyName）")
    check(TH._get_field(FHIR_PATIENT, "Patient.name[0].family") == "张",
          "A5 FHIR 两段限定名 + 路径索引可解析")
    check(TH._get_field(FHIR_PATIENT, "name[0].given[0]") == "伟芳", "A6 裸 FHIR 路径数组索引可解析")
    check(TH._get_field(FHIR_PATIENT, "FHIRObj.name[0].given") == "伟芳",
          "A7 三段 FHIR 限定名（前缀任意）可解析")
    check(TH._get_field(SQL_ROW, "SQLUser.Patient.NotAColumn") is None,
          "A8 不存在的字段仍返回 None（不误取）")
    check(TH._get_field({"c": {"value": "v1"}}, "Patient.c") == "v1", "A9 dict 值归一（取 value 字段）")
    check(TH._get_field({"c": ["a", "b"]}, "Patient.c") == "a", "A10 list 值取首元素（回归保持）")

    print("\n==== B. _eval_expr（concat / 字面量） ====")
    check(TH._eval_expr(SQL_ROW, "concat(SQLUser.Patient.FamilyName, ' ', SQLUser.Patient.GivenName)")
          == "张 伟芳", "B1 concat 参数支持三段限定名")
    check(TH._eval_expr(SQL_ROW, "'X'") == "X", "B2 引号字面量保持")

    print("\n==== C. transform_resource_json 端到端（扁平 SQL 行 + 三段限定名映射） ====")
    FMS = [{"source": "SQLUser.Patient.ID", "target": "MRN", "transform": None},
           {"source": "SQLUser.Patient.FamilyName", "target": "FamilyName", "transform": None},
           {"source": "SQLUser.Patient.BirthDate", "target": "BirthDate", "transform": "date"},
           {"source": "concat(SQLUser.Patient.FamilyName, ' ', SQLUser.Patient.GivenName)",
            "target": "FullName", "transform": None},
           {"source": "SQLUser.Patient.ID", "target": "ID", "transform": "direct"}]
    row = json.loads(TH.transform_resource_json(json.dumps(SQL_ROW, ensure_ascii=False),
                                               json.dumps(FMS, ensure_ascii=False)))
    check(row.get("ID") == "MRN-1001", "C1 目标主键 ID 产出值（DB INSERT 不再报 is required）")
    check(row.get("MRN") == "MRN-1001" and row.get("FamilyName") == "张",
          "C2 业务列均有值（修复前全为 null）")
    check(row.get("BirthDate") == "1980-01-01", "C3 date 指令仍生效")
    check(row.get("FullName") == "张 伟芳", "C4 concat 表达式在运行期产出中文拼接")
    check(all(v is not None for v in row.values()), "C5 整行无 null（数值级事实）")

print("\n==== D. term_precheck._jobs_from_mappings 源表定位（三段限定名） ====")
try:
    from backend.services import term_precheck as TC  # noqa: E402
    maps = [{"field_mappings": [{"source": "SQLUser.Diagnosis.Code", "target": "code",
                                 "transform": "term_map:cn2snomed"}]},
            {"field_mappings": [{"source": "MedicationOrder.MedicationCode", "target": "code",
                                 "transform": "term_map:cn2rx"}]},
            {"field_mappings": [{"source": "SQLUser.Patient.FamilyName", "target": "family",
                                 "transform": None}]}]
    jobs = TC._jobs_from_mappings(maps)
    check(("Diagnosis", "cn2snomed") in jobs, "D1 三段限定名定位到源表 Diagnosis（原取到 SQLUser → 丢任务）")
    check(("MedicationOrder", "cn2rx") in jobs, "D2 两段限定名保持可定位")
    check(len(jobs) == 2, "D3 非 term_map 映射不产生任务")
except Exception as exc:  # noqa: BLE001 - 容器外可能缺 iris.dbapi/backend 包
    print("  ! D 段跳过（依赖 backend 包/IRIS 环境）: %s" % str(exc)[:120])

print("\n==== 结果: %d PASS / %d FAIL ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
