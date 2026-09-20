# -*- coding: utf-8 -*-
"""单元验证（缺陷 M 回归）：「按目标名取列结构」必须**类型感知**——同名跨类型不得混用。

背景（2026-09-16 3源×3目标 Round 1 实测，一次毁掉两组管道）：
`Patient` 既是目标库表名（DB，9 列 ID/FamilyName/…）又是 FHIR 资源名（5 列 identifier/family/…）：

1. `routes/pipelines.py::_c1_target_models` 对**所有**映射按名注入 FHIR 模型 →
2. `pipeline_validator._get_table_columns("Patient")` 名字命中即返回 **FHIR 列清单** →
3. `check_recommendations` 报「目标列 ID 不存在于目标表 Patient」，`_l1_fix_mappings` 把 DB 映射
   的 9 个真实列**整批静默剔除**（fail-closed，无任何告警）→
4. `_fhir_required_issues` 又要求 FHIR 必填列 → C1 的 LLM 把映射改写成 `identifier/family/given/
   gender/birthDate`（DB 映射被"升级"成 FHIR 列名）→
5. 运行期：DB 目标 SQLOp 缺 `Patient.ID` → `ID is required`；FHIR/SOAP 组连带失败。

本测试锁死修复后的四条不变量（纯离线，不调 LLM、不写任何数据）：
A. DB 映射取列只认 DB（`schema=SQLUser`），绝不返回 FHIR 列；
B. FHIR 映射取列只认 FHIR 模型（`schema=FHIR`）；
C. 注入的 FHIR 模型不会污染 DB 映射的列解析；
D. `_c1_target_models` 只为 FHIR（或未声明类型）映射注入 FHIR 模型；
E/F. C1 的 FHIR 必填/结构检查对已声明为 DB/SOAP 的映射**不触发**，L1 也不剔除其字段；
G/H. FHIR 映射的检查仍然生效（修复没有把检查关掉）。
"""
import sys
import types

PASS = 0
FAIL = 0

DB_COLS = ["ID", "MRN", "FamilyName", "GivenName", "Gender", "BirthDate",
           "Phone", "Address", "City"]
FHIR_COLS = ["identifier", "family", "given", "gender", "birthDate",
             "phone", "address_line", "city"]
FHIR_REQUIRED = FHIR_COLS[:5]          # 与 fhir_target_model 的 US Core Patient 必填列一致

# 合成目标登记（复刻真实结构：target 级带 type，实体级带 schema）
DB_TARGET = {"id": "TG_DB", "type": "DB", "name": "CLINIC 目标库",
             "tables": [{"table": "Patient", "entity_name": "Patient", "schema": "SQLUser",
                         "columns": [{"name": c} for c in DB_COLS]}]}
FHIR_TARGET = {"id": "TG_FHIR", "type": "FHIR", "name": "FHIR 存储",
               "tables": [{"table": "Patient", "entity_name": "Patient", "schema": "FHIR",
                           "modeled": True, "open": False, "columns": [{"name": c} for c in FHIR_COLS]}]}


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % msg)
    else:
        FAIL += 1
        print("  ✗ %s" % msg)


# ---- monkeypatch：让 "按名取列" 的真实路径（repository.list_targets）返回合成登记 ----
from backend.services import repository as _repo  # noqa: E402

_repo.list_targets = lambda: [DB_TARGET, FHIR_TARGET]

from backend.services import pipeline_validator as PV  # noqa: E402
from backend.services import transformation_validator as TV  # noqa: E402
from backend.routes.pipelines import _c1_target_models  # noqa: E402


def db_mapping():
    """DB 映射（正确形态）：FHIR Patient 源 → CLINIC.Patient 表 9 列。"""
    return {"id": "R1", "source": "Patient", "target_table": "Patient", "target_type": "DB",
            "target_id": "TG_DB",
            "field_mappings": [{"source": c, "target": c, "transform": "direct"} for c in DB_COLS]}


def fhir_mapping(missing=None):
    """FHIR 映射：源 FHIR Patient → 目标 FHIR Patient（5 列）。"""
    fms = [{"source": c, "target": c, "transform": "direct"} for c in FHIR_COLS if c != missing]
    return {"id": "R1_2", "source": "Patient", "target_table": "Patient", "target_type": "FHIR",
            "target_id": "TG_FHIR", "field_mappings": fms}


print("A. DB 映射取列只认 DB（schema=SQLUser）")
cols_db = PV._get_table_columns("Patient", None, "DB")
check(not [c for c in cols_db if c in FHIR_COLS], "列清单不含 FHIR 列: %s" % cols_db[:3])
check("ID" in cols_db or "FamilyName" in cols_db, "列清单来自 DB 表（ID/FamilyName…）")
check(cols_db != FHIR_COLS, "未把 FHIR 列清单当 DB 列清单")

