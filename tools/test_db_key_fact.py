# -*- coding: utf-8 -*-
"""单元验证（2026-09-17 Round 2 缺陷回归）：**DB 目标主键事实链** + 落地判定竞态 + 查询 BO 口径。

背景（实测 P0）：SQL 源（`USER.SQLUser.Patient`）→ DB 目标（`CLINIC.SQLUser.Patient`）时，
Agent A 把源主键 `ID` 映射到目标 `MRN`，**目标主键列 `ID` 从未被产出**；生成的 SQLOperation 是
`INSERT OR UPDATE INTO Patient(... ID ...) VALUES(*ID,...)` → 运行期 IRIS 判 `ID is required`
（3 行源数据 → 9 条消息 Status=8、目标 0 落地），而生成接口在此之前一路 `code:0`（C1 只有
FHIR 必填检查、没有 DB 侧对应检查）。

本测试锁死修复后的不变量（纯离线，不调 LLM、不写任何数据）：
A. 主键事实解析：模型/登记记录优先，缺则 JDBC 现场探查并**回写登记**；跨类型不混用；拿不到即 []（fail-open）；
B. C1 的 DB 主键检查：缺主键列/取不到值判 error，已产出/常量指令放行，FHIR/SOAP 映射不触发；
C. 子表查询派发检查只看**布局声明的查询 BO**（不再把 DB 目标 SQLOperation 当查询 BO）；
D. 目标落地判定带**有界等待**（竞态不误判为失败）。
"""
import sys
import time

PASS = 0
FAIL = 0

DB_COLS = ["ID", "MRN", "FamilyName", "GivenName", "Gender", "BirthDate", "Phone",
           "Address", "City"]
SRC_COLS = ["ID", "FamilyName", "GivenName", "Gender", "BirthDate", "Phone", "Address", "City"]

DB_TARGET_NO_KEY = {"id": "TG_DB", "type": "DB", "name": "CLINIC 目标库",
                    "tables": [{"table": "Patient", "schema": "SQLUser",
                                "columns": [{"name": c} for c in DB_COLS]}]}
DB_TARGET_WITH_KEY = {"id": "TG_DB", "type": "DB", "name": "CLINIC 目标库",
                      "tables": [{"table": "Patient", "schema": "SQLUser",
                                  "key_columns": ["ID"],
                                  "columns": [{"name": c} for c in DB_COLS]}]}
FHIR_TARGET = {"id": "TG_FHIR", "type": "FHIR", "name": "FHIR 存储",
               "tables": [{"table": "Patient", "schema": "FHIR", "modeled": True,
                           "columns": [{"name": "identifier"}, {"name": "family"}]}]}

ASSETS = [{"name": "Patient", "fields": SRC_COLS}]


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % msg)
    else:
        FAIL += 1
        print("  ✗ %s" % msg)


# ---- monkeypatch：目标登记 + JDBC 探查（离线，不连数据库） ----
from backend.services import repository as _repo  # noqa: E402

_registry = {"targets": [DB_TARGET_NO_KEY, FHIR_TARGET]}
_probe_calls = []
_persisted = []
_jdbc_keys = {"value": [], "raise": False}


def _fake_list_targets():
    return _registry["targets"]


def _fake_enrich(target_id, table_name, patch):
    _persisted.append((target_id, table_name, dict(patch)))
    for tg in _registry["targets"]:
        for tb in tg.get("tables") or []:
            if tb.get("table") == table_name:
                tb.update(patch)


def _fake_list_primary_keys(conn, schema, table):
    _probe_calls.append((schema, table))
    if _jdbc_keys["raise"]:
        raise RuntimeError("driver does not support getPrimaryKeys")
    return list(_jdbc_keys["value"])


_repo.list_targets = _fake_list_targets
_repo.enrich_target_table = _fake_enrich

from backend.services import db_target_keys as DBTK  # noqa: E402
from backend.services import jdbc_client as JC  # noqa: E402
from backend.services import pipeline_validator as PV  # noqa: E402
from backend.services import transformation_validator as TV  # noqa: E402

JC.list_primary_keys = _fake_list_primary_keys

DB_CONN_TARGET = {"id": "TG_DB", "type": "DB",
                  "connection": {"jdbc_url": "jdbc:IRIS://iris:1972/CLINIC"},
                  "tables": [{"table": "Patient", "schema": "SQLUser",
                              "columns": [{"name": c} for c in DB_COLS]}]}


