# -*- coding: utf-8 -*-
"""CLINIC 演示数据生成（平台后端服务，供 UI「生成演示数据」按钮调用）。

与 tools/clinic_seed_data.py 同逻辑：一次性生成 n 位患者 + 就诊/诊断/药嘱。
诊断只取中文术语（iris-terminology, 国标 ICD-10 已剔 Z），药品只取中文药品目录（nrdl/cbih），
每行带 CodeSystem URI，便于后续 LLM 术语对照。

连接（backend 容器内，Docker 网络服务名）：
  术语源: iris-terminology:1972 / TERMINOLOGY
  目标库: iris:1972 / CLINIC
"""
import logging
import os
import random
from datetime import date, timedelta

import iris.dbapi

logger = logging.getLogger(__name__)

ICD_URI = "urn:cn-nhsa:icd10-gbt2016"
DRUG_URI = {"nrdl": "urn:cn-nhsa:drug-nrdl", "cbih": "urn:cn-nhsa:drug-cbih"}

random.seed(2026)

FAM = ["张", "李", "王", "刘", "陈", "杨", "赵", "黄", "周", "吴"]
GIV = ["伟", "芳", "娜", "静", "磊", "军", "洋", "敏", "杰", "娟"]
CITY = ["北京", "上海", "广州", "深圳", "成都", "杭州"]

SCENARIOS = [
    ("2型糖尿病", "高脂血症", ["二甲双胍", "阿托伐他汀钙"]),
    ("原发性高血压", "高脂血症", ["氨氯地平", "阿托伐他汀钙"]),
    ("急性上呼吸道感染", None, ["阿莫西林"]),
    ("慢性胃炎", None, ["奥美拉唑"]),
    ("骨关节炎", None, ["布洛芬"]),
    ("支气管哮喘", None, ["布地奈德"]),
    ("带状疱疹", None, ["阿昔洛韦"]),
    ("缺铁性贫血", None, ["硫酸亚铁"]),
    ("甲状腺功能减退", None, ["左甲状腺素"]),
    ("陈旧性心肌梗死", "高脂血症", ["阿司匹林", "阿托伐他汀"]),
]

TERM_HOST = os.getenv("TERM_IRIS_HOST", "iris-terminology")
CLINIC_HOST = os.getenv("CLINIC_IRIS_HOST", "iris")
IRIS_PORT = int(os.getenv("IRIS_PORT", "1972"))


def _pick(rows, kw):
    hit = [r for r in rows if kw in r[1]]
    if not hit:
        return None
    hit.sort(key=lambda r: len(r[1]))
    return hit[0]


def _load_terms():
    """从术语服务器读取中文诊断与中文药品（code, name, cs）。"""
    conn = iris.dbapi.connect(hostname=TERM_HOST, port=IRIS_PORT, namespace="TERMINOLOGY",
                              username="superuser", password="SYS")
    cur = conn.cursor()
    cur.execute("SELECT Code, Name FROM Terminology_Icd10.Concept")
    diags = [(r[0], r[1]) for r in cur.fetchall()]
    cur.execute("SELECT Code, Display, CodeSystemId FROM Terminology_Drug.Code")
    drugs = [(r[0], r[1], r[2]) for r in cur.fetchall() if r[0] and r[1]]
    conn.close()
    return diags, drugs


def _patients(n=10):
    out = []
    for i in range(n):
        gender = "male" if i % 3 != 0 else "female"
        out.append({
            "pid": f"P{i + 1:03d}",
            "mrn": f"MRN-{1000 + i}",
            "family": FAM[i],
            "given": GIV[(i * 2) % len(GIV)] + GIV[(i * 2 + 1) % len(GIV)],
            "gender": gender,
            "birth": f"{random.randint(1950, 2005):04d}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}",
            "phone": f"138{i:06d}{i:02d}",
            "city": CITY[i % len(CITY)],
        })
    return out


