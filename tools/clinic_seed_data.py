# -*- coding: utf-8 -*-
"""CLINIC 样例数据生成：10 患者 + 就诊/诊断/药嘱（诊断与药品只取中文术语集）。

诊断取值：iris-terminology Terminology_Icd10.Concept（国标 ICD-10，已剔除 Z 章），CodeSystem=urn:cn-nhsa:icd10-gbt2016
药品取值：Terminology_Drug.Code（医保 nrdl / 商保 cbih），CodeSystem=urn:cn-nhsa:drug-nrdl / :cbih
每个场景为「诊断(主+次) + 对症中文药品」配对，保证语义合理、术语码合法可审计。

用法: python3 tools/clinic_seed_data.py
"""
import random
from datetime import date, timedelta

import iris.dbapi

TERM = dict(hostname="127.0.0.1", port=51774, namespace="TERMINOLOGY",
            username="superuser", password="SYS")
CLINIC = dict(hostname="127.0.0.1", port=1972, namespace="CLINIC",
              username="superuser", password="SYS")
ICD_URI = "urn:cn-nhsa:icd10-gbt2016"
DRUG_URI = {"nrdl": "urn:cn-nhsa:drug-nrdl", "cbih": "urn:cn-nhsa:drug-cbih"}

random.seed(2026)

# 姓氏/名/城市
FAM = ["张", "李", "王", "刘", "陈", "杨", "赵", "黄", "周", "吴"]
GIV = ["伟", "芳", "娜", "静", "磊", "军", "洋", "敏", "杰", "娟"]
CITY = ["北京", "上海", "广州", "深圳", "成都", "杭州"]

# 场景池：主诊断kw / 伴随诊断kw(可空) / 对症药kw列表（均中文，从术语库实时匹配）
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


def pick(rows, kw):
    """在术语行列表 (code, name, cs) 中按名称包含匹配，返回最短名首条或 None。"""
    hit = [r for r in rows if kw in r[1]]
    if not hit:
        return None
    hit.sort(key=lambda r: len(r[1]))
    return hit[0]


def load_terms():
    """返回诊断行与药品行（code, name, codeSystemId/uri）。"""
    conn = iris.dbapi.connect(**TERM)
    cur = conn.cursor()
    cur.execute("SELECT Code, Name FROM Terminology_Icd10.Concept")
    diags = [(r[0], r[1]) for r in cur.fetchall()]
    cur.execute("SELECT Code, Display, CodeSystemId FROM Terminology_Drug.Code")
    drugs = [(r[0], r[1], r[2]) for r in cur.fetchall() if r[0] and r[1]]
    conn.close()
    return diags, drugs


def patients(n=10):
    """10 位患者人口学。"""
    out = []
    for i in range(n):
        gender = "male" if i % 3 != 0 else "female"
        year = random.randint(1950, 2005)
        month = random.randint(1, 12)
        day = random.randint(1, 28)
        out.append({
            "pid": f"P{i + 1:03d}",
            "mrn": f"MRN-{1000 + i}",
            "family": FAM[i],
            "given": GIV[(i * 2) % len(GIV)] + GIV[(i * 2 + 1) % len(GIV)],
            "gender": gender,
            "birth": f"{year:04d}-{month:02d}-{day:02d}",
            "phone": f"138{i:06d}{i:02d}",
            "city": CITY[i % len(CITY)],
        })
    return out
def main():
    diags, drugs = load_terms()
    print("术语载入：诊断", len(diags), "药品", len(drugs))

    pats = patients(10)
    conn = iris.dbapi.connect(**CLINIC)
    cur = conn.cursor()
    for t in ["MedicationOrder", "Diagnosis", "Encounter", "Patient"]:
        cur.execute(f"DELETE FROM {t}")
    conn.commit()

    p_rows, e_rows, d_rows, m_rows = [], [], [], []
    base = date(2026, 8, 1)
    for idx, p in enumerate(pats):
        takes = 2 if idx % 4 == 0 else 1
        for t in range(takes):
            sc = SCENARIOS[(idx + t) % len(SCENARIOS)]
            diag_kw, diag2_kw, drug_kws = sc
            eid = f"E{len(e_rows) + 1:04d}"
            start = base + timedelta(days=idx * 5 + t * 3)
            d1 = pick(diags, diag_kw)
            if not d1:
                print("!! 诊断术语未命中:", diag_kw)
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
                d2 = pick(diags, diag2_kw)
                if d2:
                    d_rows.append((f"D{len(d_rows) + 1:04d}", eid, p["pid"],
                                   d2[0], d2[1], ICD_URI, 2,
                                   (start - timedelta(days=15)).isoformat(), "active"))
                else:
                    print("!! 伴随诊断未命中:", diag2_kw)
            for k in drug_kws[:2]:
                med = pick(drugs, k)
                if not med:
                    print("!! 药品术语未命中:", k)
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
    print(f"写入：患者 {len(p_rows)} / 就诊 {len(e_rows)} / 诊断 {len(d_rows)} / 药嘱 {len(m_rows)}")
    conn.close()


if __name__ == "__main__":
    main()

