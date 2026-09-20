# -*- coding: utf-8 -*-
"""术语双 coding 完整性检查（`term_gap`）与 AI 链路提示词条款回归（纯离线）。

背景（2026-09-17 十六轮遗留②，实测）：
- `MedicationRequest.medication` 的目标列 note 写明「建议双 coding：中文药码 + RxNorm(经术语对照)」，
  但 Agent A 曾把 transform 写成 `code`（受控指令里 `code` = 仅类型提示，**不产生 coding**）→
  运行期只落 NRDL 单码；而 C1 的 LLM 在"零 error"时根本不会被调用（`if not errors: return ok`）
  → 该缺口无人检查 = 静默。
修法（AI 路径）：① Agent A / C1 提示词加硬约束（note 要求双 coding ⇒ 必须 term_map）；
② 新增**事实检查** `transformation_validator._term_gap_issues`（只报缺口，skill/源列由 LLM 决策）。

本用例为纯离线回归：不调 LLM、不写数据。
    python3 tools/test_term_gap_check.py        # 宿主（容器内执行：./run.sh test_term_gap_check.py）
"""
import json
import sys

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" | {extra}" if extra else ""))


def main() -> int:
    from backend.services import transformation_validator as TV

    med_asset = [{"name": "MedicationOrder",
                  "fields": ["ID", "PatientID", "MedicationCode", "MedicationName",
                             "CodeSystem", "StartDate", "Status"]}]
    diag_asset = [{"name": "Diagnosis",
                   "fields": ["ID", "PatientID", "EncounterID", "Code", "Name", "OnsetDate"]}]
    no_code_asset = [{"name": "MedicationOrder", "fields": ["ID", "PatientID", "Status"]}]

    def medmap(transform, target="medication"):
        return {"id": "M", "source": "MedicationOrder", "target_table": "MedicationRequest",
                "target_type": "FHIR",
                "field_mappings": [{"source": "MedicationOrder.MedicationCode",
                                    "target": target, "transform": transform}]}

    # A 组：检查本身
    a1 = TV._term_gap_issues([medmap("code")], med_asset)
    check("A1 缺 term_map → 报 term_gap（error）",
          len(a1) == 1 and a1[0]["check"] == "term_gap" and a1[0]["severity"] == "error",
          json.dumps(a1, ensure_ascii=False)[:180])
    check("A2 已有 term_map → 不报", TV._term_gap_issues([medmap("term_map:cn2rx")], med_asset) == [])
    check("A3 源侧无编码列 → fail-open 不报",
          TV._term_gap_issues([medmap("code")], no_code_asset) == [])
    check("A4 该列未映射 → 交给 fhir_required（此处不报）",
          TV._term_gap_issues([medmap("code", target="status")], med_asset) == [])
    check("A5 transform=null 同样报",
          len(TV._term_gap_issues([medmap(None)], med_asset)) == 1)
    db_map = {"id": "M2", "source": "MedicationOrder", "target_table": "MedicationRequest",
              "target_type": "DB", "field_mappings": [
                  {"source": "MedicationOrder.MedicationCode", "target": "medication",
                   "transform": "code"}]}
    check("A6 非 FHIR 声明 → 跳过（类型感知）",
          TV._term_gap_issues([db_map], med_asset) == [])

    cond = {"id": "M3", "source": "Diagnosis", "target_table": "Condition", "target_type": "FHIR",
            "field_mappings": [{"source": "Diagnosis.Code", "target": "code", "transform": "code"}]}
    c1 = TV._term_gap_issues([cond], diag_asset)
    check("A7 诊断侧 Condition.code 缺 term_map:cn2snomed → 报", len(c1) == 1,
          json.dumps(c1, ensure_ascii=False)[:160])
    cond["field_mappings"][0]["transform"] = "term_map:cn2snomed"
    check("A8 诊断侧已 term_map → 不报", TV._term_gap_issues([cond], diag_asset) == [])

    # B 组：检查已接入 C1 主循环（源码事实，避免被无意摘除）
    import inspect
    src = inspect.getsource(TV.validate_and_fix_transformation)
    check("B1 C1 主循环调用 _term_gap_issues", "_term_gap_issues(" in src)

    # C 组：提示词硬约束在位（Agent A + C1）
    from backend.services import llm_client
    a_prompt = llm_client.SYSTEM_PROMPT_TRANSFORMATION
    check("C1 Agent A 提示词含『必须 term_map』硬约束",
          "必须是 term_map:<skill_id>" in a_prompt and "不产生任何目标体系 coding" in a_prompt)
    v_prompt = TV.SYSTEM_PROMPT_TRANS_VALIDATE
    check("C2 C1 提示词含术语双 coding 缺口条款",
          "术语双 coding 缺口" in v_prompt and "不得用它代替 term_map" in v_prompt)

    # D 组：受控指令注册表（term_map skills / code 语义）
    from backend.services import transform_directives as TD
    ids = {d["id"]: d for d in TD.list_directives()}
    check("D1 term_map skills = cn2snomed/cn2rx",
          set(ids.get("term_map", {}).get("skills") or []) == {"cn2snomed", "cn2rx"})
    check("D2 code 登记为类型提示（不产生 coding）",
          "类型提示" in str(ids.get("code", {}).get("note") or ""))

    print(f"\n== 结果：PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        print("FAIL:", FAIL)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
