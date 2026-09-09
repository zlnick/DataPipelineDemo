# -*- coding: utf-8 -*-
"""构建中文诊断测试集 v1：国标 ICD-10 中文诊断 ↔ US Core SNOMED 样本（手工精选对齐）。

产物（不进 IRIS/演示系统）: data/testsets/cn2snomed_testset_v1.csv
字段: cn_code(国标) | cn_name | snomed_code | snomed_display | match_kw | note
注意: 对齐为人工语义精选 ground truth；SNOMED 码均取自 uscore 样本（可公开验证）。
用法: python3 tools/build_cn2snomed_testset.py
"""
import csv
import os

BASE = os.path.join(os.path.dirname(__file__), "..", "data", "testsets")
CN_CSV = os.path.join(BASE, "..", "terms-inbox", "icd10_main.csv")
SNOMED_SAMPLE = os.path.join(BASE, "uscore_condition_snomed_sample.csv")
OUT = os.path.join(BASE, "cn2snomed_testset_v1.csv")

# (中文检索关键词, SNOMED code, SNOMED display, 备注)
PAIRS = [
    ("原发性高血压", "1201005", "Benign essential hypertension", "常见慢病"),
    ("陈旧性心肌梗死", "1755008", "Old myocardial infarction", "心内科"),
    ("中度抑郁发作", "832007", "Moderate major depression", "精神科 F32"),
    ("急性左心衰竭", "364006", "Acute left-sided heart failure", "心衰"),
    ("慢性乙型病毒性肝炎", "1116000", "Chronic aggressive type B viral hepatitis", "感染科"),
    ("股骨颈骨折", "1705000", "Closed fracture of base of neck of femur", "创伤/骨科"),
    ("多发性肋骨骨折", "1261007", "Fracture of multiple ribs", "创伤"),
    ("高血压肾脏病", "193003", "Benign hypertensive renal disease", "肾病"),
    ("肾小管酸中毒", "1776003", "Renal tubular acidosis", "肾病"),
    ("急性肾盂肾炎", "3999002", "Acute pyelitis without renal medullary necrosis", "泌尿感染"),
]


def find_cn(kw: str):
    """在国标主表中按名称包含定位：返回（最短名称优先排序）列表。"""
    out = []
    with open(CN_CSV, encoding="utf-8-sig") as fh:
        rd = csv.reader(fh)
        next(rd, None)
        for code, name in rd:
            if kw in name:
                out.append((code, name))
    out.sort(key=lambda x: len(x[1]))
    return out


def main():
    sample = {}
    with open(SNOMED_SAMPLE, encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            sample[row["code"]] = row["display"]

    rows, unmatched = [], []
    for kw, code, display, note in PAIRS:
        assert code in sample, f"SNOMED {code} 不在样本中"
        cn = find_cn(kw)
        if not cn:
            unmatched.append((kw, code, display))
            continue
        c, n = cn[0]  # 精确度不足时取首个，人工复核列输出
        rows.append({"cn_code": c, "cn_name": n, "snomed_code": code,
                     "snomed_display": display, "match_kw": kw, "note": note,
                     "cn_candidates": len(cn)})

    os.makedirs(BASE, exist_ok=True)
    with open(OUT, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows
                           else ["cn_code", "cn_name", "snomed_code", "snomed_display",
                                 "match_kw", "note", "cn_candidates"])
        w.writeheader()
        w.writerows(rows)
    print(f"写入 {len(rows)} 条 -> {OUT}")
    if unmatched:
        print("未匹配中文(kw 在国标表无命中):")
        for kw, code, display in unmatched:
            print(f"  - {kw}  ({code} {display})")
    print("\n--- 对齐预览（请人工复核中文名是否精确） ---")
    for r in rows:
        cand = "，候选%d" % r["cn_candidates"] if r["cn_candidates"] > 1 else ""
        print(f"  {r['cn_code']} {r['cn_name'][:38]}  <->  {r['snomed_code']} {r['snomed_display'][:44]}{cand}")


if __name__ == "__main__":
    main()
