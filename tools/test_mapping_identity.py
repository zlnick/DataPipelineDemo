# -*- coding: utf-8 -*-
"""单元验证（2026-09-17 3×3 全组合前置修复：「缺陷 A」映射身份缺**数据源维度**）。

背景：演示要同时装 3 个源（FHIR `DS08385` / SQL USER / SQL CLINIC），它们都有名为 `Patient`
的资产。身份若只含 (源末段, 目标实体, 目标类型)，则 3 源 × 3 目标 = 9 条映射会被判成
**同一条转换关系**，保存时互相覆盖（只剩 3 条）——9 组合无法共存。

修复：身份 = **(数据源, 源, 目标实体, 目标类型)**；`source_id` 由
`/ai/recommend` 归一（`routes/ai.py::_canonicalize_recs`，按资产回填）与
`/mappings` 保存入口（`routes/mappings.py::_backfill_source_ids`）双重保障。

本测试锁死（纯离线：把 repository 的 global 读写 monkeypatch 成内存字典，不碰 IRIS）：
  A. 缺陷复现（无 source_id → 9 条塌成 3 条）与修复后（9 条共存、互不覆盖）
  B. 幂等：同（数据源+源+目标+类型）重复保存 → 复用 id
  C. 历史兼容/认领：库里未声明数据源的旧行，被同身份且带数据源的提交认领（不复制）
  D. **绝不跨源覆盖**：带数据源的提交不命中别的数据源那条
  E. 未声明数据源的提交只与同样未声明的行同身份（不会覆盖已有数据源的行）
  F. 同身份重复行归并 + 被删 id 的管道引用改写
  G. N10 回归：名称末段归一（`DS_A_T.SQLUser.Patient` ≡ `Patient`）仍成立
  H. `/ai/recommend` 出口带 source_id、`/mappings` 入口回填（含未解析清单）
"""
import copy
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


from backend.services import repository as repo  # noqa: E402
from backend.services import pipeline_instances as pi  # noqa: E402

# ---------------------------------------------------------------- 内存替身
STORE: dict = {}
REWRITES: list = []


def _reset():
    STORE.clear()
    REWRITES.clear()


repo.set_json = lambda g, k, d: STORE.__setitem__(k, copy.deepcopy(d))          # noqa: E731
repo.delete_json = lambda g, k: (STORE.pop(k, None) is not None)                # noqa: E731
repo.list_mappings = lambda: [copy.deepcopy(v) for v in STORE.values()]         # noqa: E731
pi.rewrite_mapping_id = lambda old, new: REWRITES.append((old, new))            # noqa: E731


def M(mid, source_id, target_table, target_type, tag="", source="Patient"):
    """构造一条映射（tag 用于区分内容，验证"没被覆盖"）。"""
    m = {"id": mid, "source": source, "target_table": target_table,
         "target_type": target_type, "field_mappings": [{"source": "x", "target": tag or mid}]}
    if source_id:
        m["source_id"] = source_id
    return m


SRCS = ["DS_A", "DS_B", "DS_C"]
TGTS = [("Patient", "DB"), ("Patient", "FHIR"), ("PatientEntity", "SOAP")]

# ---------------------------------------------------------------- A. 缺陷与修复
print("==== A. 缺陷复现 vs 修复后（3 源 × 3 目标 = 9 条共存） ====")
_reset()
flat = []
for i, s in enumerate(SRCS):
    for j, (t, tt) in enumerate(TGTS):
        flat.append(M("R%d" % (i * 3 + j + 1), "", t, tt, tag=s))
repo.save_mappings(flat)
check(len(STORE) == 3, "A1 缺陷复现：**无数据源维度**时 9 条被塌成 3 条（只剩最后写入的那批：%s）"
      % sorted(STORE))

_reset()
flat = []
for i, s in enumerate(SRCS):
    for j, (t, tt) in enumerate(TGTS):
        flat.append(M("R%d" % (i * 3 + j + 1), s, t, tt, tag=s))
repo.save_mappings(flat)
check(len(STORE) == 9, "A2 修复后：9 条映射全部共存（数据源维度生效）")
check(len({repo._mapping_identity(v) for v in STORE.values()}) == 9, "A3 9 条身份互不相同")
check(all(STORE["R%d" % (i * 3 + j + 1)]["field_mappings"][0]["target"] == s
          for i, s in enumerate(SRCS) for j, _ in enumerate(TGTS)),
      "A4 内容未被跨源覆盖（每条仍是自己数据源的 tag）")

# ---------------------------------------------------------------- B. 幂等
print("\n==== B. 幂等（同一转换关系重复保存） ====")
ids_before = sorted(STORE)
repo.save_mappings([M("R1", "DS_A", "Patient", "DB", tag="DS_A")])
check(sorted(STORE) == ids_before, "B1 重复保存不新增条目、不改 id（%d 条）" % len(STORE))
check(repo._mapping_identity(STORE["R1"]) == ("ds_a", "patient", "patient", "db"), "B2 身份含数据源维度")