def reset_facts():
    _probe_calls.clear()
    _persisted.clear()
    _jdbc_keys.update({"value": [], "raise": False})
    _registry["targets"] = [DB_TARGET_NO_KEY, FHIR_TARGET]


print("==== A. 主键事实解析（db_target_keys.key_columns_of） ====")
reset_facts()
keyed_model = [{"table": "Patient", "type": "DB", "key_columns": ["ID"]}]
check(DBTK.key_columns_of("Patient", "DB", "SQLUser", keyed_model) == ["ID"],
      "A1 target_models 已带 key_columns → 直接采用（不探查）")
check(not _probe_calls, "A2 采用模型事实时不触发 JDBC 探查")
check(DBTK.key_columns_of("Patient", "FHIR", "FHIR", keyed_model) == [],
      "A3 声明 FHIR → 不解析 DB 主键（同名跨类型不混用）")
check(DBTK.key_columns_of("Patient", "SOAP", "", keyed_model) == [], "A4 声明 SOAP → 不解析")
reset_facts()
_registry["targets"] = [DB_CONN_TARGET]        # 有连接（可探查）但探查结果为空
check(DBTK.key_columns_of("Patient", "DB", "SQLUser") == [], "A5 登记无事实＋探查空 → []（fail-open）")
check(bool(_probe_calls), "A6 登记缺事实时会现场探查（%s）" % (_probe_calls[:1],))
reset_facts()
_registry["targets"] = [DB_TARGET_NO_KEY]      # 无连接信息 → 不探查，直接 fail-open
check(DBTK.key_columns_of("Patient", "DB", "SQLUser") == [] and not _probe_calls,
      "A6b 登记无 JDBC 连接信息 → 不探查、返回 []")
reset_facts()
_jdbc_keys["value"] = ["ID"]
_registry["targets"] = [DB_CONN_TARGET]
keys = DBTK.key_columns_of("Patient", "DB", "SQLUser")
check(keys == ["ID"], "A7 现场探查得到主键 %s" % (keys,))
check(bool(_persisted) and _persisted[0][2].get("key_columns") == ["ID"],
      "A8 探查结果回写登记（%s）" % (_persisted[:1],))
reset_facts()
_jdbc_keys["raise"] = True
check(DBTK.key_columns_of("Patient", "DB", "SQLUser") == [],
      "A9 探查抛异常（驱动不支持/不可达）→ [] 且不抛出（fail-open）")

print("\n==== B. C1 的 DB 主键检查（transformation_validator._db_key_issues） ====")
reset_facts()
_registry["targets"] = [DB_TARGET_WITH_KEY, FHIR_TARGET]
m_missing = {"id": "R1", "source": "Patient", "target_table": "Patient", "target_type": "DB",
             "field_mappings": [{"source": "Patient.ID", "target": "MRN", "transform": None},
                                {"source": "Patient.FamilyName", "target": "FamilyName"}]}
issues = TV._db_key_issues([m_missing],
                           target_models=[{"table": "Patient", "type": "DB",
                                           "key_columns": ["ID"]}], assets=ASSETS)
check(len(issues) == 1 and issues[0].get("check") == "db_key",
      "B1 目标主键 ID 未被映射 → 1 条 db_key error（%s）" % ([i.get("check") for i in issues],))
check("ID" in issues[0]["message"] and "is required" in issues[0]["message"],
      "B2 报错含事实（列名 + 运行期现象 \"ID is required\"）")

m_ok = {"id": "R1", "source": "Patient", "target_table": "Patient", "target_type": "DB",
        "field_mappings": [{"source": "Patient.ID", "target": "MRN"},
                           {"source": "Patient.ID", "target": "ID"}]}
check(TV._db_key_issues([m_ok], assets=ASSETS) == [], "B3 已为主键产出值 → 无 error（登记事实）")

m_bad_src = {"id": "R1", "source": "Patient", "target_table": "Patient", "target_type": "DB",
             "field_mappings": [{"source": "Patient.NotAColumn", "target": "ID"}]}
issues = TV._db_key_issues([m_bad_src], assets=ASSETS)
check(len(issues) == 1 and issues[0].get("check") == "db_key_value",
      "B4 主键有映射但源列不存在 → db_key_value error")

m_const = {"id": "R1", "source": "Patient", "target_table": "Patient", "target_type": "DB",
           "field_mappings": [{"source": None, "target": "ID", "transform": "constant:SRC-1"}]}
check(TV._db_key_issues([m_const], assets=ASSETS) == [],
      "B5 主键用 constant:<值> 指令产出 → 放行（值级可生成）")

