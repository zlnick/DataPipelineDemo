# -*- coding: utf-8 -*-
"""离线回归（2026-09-19）：**同名资产跨数据源时，平台不得静默猜测其数据源**。

实测缺陷（用户手测触发）：两个数据源都有 `SQLUser.Patient`（`DS10122` 测试SQLDB/USER 与
`DS97297` 模拟HIS/Clinic）。一条 `Patient → Patient(FHIR)` 映射的 `source_id` 被*静默*填成
`DS10122`，于是平台按 `(源数据源, 目标)` 分组后，Clinic 组只有 3 张子表 → 缺患者主表 →
`sql2fhir-patient-tx` 布局推导 500：
「mapping 中没有目标为 Patient 的源表，无法确定患者主表（sql2fhir 以患者为起始）」。

根因 = 两处「按资产名反查数据源」用 `setdefault`（first-wins）**静默吞掉重名**：
  ① `routes/mappings.py::_asset_source_index` / `_backfill_source_ids`（保存入口）
  ② `routes/ai.py::_asset_source_index`（`/ai/recommend` 归一）

修复 = **歧义一律不猜**（该名字不参与反查；请求资产优先、登记兜底只补空缺），并把歧义清单
显式回给调用方（`source_ambiguous`）+ 日志；另给「缺患者主表」错误补可照做的提示。
**AI 决策不变**（平台只是不再替 AI/用户猜数据源归属）。

本测试纯离线（repository 的登记读取 monkeypatch 成内存字典，不碰 IRIS、不调 LLM）：
  A 索引：重名跨源 → 不进索引（不猜）；唯一名字照旧命中
  B 保存入口：无 source_id + 重名 → **不回填**且进 ambiguous（不再静默错归属）
  C `/ai/recommend` 索引：请求资产优先（无回归）+ 登记重名不猜 + 唯一名字仍有效
  D 缺患者主表提示：报出"该映射归属哪个源 / 本组源是谁" + 两条可照做的修法
  E 路由/代码接线静态断言（响应字段与两处错误包装在位）
用法（容器内）：docker exec -i dataflow-backend python - < tools/test_source_ambiguity.py
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


from backend.services import repository as repo          # noqa: E402
from backend.routes import ai as AI                      # noqa: E402
from backend.routes import mappings as MP                # noqa: E402
from backend.routes import pipelines as PL               # noqa: E402

# ---------------------------------------------------------------- 内存替身（只读登记）
SOURCES = ["DS10122", "DS97297"]          # USER（测试SQLDB） / Clinic（模拟HIS）
ASSETS = {
    "DS10122": [{"id": "DS10122_TSQLUser.Patient", "name": "Patient", "source_id": "DS10122"},
                {"id": "DS10122_TSQLUser.Observation", "name": "Observation",
                 "source_id": "DS10122"}],
    "DS97297": [{"id": "DS97297_TSQLUser.Patient", "name": "Patient", "source_id": "DS97297"},
                {"id": "DS97297_TSQLUser.Diagnosis", "name": "Diagnosis",
                 "source_id": "DS97297"},
                {"id": "DS97297_TSQLUser.Encounter", "name": "Encounter",
                 "source_id": "DS97297"},
                {"id": "DS97297_TSQLUser.MedicationOrder", "name": "MedicationOrder",
                 "source_id": "DS97297"}],
}
REG = list(ASSETS["DS10122"]) + list(ASSETS["DS97297"])   # ^demo.SourceAsset 登记口径

repo.list_datasources = lambda: [{"id": s, "type": "SQL"} for s in SOURCES]   # noqa: E731
repo.list_assets = lambda ds: list(ASSETS.get(ds) or [])                      # noqa: E731
repo.list_source_assets = lambda: list(REG)                                   # noqa: E731
repo.list_mappings = lambda: [                                               # noqa: E731
    {"id": "M404223", "source": "Patient", "target_table": "Patient", "target_type": "FHIR",
     "source_id": "DS10122"}]
repo.list_targets = lambda: []                                               # noqa: E731

print("==== A. 资产名 → 数据源 索引：重名跨源不猜 ====")
idx, amb = MP._asset_source_index()
check("patient" not in idx, "A1 重名 `patient` 不进索引（不猜；实测旧实现会静默取 DS10122）")
check(sorted(amb.get("patient") or []) == ["DS10122", "DS97297"],
      "A2 歧义清单带全部候选（%s）" % (amb.get("patient"),))
check(idx.get("diagnosis") == "DS97297" and idx.get("observation") == "DS10122",
      "A3 唯一名字照旧命中（diagnosis→DS97297 / observation→DS10122，无回归）")
check(idx.get("ds97297_tsqluser.patient") == "DS97297",
      "A4 资产 ID 键（唯一）仍可反查（限定名路径不受影响）")

print("\n==== B. 保存入口：重名 → 不回填 + 显式报告 ====")
ms = [{"id": "T1", "source": "Patient", "target_table": "Patient", "target_type": "FHIR"}]
filled, unresolved, ambiguous = MP._backfill_source_ids(ms)
check(filled == 0 and not ms[0].get("source_id"),
      "B1 未声明数据源 + 重名 → **不回填**（留空，避免静默挂错源）")
check(ambiguous.get("Patient") == ["DS10122", "DS97297"], "B2 进 ambiguous 并带候选（%s）" % ambiguous)
check(unresolved == [], "B3 不算作『无法解析』（语义区分：有候选但歧义 vs 查不到）")
ms2 = [{"id": "T2", "source": "Diagnosis", "target_table": "Condition", "target_type": "FHIR"}]
filled2, un2, amb2 = MP._backfill_source_ids(ms2)
check(filled2 == 1 and ms2[0]["source_id"] == "DS97297" and not amb2,
      "B4 唯一名字正常回填（Diagnosis → DS97297，无回归）")
ms3 = [{"id": "T3", "source": "Patient", "target_table": "Patient", "target_type": "FHIR",
        "source_id": "DS97297"}]
filled3, _u3, amb3 = MP._backfill_source_ids(ms3)
check(filled3 == 0 and ms3[0]["source_id"] == "DS97297" and not amb3,
      "B5 已声明 source_id → 不动（尊重显式口径）")


print("\n==== C. /ai/recommend 索引：请求资产优先（无回归） ====")
req_assets = [{"id": "DS97297_TSQLUser.Patient", "name": "Patient", "source_id": "DS97297"}]
idx_c = AI._asset_source_index(req_assets, registry_fallback=True)
check(idx_c.get("patient") == "DS97297",
      "C1 请求带 source_id（= 用户选中的源）→ 采信请求口径，不被登记里另一个源覆盖")
rep = {}
idx_d = AI._asset_source_index([{"id": "X", "name": "Patient"}], registry_fallback=True, report=rep)
check("patient" not in idx_d, "C2 请求没带 source_id + 登记重名 → 不猜（该键不参与反查）")
check(sorted((rep.get("ambiguous") or {}).get("patient") or []) == ["DS10122", "DS97297"],
      "C3 歧义经 report 回给调用方（%s）" % rep.get("ambiguous"))
idx_e = AI._asset_source_index([], registry_fallback=True)
check(idx_e.get("diagnosis") == "DS97297" and idx_e.get("observation") == "DS10122",
      "C4 登记兜底对唯一名字仍有效（无回归）")

print("\n==== D. 「缺患者主表」提示：报事实 + 给动作 ====")
hint = PL._missing_patient_root_hint([{"source": "Diagnosis", "source_id": "DS97297"}], "DS97297")
check("归属数据源 DS10122" in hint and "DS97297" in hint,
      "D1 指出 `Patient` 映射归属别的源、而本组源是谁")
check("AI 智能匹配" in hint and "source_id" in hint, "D2 给出两条可照做的修法")
check(PL._missing_patient_root_hint([{"source": "Diagnosis", "source_id": "DS97297"}], "DS10122") == "",
      "D3 同源时不产生噪声提示（无误报）")

# D4-D6（2026-09-19 追加：实测用户 UI 路径失败的两类情形）
repo.list_mappings = lambda: [                                               # noqa: E731
    {"id": "M638710", "source": "Patient", "target_table": "Patient", "target_type": "FHIR",
     "source_id": None}]
h_empty = PL._missing_patient_root_hint([{"source": "Diagnosis", "source_id": "DS82124"}], "DS82124")
check("未声明数据源" in h_empty and "M638710" in h_empty,
      "D4 归属为空的同类映射 → 提示『未声明数据源』并点名（实测 M638710 即此情形）")
check("只勾选本组源的资产" in h_empty, "D5 给出 UI 侧可照做的动作（只勾本组源的资产）")
repo.list_mappings = lambda: []                                              # noqa: E731
h_none = PL._missing_patient_root_hint([{"source": "Diagnosis", "source_id": "DS82124"}], "DS82124")
check("没有任何" in h_none and "AI 智能匹配" in h_none,
      "D6 完全没有同类映射 → 提示『缺主表』并指向匹配页（此前该情形无任何提示）")
repo.list_mappings = lambda: [                                               # noqa: E731
    {"id": "M404223", "source": "Patient", "target_table": "Patient", "target_type": "FHIR",
     "source_id": "DS10122"}]

print("\n==== E. 接线静态断言 ====")


def _src(rel):
    for base in (ROOT, "/app"):
        p = os.path.join(base, rel)
        if os.path.isfile(p):
            return open(p, encoding="utf-8").read()
    return ""


mp_src, ai_src, pl_src = (_src("backend/routes/mappings.py"), _src("backend/routes/ai.py"),
                          _src("backend/routes/pipelines.py"))
check('"source_ambiguous"' in mp_src, "E1 /api/mappings 响应透出 source_ambiguous")
check('"source_ambiguous"' in ai_src, "E2 /api/recommend 响应透出 source_ambiguous")
check(pl_src.count("_missing_patient_root_hint") >= 3,
      "E3 单/多管道两条错误路径都接上提示（出现 %d 次）" % pl_src.count("_missing_patient_root_hint"))
check("放弃猜测" in mp_src and "放弃猜测" in ai_src, "E4 日志明说『放弃猜测』（可观测）")

print("\n==== 结果: %d PASS / %d FAIL ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