# ---------------------------------------------------------------- C. 历史兼容 / 认领
print("\n==== C. 历史数据兼容（认领未声明数据源的行） ====")
_reset()
repo.set_json("^demo.Mapping", "L1", M("L1", "", "Patient", "DB", tag="legacy"))
repo.save_mappings([M("L1", "DS_A", "Patient", "DB", tag="claimed")])
check(sorted(STORE) == ["L1"], "C1 未声明数据源的旧行被同身份提交**认领**（不复制成两条）")
check(STORE["L1"].get("source_id") == "DS_A" and STORE["L1"]["field_mappings"][0]["target"] == "claimed",
      "C2 认领后补写数据源维度并更新内容")

# ---------------------------------------------------------------- D/E. 不跨源覆盖
print("\n==== D/E. 绝不跨源覆盖 ====")
_reset()
repo.set_json("^demo.Mapping", "X1", M("X1", "DS_B", "Patient", "DB", tag="B-content"))
repo.save_mappings([M("X1", "DS_C", "Patient", "DB", tag="C-content")])
check(len(STORE) == 2, "D1 不同数据源的同名映射 → 新增而非覆盖（%s）" % sorted(STORE))
check(STORE["X1"]["field_mappings"][0]["target"] == "B-content", "D2 B 源内容未被 C 源改写")
check("X1_2" in STORE, "D3 撞号时派生新 id（X1 → X1_2）")

_reset()
repo.set_json("^demo.Mapping", "Y1", M("Y1", "DS_A", "Patient", "DB", tag="A-content"))
repo.save_mappings([M("Y1", "", "Patient", "DB", tag="no-ds")])
check("Y1_2" in STORE and STORE["Y1"]["field_mappings"][0]["target"] == "A-content",
      "E1 未声明数据源的提交**不覆盖**已声明数据源的行（派生 Y1_2）")
repo.save_mappings([M("Y1", "", "Patient", "DB", tag="no-ds-2")])
check(len(STORE) == 2 and STORE["Y1_2"]["field_mappings"][0]["target"] == "no-ds-2",
      "E2 未声明数据源的提交彼此同身份（幂等复用 Y1_2）")

# ---------------------------------------------------------------- F. 归并 + 引用改写
print("\n==== F. 同身份重复行归并 + 管道引用改写 ====")
_reset()
repo.set_json("^demo.Mapping", "D1", M("D1", "DS_A", "Patient", "DB", tag="old1"))
repo.set_json("^demo.Mapping", "D2", M("D2", "DS_A", "Patient", "DB", tag="old2"))
report = {}
repo.save_mappings([M("D3", "DS_A", "Patient", "DB", tag="new")], report=report)
check(sorted(STORE) == ["D1"], "F1 同身份重复行被归并，只剩一条（id 稳定取最早 D1）")
check(report.get("collapsed") == ["D2"], "F2 report.collapsed 报出被删 id（%s）" % report.get("collapsed"))
check(REWRITES == [("D2", "D1")], "F3 管道实例引用被改写（D2 → D1）：%s" % REWRITES)
check(STORE["D1"]["field_mappings"][0]["target"] == "new", "F4 存活那条内容为本次提交")

# ---------------------------------------------------------------- G. N10 回归
print("\n==== G. N10 回归（末段归一 + 数据源维度） ====")
_reset()
repo.save_mappings([M("R1", "DS_A", "Patient", "DB", tag="n10")])
repo.save_mappings([M("R1", "DS_A", "Patient", "DB", tag="n10", source="DS_A_T.SQLUser.Patient")])
check(sorted(STORE) == ["R1"], "G1 带 schema/数据源前缀的写法归为同一身份（复用 R1）")
repo.save_mappings([M("R1", "DS_D", "Patient", "DB", tag="other-ds", source="DS_D_T.SQLUser.Patient")])
check(sorted(STORE) == ["R1", "R1_2"], "G2 换数据源后是另一条（R1_2），不覆盖 R1")

# ---------------------------------------------------------------- H. 出口/入口回填
print("\n==== H. /ai/recommend 出口与 /mappings 入口回填 ====")
from backend.routes import ai as AIR  # noqa: E402
from backend.routes import mappings as MAPR  # noqa: E402

idx = AIR._asset_source_index([{"id": "DS_A_TSQLUser.Patient", "name": "Patient", "source_id": "DS_A"}])
check(idx.get("ds_a_tsqluser.patient") == "DS_A" and idx.get("patient") == "DS_A",
      "H1 资产索引三键（id/name/末段）都指向数据源")
check(AIR._asset_source_index([{"id": "A1", "name": "Patient"}]) == {},
      "H2 资产未带 source_id → 索引为空（不猜数据源）")