m_fhir = {"id": "R2", "source": "Patient", "target_table": "Patient", "target_type": "FHIR",
          "field_mappings": [{"source": "Patient.family", "target": "family"}]}
check(TV._db_key_issues([m_fhir], assets=ASSETS) == [],
      "B6 FHIR 映射（同名 Patient）→ 不触发 DB 主键检查（按类型分域）")

m_soap = {"id": "R3", "source": "Patient", "target_table": "Patient", "target_type": "SOAP",
          "field_mappings": []}
check(TV._db_key_issues([m_soap], assets=ASSETS) == [], "B7 SOAP 映射 → 不触发")

reset_facts()          # 登记无主键事实 + 探查不可用 → fail-open
_registry["targets"] = [DB_TARGET_NO_KEY]
check(TV._db_key_issues([m_missing], assets=ASSETS) == [],
      "B8 拿不到主键事实 → 跳过检查（fail-open，不误报）")

res = TV.validate_mapping_against_constraints([m_missing])
check(any(i.get("check") == "db_key" for i in res.get("issues") or []) is False,
      "B9 强校验在无主键事实时同样 fail-open（不误报）")

print("\n==== C. 子表查询派发检查只看布局声明的查询 BO ====")
TOPO_MULTI = {"production": "demo.DataflowProduction", "components": [
    {"type": "SQLService", "name": "SQLService_Patient"},
    {"type": "SQLOperation", "name": "SQLOp_Patient"},              # DB 目标操作组件
    {"type": "SQLOperation", "name": "SQLQueryOp_Encounter"},       # 查询 BO
    {"type": "PatientTxProcess", "name": "SqlFhirPatientTxProcess"},
]}
COUNTS = {"SqlFhirPatientTxProcess": 3, "SQLQueryOp_Encounter": 2, "SQLOp_Patient": 9}


def _fake_query(sql, params=None):
    if "Ens.MessageHeader" in str(sql) and params:
        return [(COUNTS.get(str(params[0]), 0),)]
    return [(0,)]


_orig_query = PV.iris_connector.query
PV.iris_connector.query = _fake_query
_orig_layout = PV._layout_query_bos
# 说明：`_layout_query_bos` 现接受拓扑（按拓扑过滤陈旧布局），桩需同样接受参数
PV._layout_query_bos = lambda *_a, **_k: ["SQLQueryOp_Encounter"]
res = PV.check_query_dispatch(TOPO_MULTI)
check(res.get("checked") and res.get("ok") and not res.get("missing"),
      "C1 布局声明查询 BO 已派发 → ok（missing=%s）" % (res.get("missing"),))
PV._layout_query_bos = lambda *_a, **_k: []
res = PV.check_query_dispatch(TOPO_MULTI)
check(res.get("checked") is False and not res.get("missing"),
      "C2 多 SQLOperation 且无布局 → 跳过检查（不把 DB 目标 SQLOp_Patient 当查询 BO）")
check(any("无法区分查询 BO" in (i.get("message") or "") for i in (res.get("issues") or [])),
      "C3 跳过时给出可读原因")
PV._layout_query_bos = lambda *_a, **_k: ["SQLQueryOp_Diagnosis"]     # 布局声明但拓扑里不存在
res = PV.check_query_dispatch(TOPO_MULTI)
check(res.get("checked") is False, "C4 布局声明的 BO 不在本次拓扑 → 跳过（防陈旧布局误报）")

# —— 2026-09-18 实测缺陷回归：**单表来源**的 sql2fhir 组（布局 query_bos=[]）不能再把
#    拓扑里唯一的 SQLOperation（= DB 目标）当查询 BO（否则误报 error → 回喂 bp_code 修复 →
#    AI 幻觉出 QueryChild/Ens.Util.Log → 编译失败 #1054）。
TOPO_SINGLE = {"production": "demo.DataflowProduction", "components": [
    {"type": "SQLOperation", "name": "SQLOp_PatientSource"},           # 唯一的 SQLOperation = DB 目标
    {"type": "PatientTxProcess", "name": "SqlFhirPatientTxProcess"},
]}
_orig_present = PV._layout_present
PV._layout_query_bos = lambda *_a, **_k: []
PV._layout_present = lambda *_a, **_k: True
res = PV.check_query_dispatch(TOPO_SINGLE)
check(res.get("checked") is False and not res.get("missing"),
      "C5 单表来源（布局无 query_bos）+ 唯一 SQLOperation 是 DB 目标 → 跳过（不误报）")