def generate_clinic_seed(n: int = 10) -> dict:
    """生成 n 位患者模拟数据到 CLINIC（幂等：先清四表）。返回统计。

    生成顺序保证引用链完整：①先生成患者（含患者 key：ID/MRN）；
    ②再按患者生成就诊 Encounter，用患者 key 填 PatientID；
    ③最后生成诊断/药嘱，同时用患者 key（PatientID）与就诊 key（EncounterID）填引用；
    落库后用 SQL 反查孤儿（encounter/diagnosis/medication 的父引用缺失）并返回 integrity_ok。
    """
    diags, drugs = _load_terms()
    logger.info("术语载入：诊断 %s / 药品 %s", len(diags), len(drugs))
    pats = _patients(n)

    conn = iris.dbapi.connect(hostname=CLINIC_HOST, port=IRIS_PORT, namespace="CLINIC",
                              username="superuser", password="SYS")
    cur = conn.cursor()
    for t in ["MedicationOrder", "Diagnosis", "Encounter", "Patient"]:
        cur.execute(f"DELETE FROM {t}")
    conn.commit()

    p_rows, e_rows, d_rows, m_rows, missed = [], [], [], [], []
    base = date(2026, 8, 1)
    for idx, p in enumerate(pats):
        takes = 2 if idx % 4 == 0 else 1
        for t in range(takes):
            sc = SCENARIOS[(idx + t) % len(SCENARIOS)]
            diag_kw, diag2_kw, drug_kws = sc
            eid = f"E{len(e_rows) + 1:04d}"
            start = base + timedelta(days=idx * 5 + t * 3)
            d1 = _pick(diags, diag_kw)
            if not d1:
                missed.append(f"诊断术语未命中: {diag_kw}")
                continue
            reason_code, reason_name = d1
            e_rows.append((eid, p["pid"], "AMB" if t == 0 else "IMP",
                           "门诊" if t == 0 else "住院", "finished",
                           start.isoformat() + "T08:00:00", "",
                           reason_code, reason_name))
            d_rows.append((f"D{len(d_rows) + 1:04d}", eid, p["pid"],
                           reason_code, reason_name, ICD_URI, 1,
                           (start - timedelta(days=30)).isoformat(), "active"))
            if diag2_kw:
                d2 = _pick(diags, diag2_kw)
                if d2:
                    d_rows.append((f"D{len(d_rows) + 1:04d}", eid, p["pid"],
                                   d2[0], d2[1], ICD_URI, 2,
                                   (start - timedelta(days=15)).isoformat(), "active"))
                else:
                    missed.append(f"伴随诊断未命中: {diag2_kw}")
            for k in drug_kws[:2]:
                med = _pick(drugs, k)
                if not med:
                    missed.append(f"药品术语未命中: {k}")
                    continue
                code, name, cs = med
                uri = DRUG_URI.get(cs, cs)
                m_rows.append((f"M{len(m_rows) + 1:04d}", eid, p["pid"],
                               code, name, uri, "500", "mg", "口服",
                               "每日2次", start.isoformat(), "active"))
        p_rows.append((p["pid"], p["mrn"], p["family"], p["given"], p["gender"],
                       p["birth"], p["phone"], f"{p['city']}示例路{idx + 1}号", p["city"]))

    cur.executemany("INSERT INTO Patient (ID,MRN,FamilyName,GivenName,Gender,BirthDate,Phone,Address,City) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", p_rows)
    cur.executemany("INSERT INTO Encounter (ID,PatientID,ClassCode,ClassDisplay,Status,PeriodStart,PeriodEnd,"
                    "ReasonCode,ReasonText) VALUES (?,?,?,?,?,?,?,?,?)", e_rows)
    cur.executemany("INSERT INTO Diagnosis (ID,EncounterID,PatientID,Code,Name,CodeSystem,Rank,OnsetDate,"
                    "ClinicalStatus) VALUES (?,?,?,?,?,?,?,?,?)", d_rows)
    cur.executemany("INSERT INTO MedicationOrder (ID,EncounterID,PatientID,MedicationCode,MedicationName,"
                    "CodeSystem,DosageValue,DosageUnit,Route,Frequency,StartDate,Status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", m_rows)
    conn.commit()

    # —— 引用完整性自检（生成顺序保证：患者先行 → 就诊引用患者 key → 药/诊断引用患者+就诊 key；
    #    此处用 SQL 反向校验“无孤儿”，确保下游按患者聚合/引用组装不会悬空）——
    orphan_sql = {
        "encounter_patient": "SELECT COUNT(*) FROM Encounter e LEFT JOIN Patient p "
                             "ON e.PatientID=p.ID WHERE p.ID IS NULL",
        "diagnosis_patient": "SELECT COUNT(*) FROM Diagnosis d LEFT JOIN Patient p "
                             "ON d.PatientID=p.ID WHERE p.ID IS NULL",
        "diagnosis_encounter": "SELECT COUNT(*) FROM Diagnosis d LEFT JOIN Encounter e "
                               "ON d.EncounterID=e.ID WHERE e.ID IS NULL",
        "medication_patient": "SELECT COUNT(*) FROM MedicationOrder m LEFT JOIN Patient p "
                              "ON m.PatientID=p.ID WHERE p.ID IS NULL",
        "medication_encounter": "SELECT COUNT(*) FROM MedicationOrder m LEFT JOIN Encounter e "
                                "ON m.EncounterID=e.ID WHERE e.ID IS NULL",
    }
    orphans = {}
    for name, sql in orphan_sql.items():
        cur.execute(sql)
        orphans[name] = int(cur.fetchone()[0] or 0)
    conn.close()
    stat = {"patients": len(p_rows), "encounters": len(e_rows),
            "diagnoses": len(d_rows), "medications": len(m_rows), "missed": missed,
            "orphans": orphans, "integrity_ok": all(v == 0 for v in orphans.values())}
    logger.info("CLINIC 演示数据生成: %s", stat)
    return stat