_orig_idx = AIR._asset_source_index
AIR._asset_source_index = (  # noqa: E731
    lambda assets, registry_fallback=False, report=None: {"patient": "DS_A"})
try:
    recs = [{"source": "Patient", "target_table": "Patient", "target_type": "DB"},
            {"source": "DS_A_T.SQLUser.Patient", "target_table": "Patient", "target_type": "DB"}]
    out = AIR._canonicalize_recs(recs, [{"name": "Patient", "id": "A1", "source_id": "DS_A"}], [],
                                 [{"table": "Patient", "type": "DB"}], [])
    check(out[0].get("source_id") == "DS_A" and out[1].get("source_id") == "DS_A",
          "H3 推荐结果被回填 source_id（两种源写法都能命中）")
finally:
    AIR._asset_source_index = _orig_idx

# 2026-09-19：`_asset_source_index` 现在返回 (索引, 歧义清单)、《_backfill_source_ids》多返回歧义清单
# （重名跨源不猜 → 留空 + 显式回报；详见 tools/test_source_ambiguity.py）
_orig_map_idx = MAPR._asset_source_index
MAPR._asset_source_index = (  # noqa: E731
    lambda: ({"patient": "DS_A", "ds_x_tsqluser.patient": "DS_B"}, {}))
try:
    ms = [{"source": "Patient"}, {"source": "DS_X_TSQLUser.Patient"},
          {"source": "Unknown"}, {"source": "Patient", "source_id": "DS_C"}]
    filled, unresolved, ambiguous = MAPR._backfill_source_ids(ms)
    check(filled == 2, "H4 入口回填 2 条未声明数据源的映射（filled=%d）" % filled)
    check(ms[0]["source_id"] == "DS_A" and ms[1]["source_id"] == "DS_B", "H5 按资产名/末段反查数据源")
    check(ms[3]["source_id"] == "DS_C", "H6 已声明数据源不被改写")
    check(unresolved == ["Unknown"], "H7 反查不到**不猜测**：源名进 unresolved 显式回报（%s）" % unresolved)
    check(ambiguous == {}, "H8 无歧义时 ambiguous 为空")
finally:
    MAPR._asset_source_index = _orig_map_idx

# ---------------------------------------------------------------- I. 分组回落守卫
print("\n==== I. 分组回落：声明数据源优先 + 歧义守卫（缺陷 A 连带） ====")
from backend.routes import pipelines as PL  # noqa: E402

_o_list_ds, _o_list_assets, _o_get_ds = repo.list_datasources, repo.list_assets, repo.get_datasource
_o_list_tg = repo.list_targets
DS_ROWS = [{"id": "DS_A", "type": "SQL"}, {"id": "DS_B", "type": "SQL"}, {"id": "DS_C", "type": "FHIR"}]
ASSETS_BY_DS = {"DS_A": [{"name": "Patient", "id": "DS_A_TSQLUser.Patient"}],
                "DS_B": [{"name": "Patient", "id": "DS_B_TSQLUser.Patient"}],
                "DS_C": [{"name": "Patient", "id": "DS_C_TPatient"}]}
try:
    repo.list_datasources = lambda: DS_ROWS
    repo.list_assets = lambda ds_id=None: ASSETS_BY_DS.get(ds_id, [])
    repo.get_datasource = lambda i: next((d for d in DS_ROWS if d["id"] == i), None)
    got = PL._datasource_for_mappings([{"source": "Patient", "source_id": "DS_B"}])
    check((got or {}).get("id") == "DS_B", "I1 声明的 source_id 优先（3 源同名资产不再歧义）")
    check(PL._datasource_for_mappings([{"source": "Patient"}]) is None,
          "I2 三名同源同名且未声明数据源 → **不猜**（None，由下游显式报错）")
    check(PL._datasource_for_mappings([{"source": "Patient", "source_id": "DS_C"}], ds_type="SQL") is None,
          "I3 声明数据源类型与调用方期望不符 → 退化为名字反查后仍歧义 → None（不静默错源）")

    repo.list_targets = lambda: [{"id": "TG_DB", "type": "DB", "tables": [{"table": "Patient"}]},
                                 {"id": "TG_FHIR", "type": "FHIR", "tables": [{"entity_name": "Patient"}]}]
    check((PL._target_for_mappings([{"target_table": "Patient", "target_type": "FHIR"}]) or {}).get("id")
          == "TG_FHIR", "I4 目标类型以映射声明的 target_type 为准（DB/FHIR 同名 Patient 不选错）")
    check(PL._target_for_mappings([{"target_table": "Patient"}]) is None,
          "I5 未声明目标类型且命中多个目标 → 不猜（None）")
finally:
    repo.list_datasources, repo.list_assets, repo.get_datasource = _o_list_ds, _o_list_assets, _o_get_ds
    repo.list_targets = _o_list_tg

print("\n==== 结果: %d PASS / %d FAIL ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
