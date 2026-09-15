# -*- coding: utf-8 -*-
"""向 CLINIC 追加 1 个新患者（含就诊/诊断/药嘱，引用完整），不清空现有数据。"""
import random

import iris.dbapi

from backend.services.clinic_seed import (CITY, DRUG_URI, FAM, GIV, ICD_URI,
                                          IRIS_PORT, SCENARIOS, _load_terms, _pick)

CLINIC = dict(hostname="iris", port=IRIS_PORT, namespace="CLINIC",
              username="superuser", password="SYS")

FALLBACK_DIAG = [("E11.900", "2型糖尿病"), ("E78.500", "高脂血症")]
FALLBACK_DRUG = [("B01AC06", "阿司匹林肠溶片", "nrdl")]


def next_id(cur, table, prefix):
    """按现有 ID 数字后缀取最大 +1（避免 MAX/LIKE 在不同排序下的偏差）。"""
    cur.execute(f"SELECT ID FROM {table}")
    nums = []
    for r in cur.fetchall():
        s = str(r[0] or "")
        if s.startswith(prefix) and s[len(prefix):].isdigit():
            nums.append(int(s[len(prefix):]))
    return f"{prefix}{(max(nums) if nums else 0) + 1:04d}"


def main():
    try:
        diags, drugs = _load_terms()
    except Exception as exc:  # noqa: BLE001 - 术语服务可选，测试数据用内置示例
        print("术语服务不可用，使用内置示例码:", exc)
        diags, drugs = FALLBACK_DIAG, FALLBACK_DRUG

    conn = iris.dbapi.connect(**CLINIC)
    cur = conn.cursor()

    pid = next_id(cur, "Patient", "P")
    eid = next_id(cur, "Encounter", "E")
    idx = int(pid[1:]) - 1
    fam, giv = FAM[idx % len(FAM)], GIV[idx % len(GIV)]
    mrn = f"MRN-{1000 + idx}"
    gender = "male" if idx % 3 else "female"
    birth = f"{random.randint(1950, 2005):04d}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"
    phone = f"138{idx:06d}{idx:02d}"
    city = CITY[idx % len(CITY)]

    kw, kw2, drug_kws = SCENARIOS[idx % len(SCENARIOS)]
    d1 = _pick(diags, kw) or FALLBACK_DIAG[0]
    d2 = _pick(diags, kw2) if kw2 else None

    # ① 患者
    cur.execute("INSERT INTO Patient (ID,MRN,FamilyName,GivenName,Gender,BirthDate,Phone,Address,City) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [pid, mrn, fam, giv, gender, birth, phone, f"{city}测试路{idx + 1}号", city])
    # ② 就诊（引用患者 key）
    cur.execute("INSERT INTO Encounter (ID,PatientID,ClassCode,ClassDisplay,Status,PeriodStart,"
                "PeriodEnd,ReasonCode,ReasonText) VALUES (?,?,?,?,?,?,?,?,?)",
                [eid, pid, "AMB", "门诊", "finished", "2026-09-15T09:00:00", "",
                 d1[0], d1[1]])
    # ③ 诊断（引用患者 + 就诊 key）
    dids = []
    for rank, d in enumerate([d1] + ([d2] if d2 else []), 1):
        did = next_id(cur, "Diagnosis", "D")
        dids.append(did)
        cur.execute("INSERT INTO Diagnosis (ID,EncounterID,PatientID,Code,Name,CodeSystem,Rank,"
                    "OnsetDate,ClinicalStatus) VALUES (?,?,?,?,?,?,?,?,?)",
                    [did, eid, pid, d[0], d[1], ICD_URI, rank, "2026-09-01", "active"])
    # ④ 药嘱（引用患者 + 就诊 key）
    mids = []
    for k in (drug_kws or [None])[:2]:
        med = (_pick(drugs, k) if k else None) or FALLBACK_DRUG[0]
        code, name, cs = med[0], med[1], (med[2] if len(med) > 2 else "nrdl")
        mid = next_id(cur, "MedicationOrder", "M")
        mids.append(mid)
        cur.execute("INSERT INTO MedicationOrder (ID,EncounterID,PatientID,MedicationCode,"
                    "MedicationName,CodeSystem,DosageValue,DosageUnit,Route,Frequency,StartDate,Status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [mid, eid, pid, code, name, DRUG_URI.get(cs, cs), "500", "mg", "口服",
                     "每日2次", "2026-09-15", "active"])
    conn.commit()

    print(f"新增患者 {pid}({fam}{giv}, {mrn}) / 就诊 {eid} / 诊断 {dids} / 药嘱 {mids}")
    for t in ("Patient", "Encounter", "Diagnosis", "MedicationOrder"):
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        print(f"  CLINIC {t}: {cur.fetchone()[0]}")
    # 引用完整性反查
    cur.execute("SELECT COUNT(*) FROM Encounter e LEFT JOIN Patient p ON e.PatientID=p.ID WHERE p.ID IS NULL")
    o1 = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM Diagnosis d LEFT JOIN Encounter e ON d.EncounterID=e.ID WHERE e.ID IS NULL")
    o2 = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM MedicationOrder m LEFT JOIN Encounter e ON m.EncounterID=e.ID WHERE e.ID IS NULL")
    o3 = cur.fetchone()[0]
    print("orphans:", {"encounter_patient": o1, "diagnosis_encounter": o2, "med_encounter": o3})
    conn.close()


if __name__ == "__main__":
    main()