print("B. FHIR 映射取列只认 FHIR 模型（schema=FHIR）")
cols_fhir = PV._get_table_columns("Patient", None, "FHIR")
check(cols_fhir == FHIR_COLS, "得到 FHIR 模型列: %s" % cols_fhir)
check("ID" not in cols_fhir, "FHIR 视角下不含 DB 列 ID（不会误用 SQL 列名）")

print("C. 注入的 FHIR 模型不污染 DB 映射列解析（L1 剔除判据）")
cols_mixed = PV._get_table_columns("Patient", _c1_target_models([fhir_mapping()]), "DB")
check(not [c for c in cols_mixed if c in FHIR_COLS],
      "即使入参带 FHIR 模型，DB 视角仍拿不到 identifier/family: %s" % cols_mixed[:3])

print("D. _c1_target_models 只为 FHIR/未声明类型注入")
check(_c1_target_models([db_mapping()]) == [], "DB 映射 → 不注入 FHIR 模型")
check(len(_c1_target_models([fhir_mapping()])) == 1, "FHIR 映射 → 注入 1 个 FHIR 模型")
check(len(_c1_target_models([{**fhir_mapping(), "target_type": ""}])) == 1,
      "未声明类型 → 按名字推断，仍注入（历史兼容）")

print("E. C1 的 FHIR 必填检查对 DB 映射不触发")
m_db = db_mapping()
check(TV._fhir_scope_of(m_db) is None, "_fhir_scope_of(DB 映射) = None")
req_db = TV._fhir_required_issues([m_db], assets=[])
check(req_db == [], "无 fhir_required 误报（修复前会要求 identifier/family/…）")
check(TV._fhir_structure_issues([m_db]) == [], "无 fhir_structure 误报")

print("F. L1 不剔除 DB 映射字段（fail-closed 误删回归）")
fixed, changed = TV._l1_fix_mappings([m_db], _c1_target_models([m_db]))
check(changed is False, "L1 未改动映射")
check(len(fixed[0]["field_mappings"]) == len(DB_COLS),
      "9 个 DB 字段完整保留（修复前会被剔剩 0 个）")
rec = PV.check_recommendations([m_db], _c1_target_models([m_db]))
check(rec["ok"] is True, "check_recommendations ok=True（修复前报『目标列 ID 不存在』）")

print("G. FHIR 映射的检查仍然生效（未把检查关掉）")
m_fhir = fhir_mapping()
check(TV._fhir_scope_of(m_fhir) == "Patient", "FHIR 映射识别为 Patient 资源")
req_fhir = TV._fhir_required_issues([{**m_fhir, "field_mappings": []}], assets=[])
check(any(i.get("check") == "fhir_required" for i in req_fhir),
      "FHIR 必填缺失仍被判 error（%d 项）" % len(req_fhir))
check(not TV._fhir_required_issues([m_fhir], assets=[]),
      "FHIR 映射字段齐全时不误报")

print("H. 强校验入口（生成前）对两组均放行")
strong_db = TV.validate_mapping_against_constraints([m_db])
check(strong_db["ok"] is True, "DB 映射强校验通过（errors=%d）" % strong_db["error_count"])
strong_fhir = TV.validate_mapping_against_constraints([m_fhir])
check(strong_fhir["ok"] is True, "FHIR 映射强校验通过（errors=%d）" % strong_fhir["error_count"])

print("I. FHIR 目标端点并入（base_url → HTTPOp 的 HTTPServer/HTTPPort，缺陷 N2 回归）")
from backend.routes.pipelines import _merge_fhir_target_config, _fhir_http_host_port  # noqa: E402

TG_FHIR = {"id": "TG_FHIR", "type": "FHIR",
           "connection": {"base_url": "http://localhost:52773/csp/healthshare/fhirserver/fhir/r4"},
           "runtime": {"connection": {
               "base_url": "http://localhost:52773/csp/healthshare/fhirserver/fhir/r4",
               "username": "superuser", "password": "SYS"}}}
cfg = _merge_fhir_target_config({}, TG_FHIR)
host, port = _fhir_http_host_port(cfg.get("base_url"))
check(cfg.get("base_url", "").endswith("/r4"), "base_url 已并入 target_config")
check((host, port) == ("localhost", "52773"),
      "解析 HTTPServer/HTTPPort = (%s, %s)（修复前为空主机 → socket :52773 报错）" % (host, port))
check(cfg.get("username") == "superuser", "凭据已并入（供 ^demo.Config(fhir,username/password)）")
check(_merge_fhir_target_config({}, {"id": "T2", "type": "FHIR", "connection": {},
                                     "runtime": {"connection": {"base_url": "http://iris:52773/x"}}}
                                ).get("base_url") == "http://iris:52773/x",
      "仅 runtime 契约（无顶层 connection）也能并入")
check(_fhir_http_host_port("") == ("", "52773"),
      "缺 base_url 时主机为空且显式告警（不静默指向别的服务器）")

print("\n%s  PASS=%d FAIL=%d" % ("TARGET-TYPE-OK" if FAIL == 0 else "TARGET-TYPE-FAIL",
                                 PASS, FAIL))
sys.exit(1 if FAIL else 0)
