# -*- coding: utf-8 -*-
"""单元验证（2026-09-17 Round 2 缺陷回归）：**DB 目标列清单必须按目标库取**。

背景（实测 P1，静默丢列）：DB 目标 SQLOp 的 UPSERT 列清单固定查当前命名空间（USER）的
`information_schema`；演示默认目标是 **CLINIC**（跨库），USER 里的同名 `Patient` 只有 8 列
（无 MRN），CLINIC 的是 9 列 → 生成的 `INSERT OR UPDATE INTO Patient (…)` **缺 MRN** →
映射里 `→ MRN` 的条目永远不落库（消息全 Completed、校验全绿，无任何告警）。

本测试锁死修复后的取值顺序（登记事实 → 目标命名空间 → 当前命名空间兜底 + 告警）：
A. `columns_of` 优先用 `target_config["table_columns"]`（登记事实，零查询）；
B. `_registered_table_columns` 从目标登记提取 {表→列} + schema；
C. **真实跨库探针**（需 IRIS）：CLINIC.Patient 有 9 列含 MRN、USER.Patient 只有 8 列（无 MRN）；
D. 无 jdbc_url 时兜底到当前命名空间（历史行为，但必须能取到列）。
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


from backend.services import db_target_columns as DTC  # noqa: E402

print("==== A/B. 登记事实优先 + 登记提取 ====")
CFG = {"table_columns": {"Patient": ["ID", "MRN", "FamilyName"]}, "schema": "SQLUser"}
check(DTC.columns_of("Patient", CFG) == ["ID", "MRN", "FamilyName"],
      "A1 table_columns 命中 → 直接返回登记事实（含 MRN）")
check(DTC.columns_of("patient", CFG) == ["ID", "MRN", "FamilyName"],
      "A2 表名大小写不敏感")
check(DTC.columns_of("Patient", {"columns": [{"name": "A"}, "B"]}) == ["A", "B"],
      "A3 兼容单表 columns 形态（dict/str 混合）")
check(DTC.columns_of("", CFG) == [] and DTC.columns_of("Nope", {}) == [],
      "A4 空表名/无任何来源时不误报")

try:
    from backend.routes.pipelines import _registered_table_columns as REG
    tg = {"tables": [{"table": "Patient", "schema": "SQLUser",
                      "columns": [{"name": "ID"}, {"name": "MRN"}]},
                     {"table": "Encounter", "schema": "SQLUser", "columns": [{"name": "ID"}]}]}
    cols_by, schema = REG(tg)
    check(cols_by.get("Patient") == ["ID", "MRN"] and cols_by.get("Encounter") == ["ID"],
          "B1 登记提取：{表→列清单}")
    check(schema == "SQLUser", "B2 登记提取：schema")
except Exception as exc:  # noqa: BLE001
    print("  ! B 段跳过（依赖 backend 路由包/Flask 环境）: %s" % str(exc)[:120])

print("\n==== C/D. 真实跨库探针（需 IRIS；非容器环境自动跳过） ====")
CLINIC_CFG = {"jdbc_url": "jdbc:IRIS://iris:1972/CLINIC", "schema": "SQLUser"}
try:
    clinic_cols = DTC.columns_of("Patient", CLINIC_CFG)
    user_cols = DTC.columns_of("Patient", {})
    print("     CLINIC.Patient =", clinic_cols)
    print("     USER.Patient   =", user_cols)
    check("MRN" in clinic_cols, "C1 目标命名空间 CLINIC 的 Patient 列含 MRN（跨库正确来源）")
    check("MRN" not in user_cols or not user_cols,
          "C2 当前命名空间 USER 的同名表列不含 MRN（正是原缺陷取错的来源）")
    check(len(clinic_cols) > len(user_cols or []),
          "C3 两库列清单确实不同（%d vs %d）——证明按目标库取改变了结果"
          % (len(clinic_cols), len(user_cols or [])))
    check(bool(user_cols), "D1 无 jdbc_url 时兜底当前命名空间仍能取到列（历史行为保留）")
except Exception as exc:  # noqa: BLE001
    print("  ! C/D 段跳过（无 IRIS 连接）: %s" % str(exc)[:140])

print("\n==== 结果: %d PASS / %d FAIL ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