check(any("单表来源" in (i.get("message") or "") for i in (res.get("issues") or [])),
      "C6 跳过原因写明「单表来源」（可读、可排障）")
PV._layout_present = lambda *_a, **_k: False           # 布局完全读不到
res = PV.check_query_dispatch(TOPO_SINGLE)
check(res.get("checked") is False and not res.get("missing") and not res.get("bp_messages"),
      "C7 无布局声明 + 唯一 SQLOperation → 跳过（fail-open，不再按名推断为查询 BO）")
check(any("无法确认唯一的 SQLOperation" in (i.get("message") or "")
          for i in (res.get("issues") or [])),
      "C8 跳过原因写明「唯一 SQLOperation 不能当查询 BO」")
PV._layout_present = _orig_present
PV._layout_query_bos = _orig_layout
PV.iris_connector.query = _orig_query

print("\n==== D. 目标落地判定有界等待（防生成后立即计数的竞态假失败） ====")
_calls = {"n": 0}
_orig_eval = PV._evaluate_target_effect


def _flaky(*a, **kw):
    _calls["n"] += 1
    if _calls["n"] < 2:
        return {"ok": False, "counts": {"Patient": 0},
                "issues": [{"severity": "error", "item": "Patient",
                            "message": "FHIR Patient 落地数 0 < 预期 1"}]}
    return {"ok": True, "counts": {"Patient": 3}, "issues": []}


PV._evaluate_target_effect = _flaky
t0 = time.monotonic()
res = PV.check_target_effect(["FHIR"], {"Patient": 1}, "", strict=True, wait_seconds=6)
elapsed = round(time.monotonic() - t0, 1)
check(res.get("ok") and _calls["n"] >= 2,
      "D1 首次判失败 → 复判后通过（调用 %d 次，耗时 %ss）" % (_calls["n"], elapsed))
_calls["n"] = 0
t0 = time.monotonic()


def _always_fail(*a, **kw):
    _calls["n"] += 1
    return {"ok": False, "counts": {"Patient": 0},
            "issues": [{"severity": "error", "item": "Patient",
                        "message": "FHIR Patient 落地数 0 < 预期 1"}]}


PV._evaluate_target_effect = _always_fail
res = PV.check_target_effect(["FHIR"], {"Patient": 1}, "", strict=True, wait_seconds=4)
elapsed = round(time.monotonic() - t0, 1)
check((not res.get("ok")) and elapsed <= 12,
      "D2 始终失败 → 有界返回（判 %d 次，耗时 %ss，不无限等待）" % (_calls["n"], elapsed))
_calls["n"] = 0
PV.check_target_effect(["FHIR"], {"Patient": 1}, "", strict=True)     # wait_seconds=0
check(_calls["n"] == 1, "D3 wait_seconds=0 → 单次判定（与旧行为一致）")
PV._evaluate_target_effect = _orig_eval


print("\n==== K. 有界等待首个业务消息（2026-09-18 检测盲区：BP 零消息时直接跳过） ====")
_calls_k = {"n": 0}
_orig_fake = PV.iris_connector.query


def _late_query(sql, params=None):
    """前 2 次 BP 计数为 0（源适配器还没轮询），之后给 3 —— 模拟"消息稍后到达"。"""
    if "Ens.MessageHeader" in str(sql) and params:
        target = str(params[0])
        if target == "SqlFhirPatientTxProcess":
            _calls_k["n"] += 1
            return [(_calls_k["n"] if _calls_k["n"] > 2 else 0,)]
        return [(0,)]        # 查询 BO 从未被派发
    return [(0,)]


PV.iris_connector.query = _late_query
PV._layout_query_bos = lambda *_a, **_k: ["SQLQueryOp_Encounter"]
res = PV.check_query_dispatch(TOPO_MULTI, wait_seconds=6)
check(res.get("checked") is True and res.get("missing") == ["SQLQueryOp_Encounter"],
      "K1 等待窗口内消息到达 → 判定为「从未派发子表查询」error（%s）" % (res.get("missing"),))
check(res.get("bp_messages") == 3, "K2 bp_messages 取到等待后的真实值（%s）" % res.get("bp_messages"))
_calls_k["n"] = 0
res0 = PV.check_query_dispatch(TOPO_MULTI, wait_seconds=0)
check(res0.get("checked") is False, "K3 wait_seconds=0 → 旧行为（零消息即跳过，不等待）")
PV.iris_connector.query = _orig_fake
PV._layout_query_bos = _orig_layout

print("\n==== 结果: %d PASS / %d FAIL ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
